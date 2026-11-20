# integration-gateway-a2a

Standalone, self-hosted Node.js/TypeScript middleware engine mirroring Pipedream's
`components/` modular structure, with first-class native plugins. No Pipedream cloud
dependencies. Runtime: Node 20+, TypeScript ESM, plain `node:http` (no framework).

> **This fork** adds an A2A (Agent2Agent) front door: agent cards for
> discovery and a JSON-RPC endpoint that delegates tasks to the
> gateway's components as skills. See [docs/A2A.md](docs/A2A.md).
> It also fixes a loader bug (unbounded scan fan-out silently dropped
> most `component.json` metadata under file-descriptor pressure) and
> repoints the lockfile from a dead npm mirror to registry.npmjs.org.

## Quickstart

```bash
npm ci
npm run dev
```

The health and registry routes remain available. Execution is **fail-closed by default**: the standalone server has no trusted caller resolver or Foundry approval verifier configured, so `POST /api/v1/execute` refuses calls until those are wired. This is deliberate; it prevents an unconfigured gateway from treating an unverified token as approval.

Run the local-only loopback demonstration (test issuer, signed ledger, REST + A2A checks, restart invalidation) with:

```bash
PORT=3456 npm run demo:approval-gate
```

The demo binds only to `127.0.0.1`, writes its non-secret output under `demo-output/`, and does not configure or start the normal all-interface server. The local test issuer must not be enabled in a deployed gateway.

Other scripts: `npm start` (same as dev), `npm test` (vitest), `npm run build` (tsc typecheck).

## HTTP API

### Approval gate

`Router.execute` is the shared gate for REST and A2A. It consumes an Ed25519-signed, single-use approval before loading a component or resolving any app credentials. The signed approval references a signed `approval.granted` ledger event and binds the caller, exact target, and SHA-256 hash of the event payload. Its lifetime is exactly 10 minutes. Consumed approvals are not retried; component retries occur only after the gate and consume.

REST sends the token in `x-approval-token`. A2A sends it as `approvalToken` in the data part. In both cases, `createServer` requires a trusted `resolveCaller` callback; the default is intentionally absent and denies execution. Do not treat a client-supplied caller header as identity unless a trusted authenticated proxy validates it. The approval is additional to existing app credentials; it does not supply them.

The included `LocalTestApprovalIssuer` is clearly marked `LOCAL_TEST_ONLY`, creates a process-ephemeral key and in-memory approval state, and exists only for tests and the loopback demo. Its signing keys and approval state are not restored after restart. It is not the Foundry issuer. Production remains fail-closed until the gateway is wired to Foundry's trusted public key and ledger lookup/atomic-consume service, and a trusted caller identity is passed into `Router.execute`. If the ledger/verifier is unavailable, the production adapter must refuse the call.

- `POST /api/v1/execute` — body `ExecuteRequest` → `ExecuteResponse`:
  - `target`: `"external/<app>/<component>"`, `"native/<module>/<component>"`, or `"native/<module>"`
    (a native module dir that is itself the component).
  - `event?`: arbitrary payload passed to the component.
  - `auth?`: per-request credentials, merged **over** env-derived auth (`APP_<APPNAME>_*`,
    e.g. `APP_GITHUB_ACCESS_TOKEN=...` → `ctx.auth.access_token`).
  - `config?`: arbitrary config; `config.httpOverride` may inject a fake `fetch` (used by tests).
  - `retry?`: `{ attempts?: number; backoffMs?: number }` — default 3 attempts, 250ms backoff
    doubling per attempt. Actual attempts are reported in the response.
  - Every execution also requires a valid approval token in `x-approval-token`. Missing,
    expired, replayed, caller/target/event-mismatched, or unchained tokens return HTTP 403
    before a component is loaded or app credentials are resolved. A2A uses `approvalToken`
    in the invocation data part.

- `GET /api/v1/registry` — recursively walks `registry/` (filesystem-only, no imports;
  scales to hundreds of apps) and lists every component discovered via `component.json`/`index.ts`.
- `GET /health` — `{ok:true}`.

Every execution is appended as one JSON line to `logs/executions.jsonl`
(target, event, result/error, durationMs, attempts). Sensitive keys are redacted
(configure via `LOG_REDACT_KEYS`, comma-separated).

Component failures never crash the server: errors are caught (after retries) and returned
in the `ExecuteResponse.error` field.

## Bulk Registry

This repo ships a **full mirror of Pipedream's component library**: **3,402 apps /
14,683 components** (11,497 actions + 3,186 sources) under `registry/external/`,
mirrored from [`PipedreamHQ/pipedream`](https://github.com/PipedreamHQ/pipedream) (MIT
licensed). Each upstream `.mjs` component was codemodded by `scripts/ingest.mjs` into a
gateway component (`component.json` + `index.ts` adapter over a Pipedream-compat shim),
with the pristine upstream file preserved alongside as `original.mjs`.

### Browsing: `GET /api/v1/manifest`

`GET /api/v1/manifest` serves the pre-built registry index
(`registry/external/index.json`): totals, per-app metadata, and components grouped by
vertical **category** (Developer Tools, Communication, Finance & Payments, CRM & Sales,
Marketing, Databases & Data, Cloud & DevOps, Productivity, Social Media, E-Commerce,
AI & ML, Analytics, and more). The server auto-regenerates the index via
`scripts/registry-manifest.mjs` when it detects it is stale.

### Executing any component

```bash
curl -s -X POST http://localhost:3000/api/v1/execute \
  -H 'content-type: application/json' \
  -H 'x-approval-token: <single-use-token-from-trusted-issuer>' \
  -d '{"target":"external/<app>/<component-slug>","event":{...},"auth":{...}}'
```

The placeholder is not a usable token. The standalone server intentionally refuses execution until its Foundry verifier and trusted caller identity resolver are configured.

- `target` is `external/<app>/<slug>` as listed in the manifest,
  e.g. `external/github/create-issue`.
- **Auth** is resolved per app: pass credentials in the request `auth` object, or set
  `APP_<APPNAME>_*` env vars (e.g. `APP_GITHUB_ACCESS_TOKEN=...` →
  `ctx.auth.access_token`). Request `auth` is merged over env-derived auth.

### Quarantine

Components whose code needs their app's **npm SDK** at import time (e.g. `@slack/web-api`,
`stripe`) are **quarantined** — import-verification could not load them without that
dependency. **3,202 components** are currently quarantined. To un-quarantine one, install
the app's SDK in the gateway root:

```bash
npm i <pkg>        # e.g. npm i @slack/web-api  → un-quarantines slack's components
```

then re-run `node scripts/validate-registry.mjs` to confirm.

### Validation summary

`scripts/validate-registry.mjs` import-verified every component via tsx:
**11,468 components import-verified OK**; 3,202 quarantined (missing npm SDKs, see
above); **5 known upstream edge cases** fail for reasons in the upstream code itself
(see `validation-report.json` → `failed`: two `daffy` components using an undefined
`defineApp`, one `notiff` component, two `sftp_password_based_auth` components with
malformed `props`). Full details: `validation-report.json`; ingestion details:
`ingestion-report.json`.

### Registry tooling

```bash
node scripts/ingest.mjs             # codemod pipeline: Pipedream clone → registry/external/
node scripts/registry-manifest.mjs  # rebuild registry/external/index.json (categories, totals)
node scripts/validate-registry.mjs  # import-verify all components → validation-report.json
```

## Directory layout

```
src/            core engine (types, loader, router, server, event-bus, retry, logger)
registry/
  external/<app>/components/<comp>/{component.json,index.ts,original.mjs}   mirrored Pipedream components
  native/<module>/{component.json,index.ts}                                  our own modules
schemas/        canonical event JSON schemas
tests/          vitest suites
```

## Adding an external mirror

1. Copy the upstream component from `PipedreamHQ/pipedream` `components/<app>/...`
   into `registry/external/<app>/components/<comp-name>/original.mjs` (keep pristine).
2. Write `component.json`: `{key,name,description,type:"action"|"source",version,propsSchema?}`.
3. Write `index.ts` — a thin adapter implementing `IIntegrationComponent`
   (see `src/types.ts`). Mapping contract:
   - `this.<app>.$auth.oauth_access_token` → `ctx.auth.access_token`
   - `$.export` / `$emit` → `ctx.emit(topic, canonicalEvent)`
   - `axios` → `ctx.http` (fetch-compatible); the `$` interface → `ctx`
   - Strip all `@pipedream/platform` imports.
4. Add/update `registry/external/<app>/manifest.json`:
   `{app, mirroredFrom: "PipedreamHQ/pipedream", commit?, components: [...keys]}`.
5. Provide credentials via env (`APP_<APPNAME>_<KEY>`) or per-request `auth`.

## Adding a native module

1. Create `registry/native/<module>/` with `component.json` and `index.ts`.
2. `index.ts` default-exports an `IIntegrationComponent`:

```ts
import type { IIntegrationComponent } from '../../../src/types.js';

export default {
  def: { key: 'native_my_module', name: 'My Module', description: '...', version: '0.1.0', type: 'action' },
  async run(ctx, event, steps) {
    ctx.log('info', 'hello', { event });
    return { ok: true };
  },
} satisfies IIntegrationComponent;
```

3. Execute it only after the trusted Foundry verifier is wired, providing both app credentials and an `x-approval-token` issued for that caller, target, and event.

## RuntimeContext (what components get)

| field | purpose |
|---|---|
| `auth` | per-app credentials (request `auth` over `APP_<APPNAME>_*` env) |
| `config` | app-level config (`config` from request; `httpOverride` injectable) |
| `http` | fetch-compatible HTTP client |
| `log`  | namespaced structured logger |
| `emit` | publish a `CanonicalEvent` to the in-process event bus |
| `store` | async key-value store (in-memory, optional file persistence via `STORE_FILE`) |

## Event bus

In-process async bus (`src/event-bus.ts`): `subscribe(topic, handler)` returns an
unsubscribe function; `emit(topic, payload)` awaits all handlers and isolates handler
errors (a throwing subscriber never breaks the emitter).

See `.env.example` for all configuration knobs.
