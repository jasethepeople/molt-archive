# Gateway approval-token gate — implementation report

**Status:** Builder and independent verification complete.  
**Source:** `jasethepeople/integration-gateway` at merge commit `acb4c399ad6ba19ee87ba01bb7bbd5b678591791`. Earlier modified worktrees were references only. The zip is a changed-file overlay for this exact base, not a second copy of the untouched 3,402-app registry. The inherited staging tree had 12 unrelated registry-file differences; none were modified or included. The independent run applied only this overlay to a clean checkout of the pinned base.

## What changed

Both REST and A2A execution paths now pass through the approval check in `Router.execute`, before component loading, retry, or app-credential resolution. REST takes the token from `x-approval-token`; A2A takes `approvalToken` from the message data part. A refusal returns `approval_denied`, zero component attempts, and HTTP 403 on REST; A2A returns a failed task. Existing app credentials remain separate from approval, and retries remain below the gate.

The local issuer/verifier is isolated in `tests/support/local-test-approval-issuer.ts` and marked `LOCAL_TEST_ONLY`. It uses ephemeral Ed25519 keys, a 10-minute expiry, caller/target/exact-event binding, single-use consumption, and a signed SHA-256 hash-chained ledger. It is for tests and the loopback demo only; its token format is not a Foundry contract or production issuer.

## Required bypass results

Each test checks the response and an instrumented probe component's run counter—not just an HTTP status.

| Attack | REST | A2A |
|---|---|---|
| No token | Refused; 403; 0 attempts; 0 component runs | Refused; failed task; 0 attempts; 0 component runs |
| Expired token | Refused; 403; 0 attempts; 0 component runs | Refused; failed task; 0 attempts; 0 component runs |
| Replayed token | First use runs once; replay refused; 0 replay attempts; no second run | First use runs once; replay refused; 0 replay attempts; no second run |
| Cross-target replay | Refused before either probe runs | Refused before either probe runs |

Additional gate tests cover changed event payload, a token detached from any signed ledger event, caller mismatch, concurrent double-use (exactly one execution), authority outage, default production refusal, and proving request app credentials are not inspected before approval. A valid call creates signed authorization and completion events in the ledger.

## Verification

- Clean `npm ci --no-audit --no-fund` completed from the pinned lockfile.
- `npm test -- --reporter=dot`: **46/46 passed** across five files (21 approval-gate tests and the existing 25 tests).
- `npm run build`: `tsc --noEmit` passed.
- `PORT=3456 npm run demo:approval-gate`: health and `native/echo` smoke checks passed on `127.0.0.1` only. The persisted report records all eight required REST/A2A bypass checks plus restart invalidation; all nine refusals show zero attempts. The bypass and restart tests separately assert that the instrumented component run counter remains unchanged. A valid token worked before restart; an outstanding token was refused by a fresh server/authority instance afterward. The demo independently verified the pre-restart ledger hash chain and signatures.
- The restart scenario reconstructs the gateway and authority in the same Node process; it does not kill/restart an OS process or service supervisor.
- The existing suite performs one read-only lookup of a public GitHub repository, with a fixture fallback. No GitHub writes, deployment, public service, paid service, or live gateway changes occurred.
- Gitleaks 8.30.1 scanned the staged bundle: 16 generic-key candidates, all redacted and all traced to the demo ledger’s `LOCAL_TEST_ONLY` issuer marker or generated execution UUIDs. No token or private key is serialized; the scan summary is in `evidence/secret-scan.md`.

Final test/build/demo logs and JSONL ledger evidence are in `evidence/`. The zip also contains the demo JSON report.

## Changed source files

`README.md`, `docs/A2A.md`, `package.json`, `scripts/approval-gate-demo.ts` (new), `src/a2a/agent.ts`, `src/approval.ts` (new), `src/router.ts`, `src/server.ts`, `src/types.ts`, `tests/a2a.test.ts`, `tests/approval.test.ts` (new), `tests/e2e.test.ts`, `tests/helpers.ts`, `tests/router.test.ts`, and `tests/support/local-test-approval-issuer.ts` (new). No runtime dependency or lockfile change was needed.

## Open work: Foundry integration

Production remains fail-closed. The next phase must provide a trusted caller resolver and an `ApprovalAuthority` backed by Foundry's real trusted key and authoritative ledger, with atomic single-use consumption and fail-closed behavior on verifier/ledger errors. It must use Foundry's frozen token/ledger contract; the local test format and in-memory test state are not interoperable. No live wiring or deployment was attempted.

## Independent archive verification

A separate verifier extracted the zip, applied `source/` to a fresh checkout at the pinned commit, and installed from the lockfile. Its clean rerun passed: **46/46 tests**, `tsc --noEmit`, and a Gitleaks scan with the same 16 classified UUID/`LOCAL_TEST_ONLY` candidates. The independent logs are included in `evidence/test-results/`; its scanner summary is `evidence/independent-gitleaks.log`. The final re-packed archive is integrity-checked and rescanned after adding this evidence.
