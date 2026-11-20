# Account Orchestrator — Phases 1–2

A local, private account/service registry seeded from the network map, with a
read-only query CLI and an explicit live-health checker. Phase 2 adds HTTP checks
for service URLs, TCP checks for registered TCP ports, an on-demand check command,
and a supervised periodic runner. No MCP/A2A/REST front door, prefetch, swarms, or
router is included; those remain Phases 3–5.

Working title: "Account Orchestrator". The real name is Jason's to pick; it appears
in one place (`package.json`, the `ao` command, the `AO_DB` variable) so renaming is
a five-minute job.

## Status

| | |
| --- | --- |
| Code | complete, compiles clean under `strict` TypeScript |
| Tests | 44 passing (`npm test`) |
| Registry | seeded and verified against the exact Markdown network map |
| Map source | `jason-network-map-2026-11-09.md`, 468 lines; SHA-256 `387420fdbe07cf16790b06b3dab95d2df48ac71433d8726589296e54900a5f12` |

The exact Markdown map seeds 10 accounts, 6 machines/devices, 315 services/repositories,
3 ports, and reports 5 gaps. Verification passes all 12 hard checks. Acceptance
details are in [`reports/round4-verification-2026-11-09.md`](reports/round4-verification-2026-11-09.md).

```bash
MAP=/path/to/jason-network-map-2026-11-09.md
npm run seed -- --map "$MAP"
npm run verify -- --map "$MAP"
```

The parser reads the Markdown as provided; it does not create a normalized map file.
It also retains the Round 3 direct parser for the line-oriented text export. See
[docs/MAP-FORMAT.md](docs/MAP-FORMAT.md) for supported input shapes.

## Quick start

```bash
npm ci
npm run build
MAP=/path/to/jason-network-map-2026-11-09.md

# 1. Build the registry from the map
npm run seed -- --map "$MAP"

# 2. Run live checks and view the latest results
npm run ao -- check
npm run ao -- live-status

# 3. Ask read-only registry questions
npm run ao -- stats
npm run ao -- accounts --status live
npm run ao -- account github:fixture-user-example-com
npm run ao -- machines
npm run ao -- ports --machine acer
npm run ao -- lookup praxis

# 4. Prove it against the map
npm run verify -- --map "$MAP"
```

`npm run verify` exits `0` when every hard check passes, `1` when one fails, and
`2` when the map is unavailable — in which case it prints `NOT VERIFIED` rather than
pretending. A Markdown report lands in `reports/`.

## Commands

| Command | What it answers |
| --- | --- |
| `ao stats` | what the registry holds, where it came from, when it was seeded |
| `ao accounts` | every account, with purpose, status, and service count (`--status`, `--provider`, `--search`) |
| `ao account <id\|login\|provider>` | one account: its services, its ports, what it links to |
| `ao services` | services by kind, machine, owning account |
| `ao service <id>` | one service and its ports |
| `ao machines` / `ao machine <name>` | the six machines/devices, their tailnet addresses, every port on each |
| `ao ports [--machine <name>]` | the port list, flat |
| `ao check` | run one health-check cycle now (`--timeout-ms`, `--concurrency`) |
| `ao run` | run checks immediately, then repeat every five minutes by default (`--interval-ms`, or `AO_CHECK_INTERVAL_MS`) |
| `ao live-status` | show map status beside live status, health, and last-seen for every service and port |
| `ao links` | account-to-account links |
| `ao gaps` | map lines the parser could not place |
| `ao reference` | status keys and legends kept as text, not records |
| `ao warnings` | merges and inferences worth a human glance |
| `ao lookup <text>` | search accounts, services and machines at once |

All query commands take `--json` and `--db <path>` and open SQLite read-only.
Only `seed`, `check`, and the supervised `run` loop write; health checks update
live-health columns in place and preserve the map's original status.

## What it stores

One SQLite file, `data/registry.sqlite`, local only, no cloud copy.

- **account** — `id`, `provider`, `login`, `purpose`, `status`, `secret_ref`, `last_checked`, `source_ref`, `notes`
- **machine** — `id`, `name`, `tailnet_ip`, `os`, `role`, `status`, `last_seen`
- **service** — map snapshot (`status`) plus live `live_status`, `health`, and `last_seen`; also `id`, `name`, `kind`, `account_id`, `url`, `machine_id`
- **port** — `service_id`, `machine_id`, `port`, `protocol`, `health`, `last_seen`
- **account_link** — `from_account`, `to_account`, `relation`
- **import_note** — gaps and warnings from the last seed, so `ao gaps` works offline

Two vocabulary decisions are enforced by `CHECK` constraints rather than convention:
`status` is only ever `live | dead | unknown`, and `purpose` can be `unknown` but
never empty. `unknown` is a recorded answer, not a missing one.

The checker preserves each service's original map `status` and writes live results
separately: `service.live_status` is `live | dead | unknown`; `service.health` is
`up | down | unknown`. TCP port health is `up | down | unknown`; UDP and unprobeable
endpoints remain `unknown`. Successful or failed attempts record `last_seen`; an
unknown endpoint keeps it null.

## The security rules, and where they are enforced

| Rule | Enforcement |
| --- | --- |
| No inbound listener | the checker initiates HTTP probes for registered URLs and TCP probes only to literal machine IPs recorded in the registry; nothing accepts inbound connections |
| No secrets in the registry or the repo | `src/secrets.ts` scans the whole map before parsing, every string before insert, and every stored text column during `verify`. Strong patterns block the seed; entropy heuristics only warn |
| References only, never values | `secret_ref` must be `scheme:path` (`env:GITHUB_TOKEN`, `keychain:github`, `1password:github`); anything else is refused with a reason |
| Default-deny | no callers exist yet. The CLI is the only reader, on the local filesystem |
| Signed and verifiable where it matters | not built in Phase 1 — there is nothing to sign yet. Phase 3 reuses Foundry's pattern rather than inventing one |
| Controlled writes | `seed` rebuilds the map snapshot; `check` and `run` atomically update only live-health fields. `verify` and query commands open SQLite read-only |

A seed that finds a credential in the map stops, prints `file:line` for each hit with
the value masked, and writes nothing.

## Fix round — what changed and why

Five defects were reported against the first build. All five are fixed, each with a
test that names it:

| Defect | Fix | Test |
| --- | --- | --- |
| `Thing / What it is / Status` tables produced 27 gaps | wide-table recognition, dispatched by header and section: accounts, machines, services | `shapes.test.ts` — defects 1a, 1b, 1c |
| Markdown-link URLs were stripped, leaving every service with `url NULL` | link targets are carried through the block model; the link text stays the name | `shapes.test.ts` — defect 2 |
| Ports in prose (`Hub (8471)`, `129.146.21.52:443`) were missed | `NAME_PORT` and generalised IPv4 `host:port` harvesting, with machine binding | `shapes.test.ts` — defect 3 |
| 37 bullets dropped as "outside any recognised section" | bullets outside sections now yield services when they carry a link, and status keys become reference text | `shapes.test.ts` — defect 4 |
| `verify` passed vacuously when the parser extracted nothing | two new hard checks: `extraction-nonzero` and `registry-nonzero`; `seed` also refuses to erase a populated registry with an empty parse | `registry.test.ts` — defects 5a, 5b, 5c |

`verify` now runs 12 hard checks. An empty map exits 1 with
`extraction-nonzero FAIL`, which is the control the first build lacked.

## Phase 1 exit criterion

> Every account in the map resolves to a record.

`verify` checks that as a hard gate (its `accounts-resolve` check), alongside nine
other hard checks: schema version, foreign keys, no stored secrets, non-empty
purposes, closed status vocabulary, account uniqueness, and machine/service/port
identity. Soft warnings cover missing credential locations, services without a known host,
and the five intentionally unparsed notes.

Round 4 exact-Markdown result: **PASS** — 12 hard checks, 0 failures. Counts: 10 accounts,
6 machines/devices, 315 services, 3 ports; 5 expected gaps. Empty-map verification:
**FAIL**, exit 1, with `extraction-nonzero` and `registry-nonzero` failing.

## Phase 2 deployment boundary

The target is the ruled Oracle VM (`ubuntu`, Node 24.22.2 ARM64). The supplied
systemd user unit and install guide are instructions only: this environment cannot
reach the VM or its tailnet, so no deployment or real-service probe was attempted.
Health observations remain in separate live fields; checks preserve the imported
`service.status`. The working title remains "Account Orchestrator".

## Layout

```
account-orchestrator/
├── src/
│   ├── cli.ts              query commands plus check and supervised runner
│   ├── health.ts           bounded HTTP/TCP probes, atomic registry updates
│   ├── seed.ts             validates and rebuilds the map snapshot
│   ├── verify.ts           reconciliation + structural checks + Markdown report
│   ├── db.ts               schema migrations, read-only/write opens, paths
│   ├── secrets.ts          credential detection, secret-reference validation
│   ├── ids.ts              deterministic ids (re-seeding is byte-identical)
│   ├── types.ts            the parsed model
│   ├── format.ts           plain-text tables
│   └── parse/
│       ├── markdown.ts     tolerant block scanner with line numbers
│       └── map.ts          map -> entities, with provenance and gaps
├── schema/001_init.sql     Phase 1 data model
├── schema/002_health_checks.sql  additive Phase 2 migration
├── fixtures/               synthetic maps: a contrived one, and one reconstructing
│                           and parser-shape fixtures
├── tests/                  44 tests: parser, health, secrets, end-to-end seed/verify
├── docs/MAP-FORMAT.md      what the parser reads, and what it refuses
├── docs/HEALTH-CHECKS.md   health semantics and Oracle VM install steps
├── systemd/                user-service unit (not deployed)
├── reports/                generated verification reports
└── data/                   the registry (git-ignored)
```

## Tests

```bash
npm test
```

Covers: parser shapes and provenance, credential refusal, migration from Phase 1,
byte-identical re-seeding, verification and empty-map controls, read-only query CLI,
HTTP-to-TCP live checks, HEAD-to-GET fallback, unknown endpoints, and the local
server up → dead down-detection cycle.

## Not in this build

Phase 3 (MCP/A2A/REST front doors and prefetch), Phase 4 (swarms), and Phase 5
(router orchestration) remain out of scope. The Oracle VM unit is provided as an
install artifact only; it has not been installed or run on the VM.
