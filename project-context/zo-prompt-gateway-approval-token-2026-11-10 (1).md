# Prompt for Zo — gateway approval-token gate: refuse calls without a valid approval token

Paste this into a NEW Zo chat, with the files listed at the bottom attached in the same chat.

## The problem (read this first)

Today, nothing stops an agent from not logging. The gateway executes any well-formed ExecuteRequest. Per-caller permissions: NONE — Foundry-side grants are the only barrier, and they live outside the gateway. This round closes that hole at the gateway itself.

## The design (already decided — implement it, do not redesign it)

The gateway refuses a call unless it carries a valid, unexpired approval token chained to a ledger event. Fail closed at entry: no token, expired token, token not chained to a ledger event, or an already-used token → refuse BEFORE any component runs and BEFORE any app credential is used.

Token properties:

- Single-use. Once consumed, a second call with the same token is refused.
- Short TTL: 10 minutes, matching Foundry's frozen single-use approvals.
- Bound to caller + target + ledger event: a token issued for one target or event cannot be replayed against another.
- Chained to a ledger event: the token must reference a real ledger event (event id + hash) that authorizes this call. A token with no ledger event behind it is invalid.

Fail-closed rules:

- Restart or partition = FAIL CLOSED. No outstanding approval survives a restart. On restart, all outstanding approvals are void until re-issued. Do not cache approvals across restarts.
- A refused call is never retried into success. Retries happen below the ledger (attempts:1 means a single try, omitted means 3) — token consumption must be atomic with the first attempt, so a retry cannot spend a token twice or slip past the gate.
- The approval token is ADDITIONAL to app credentials, not a replacement. A call needs both valid app credentials AND a valid approval token.

Scope note: tokens are issued by the Foundry-side approval flow (frozen single-use approvals already exist in Foundry Phase 5). This round is the VERIFY side in the gateway. Build a test issuer (local, for tests and the demo only, clearly marked as such) so the gate is provable standalone. Wiring the real Foundry issuer into the gateway is the follow-up round — say in the report what that flip needs.

## Where the gate goes (grounded in the code — read it before building)

- Clone the gateway: https://github.com/jasethepeople/integration-gateway at merge commit acb4c39. That is the permanent home (REST API intact, A2A front door merged, registry 3,402 apps / 14,683 components).
- Both entry points funnel through one function: POST /api/v1/execute calls `router.execute` (src/server.ts, around line 126-142), and the A2A agent delegates to the same `router.execute` (src/server.ts line 42). Put the gate at the entry of `Router.execute` (src/router.ts, around line 129) so it covers REST AND A2A. A bypass through either path must be impossible — test both.
- Current auth (src/router.ts, around lines 68-115): env-derived auth (`APP_<APPNAME>_*` keys) merged with per-request auth, per-request winning. The gate sits alongside that auth, not instead of it.
- `server.listen` binds ALL interfaces despite a log line claiming localhost (src/server.ts:192). Do not change the bind in this round — but the gate must not depend on loopback to be safe.
- Existing bar: tests 25/25 (`vitest run`), build `tsc --noEmit`. Both must still pass.

## Four bypass tests (all must fail closed — required)

1. No token at all → refused, component never runs, no credential touched.
2. Expired token (past the 10-minute TTL) → refused.
3. Replayed token (same token used twice) → first call proceeds, second is refused.
4. Cross-target replay (token issued for target/event A, presented for target/event B) → refused.

Each test must prove the component did NOT execute. A test that only checks the status code proves nothing.

## Demo — prove it works

- A call with a valid token executes and lands in the ledger as a signed event.
- All four bypass tests fail closed, on BOTH entry points (REST and A2A).
- A restart voids outstanding approvals (show a token valid before restart being refused after).
- Loopback smoke run (the Job 1 pattern: PORT=3456, /health ok, native/echo ok). Nothing exposed.

## Hard rules

- No pushes to GitHub. No deploys — the gateway's live copy on the owner's Oracle VM is NOT yours to touch; work on a clone, deliver a zip. No secrets in any file. No paid services.
- Do not redesign the token. If the design as written cannot be implemented as specified, stop and report exactly where it breaks — do not silently weaken it.
- Follow the attached backend-prime skill for how to build, test, and report.

## Deliverables

A zip with every file (the changed gateway tree, new tests, demo output), plus a written report: what you built, per-test results (including all four bypass tests on both entry points, and the restart test), what is still open (the Foundry issuer wiring), and the next phase. Stop and report when done.

## Files to attach in the chat

- backend-prime SKILL.md (the process — follow it)
- Zo clones the repo itself: https://github.com/jasethepeople/integration-gateway at acb4c39
