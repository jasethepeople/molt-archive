# Sandbox Mesh Phase 1 — independent verification (Tue Nov 17, 2026)

Verifier: Muse. Zo's zip received 07:41 MST as
`~/workspace/user/files/sandbox-mesh-phase1_3_wp2l.zip` (103,369 B,
sha256 `e184d2bbbf6a3e87f87eeea6bd9d48451f225e843631b14e05aa9aad76770387`),
extracted to `~/workspace/sandbox-mesh-phase1-verify/`.

## Checks on the deliverable itself (this Muse VM)
- Unit tests: `python3 -m pytest tests/ -q` → **53 passed, 15 subtests
  passed** — matches Zo's claim of 53. OK.
- BUILD-PROMPT.md is **byte-identical** to the prompt handed to Jason
  (sha256 `ac6876ba…`). OK.
- `config.yaml`: phase 1, foundry, linux/arm64, concurrency 1, control
  plane 127.0.0.1:8787, sandbox ports 8900–8999, default-deny egress,
  buildx 0.39.0 + nixpacks 1.41.0 pinned with sha256, phases 2–5 off. OK.
- Code read: `install.sh`, `demo.sh`, `mesh/server.py` (Bearer auth,
  CSRF on mutations, binds 127.0.0.1:8787), `mesh/docker_driver.py`
  (egress proxy + app containers: non-root uid 65532/65534, --read-only,
  --cap-drop ALL, no-new-privileges, loopback-only ports),
  `mesh/tailscale.py` (refuses Funnel, refuses to touch conflicting
  routes, verifies after adding). All as specified.
- `host-profile-foundry.json` in the zip differs from my measured probe
  by ONE line: `driver_rationale` reworded (Zo's version is actually
  more accurate for foundry — no runsc/KVM). Substantive fields
  identical. Noted, not a finding.
- REPORT.md is honest: Zo states Foundry deployment/acceptance were
  unverified in its sandbox and lists the open checklist. The 53-test
  claim re-verified here.

## Install on foundry — two real bugs found and fixed here
Foundry: up 9d 16h, 33G free, Docker 29.9.3 aarch64 cgroups v2 runc,
~/sandbox-mesh present from Phase 0, no Serve config, 8787 free.

**Bug 1 — buildx driver-opt comma split (install run 1, exit 1).**
`--driver-opt env.NO_PROXY=localhost,127.0.0.1,::1,mesh-egress`:
buildx parses driver-opts as a CSV record, so the commas split the
value and `127.0.0.1` arrived as a bare token → `ERROR: invalid value
"127.0.0.1", expecting k=v`, builder never created. First fix attempt
(double-quoting just the value) failed with `bare " in
non-quoted-field`. Working fix: quote the WHOLE field —
`--driver-opt '"env.NO_PROXY=localhost,127.0.0.1,::1,mesh-egress"'`
(verified with a throwaway builder create, then removed). Applied to
both NO_PROXY and no_proxy lines. Builder then created fine.

**Bug 2 — limits check can never pass + --cpus update conflict
(install run 2, exit 1).** The builder is created with
cpu-period=100000 / cpu-quota=70000, but install.sh verified
`Memory|NanoCpus|PidsLimit` against `1610612736|700000000|128`.
Measured on the real container: `Memory=1610612736 NanoCpus=0
CpuPeriod=100000 CpuQuota=70000 PidsLimit=<unset>`. Docker reports
NanoCpus=0 when period/quota are set directly, and buildx sets no
pids limit at create — so the check could never match, and the
fallback `docker update --cpus 0.7` then failed with `Conflicting
options: Nano CPUs cannot be updated as CPU Period has already been
set`. Fix: verify
`Memory|CpuPeriod|CpuQuota|PidsLimit` against
`1610612736|100000|70000|128`, and update with `--cpu-period 100000
--cpu-quota 70000 --pids-limit 128` (no --cpus). Install run 3 in
progress with this fix.

Both patches are in the foundry copy (`install.sh`, with
`install.sh.bak` = the pre-patch original). They must go back into
Zo's tree — the zip as delivered cannot install.

## Open / not yet verified
- Install run 3 outcome; install idempotence (second run, no changes);
  demo.sh (Node + Python + Dockerfile repos through the proxy,
  failed-build object counts, cached-start p50/p95); Tailscale Serve
  private + no Funnel; /api/health from the tailnet.
- **Privileged BuildKit helper (Jason's call, not blocking the demo):**
  the `docker-container` buildx driver runs buildkitd in a privileged
  helper container (standard for that driver). App + proxy containers
  are non-privileged. The Phase 1 prompt's no-privileged rule governs
  sandboxes; the helper is build infrastructure, same trust domain as
  the Docker daemon. Running arbitrary repos through it is the decision
  owed — the three pinned demo repos are not arbitrary.
- Bearer token: generated on foundry per install run
  (`openssl rand -hex 24`); the successful run's token is imported
  into the systemd user environment, never written to a file. Jason
  gets it once in chat when the service is up.

## Install run 3 — two more bugs (both found by measurement, not guesswork)
**Bug 3 — same --cpus flaw, second copy, in mesh/docker_driver.py.**
`_ensure_buildkit` AND `_isolate_buildkit_networks` both compare
`(Memory, NanoCpus, PidsLimit)` to `(memory, round(cores*1e9), pids)`
and "fix" with `docker update --cpus`. Docker reports NanoCpus=0
when period/quota are set, so the check can never pass and the update
always fails with the NanoCpus/CPU-Period conflict — the server then
crashes at startup in `startup_reconcile`. Fixed both sites the same
way as install.sh: compare `(Memory, CpuPeriod, CpuQuota, PidsLimit)`
to `(memory, 100000, round(cores*100000), pids)`, update with
`--cpu-period/--cpu-quota`. All 53 tests still pass after the patch.
Manual server run then passed health
(`{"status":"ok","host":"foundry","driver":"docker","concurrency_cap":1}`)
and unauthenticated /api/health returned 401. Builder limits verified
on the real container: Memory=1610612736 CpuPeriod=100000
CpuQuota=70000 PidsLimit=128. `--cpus` at container CREATE time
(docker_driver lines 283/489/687) is fine and untouched.

**Bug 4 — systemd unit cannot start as a user service (two causes).**
(a) The user manager (PID 26221, started Nov 9 22:37) predates ubuntu's
docker-group membership (group 988): its Groups are `4 24 27 30 104
1001`, no 988. So any service under it gets `permission denied` on
/var/run/docker.sock (root:docker 660), while fresh SSH logins (with
988) work. Ten user services run under that manager (Streetbot,
Foundry, gateway, memorial-api, network-atlas, orchestrator ×3 +
health, dbus). Restarting the manager (loginctl terminate-user) would
briefly stop all of them — NOT done; needs Jason's go on timing.
Linger=yes, so a fresh manager respawns on next login and enabled
services restart.
(b) The unit's full hardening set fails at exec with 218/CAPABILITIES
("Failed to drop capabilities"). Bisect so far: minimal unit and
variant B (NoNewPrivileges + PrivateTmp + ProtectSystem=strict +
ReadWritePaths) both reach Python; the full set 218s. Offender is one
of ProtectKernelTunables / ProtectKernelModules / ProtectControlGroups
/ LockPersonality / RestrictSUIDSGID — pinning it down after the demo.
Variant B is the shippable unit for now.

## Demo — running (launched ~07:56 MST)
Manual server (nohup, token from systemd env) + ./demo.sh in
background on foundry. Demo = 50 unit tests, Node/Python/Dockerfile
repos through the proxy, failing-build object counts, 5 cached runs
(p50/p95). Output: ~/sandbox-mesh/logs/demo-run-2026-11-17.log on
foundry. Still to verify after demo: install.sh idempotence (blocked
on bug 4a — manager restart), Tailscale Serve private + no Funnel,
/api/health from the tailnet, final REPORT.md benchmark row.

## Demo run 1 — FAILED on a real code bug (bug 5)
demo.sh: 50 tests OK on foundry, then the Node repo failed with
`DemoError: unexpected control-plane failure (TypeError)`. The
orchestrator's generic catch recorded only the type name — no
traceback anywhere — so I patched orchestrator.py to log the
traceback into the job log (53 tests still pass; this is a genuine
diagnosability fix, keep it) and re-ran the Node repo. Root cause:
**`self.build` name collision in mesh/docker_driver.py.** Line 27
sets `self.build = config["build"]` (a dict), which shadows the
`def build(...)` method at line 413 — so `driver.build(...)` raised
`TypeError: 'dict' object is not callable`. The build path could never
have worked; the unit tests pass only because their stub drivers
define their own `build` methods. Fix: renamed the attribute to
`self.build_config` (9 occurrences, method untouched). 53 tests still
pass here. Demo run 2 launched with the fix.
Note: the failed jobs left NO orphan containers/networks/volumes
(verified after both failures) — the failure cleanup works.

## Bug 6 — edge-proxy startup race (the demo's real blocker)
Demo run 2 got past bug 5 and failed in the Node build: BuildKit could
not reach the egress proxy (`dial tcp 172.19.0.1:8765: connection
refused`). Measured on foundry:
- The proxy container's IP on the per-job internal network really is
  .1 (an `--internal` + `gateway_mode_ipv4=isolated` network has no
  gateway, so the first container gets .1) — the address was right.
- The bundle (egress 8765 + ingress 8081, one process) binds at
  ~20 s under load on this 1-OCPU host (0.1-CPU cap), not ~2 s.
  A bare TCP connect to the published port proves nothing (Docker's
  userland proxy accepts with nothing behind it), and a 2 s HTTP
  timeout never completed even while the proxy was serving and logging
  502s — a request round-trip through the throttled proxy takes
  several seconds under load.
- Fix (`_wait_proxy_ready` in prepare_network, after the Running
  check): poll the published ingress port with a real HTTP request
  (any HTTP response proves the bundle serves; the bundle binds
  egress before ingress in the same expression, so ingress serving
  implies egress is bound). Per-attempt timeout 10 s, overall deadline
  120 s, fail closed with cleanup. Two unit tests (one with a real
  HTTP server, one fail-closed). Suite is now 55 tests.
- The first version of this fix used a bare TCP connect (fooled by the
  userland proxy) and a 2 s HTTP timeout (too short under load) — both
  measured wrong on the live host, both corrected by measurement.
- VERIFIED on foundry: Node repro #3 (job `2c8f72a31cc2`) reached
  `building` at t=26 s — the first run past the readiness gate. (It then
  failed for bug 7's reason, below — not the proxy.)

## Bug 7 — nixpacks build context had no repo files (Node `npm ci`: no package-lock.json)
Repro #3 got past bug 6 and failed at stage 6/8: `npm ci` errored "can
only install with an existing package-lock.json". Measured root cause:
`_build_nixpacks` passed the nixpacks `--out` dir as the buildx context.
The generated Dockerfile COPYs BOTH `.nixpacks/*.nix` (from --out) and
`.` (the repo: package-lock.json, index.js). With context=out, `COPY .`
copied only generated files; with context=repo, the `.nix` COPY would
fail. The repo DOES have package-lock.json at d33649d5 (verified by
`git ls-tree`) — the file simply never reached the build.
Fix (docker_driver.py): stage a per-job context in the existing temp
dir — copy the clone excluding `.git`, overlay the generated `.nixpacks`
dir, build from staging. The clone itself is never modified (it is
shared across jobs through the clone cache). New unit test asserts the
staged context holds repo files + the generated `.nix`, excludes `.git`,
and is not the repo. Suite is now 56 tests.
Failure cleanup re-verified after this failure: no job
containers/networks/volumes remained (only the warm pool + builder, as
expected). Re-test: job `79e1e033544a` on the restarted server
(pid 381999) with the fix; repro saved at `scripts/repro_node.py`
(polls an existing job via REPRO_JOB_ID, prints status transitions +
proxy fetch).
- VERIFIED on foundry (job `79e1e033544a`, t=360 s): `npm ci` added 380
  packages in 2 m, all 8 stages DONE, image exported
  (sha256:29f44b84…) and imported — the staged context is correct. The
  job then failed for bug 8's reason, below — not the build. The Phase 2
  zip's `phase1-fixed-tree/` still carries the pre-bug-7 tree and MUST
  be refreshed once the fixes are verified on foundry.

## Bug 8 — app container `create` used log opts the daemon rejects (local driver + max-file=1)
After the image imported, `docker start` of the app container failed:
`failed to initialize logging driver: compression cannot be enabled
when max file count is 1`. Measured: `start()` passes
`--log-driver local --log-opt max-size=10m --log-opt max-file=1`; the
local driver compresses by default, and the daemon rejects compression
with max-file=1. Reproduced on foundry with a trivial container:
max-file=1 alone fails, max-file=1 + compress=false passes,
max-file=3 passes. Fix: add `--log-opt compress=false`, keeping the
single 10 MB file (the original disk bound, unchanged). Existing
`start()` test now asserts both opts. The app image for d33649d5 is
cached on foundry, so the re-test skips the ~6 min build. Re-test: job
`500e8c26ed9b` on the restarted server (pid 393196).
- VERIFIED on foundry (job `500e8c26ed9b`): queued → starting at t=30 s
  → **running at t=61 s**; proxy fetch **HTTP 200, 10,069 bytes** — the
  real Heroku Node getting-started page through the mesh proxy. Bug 8
  fixed. (The first fetch attempt failed only because the repro passed
  the relative `proxy_url` path straight to urlopen — a repro-script
  bug, fixed in `scripts/repro_node.py` to prepend the control-plane
  base and reuse the authed session.) Job destroyed cleanly afterwards:
  status `destroyed`, zero orphans.
- Full `./demo.sh` launched on foundry after the Node pass (pid 396887,
  log /tmp/demo-phase1.log): unit tests + the 3 repos + benchmark.
- Demo run 1 result: **Node exercise 7/7 PASS** (clone/detect/build/start,
  proxy HTTP, live logs, stop, restart, proxy after restart, destroy).
  Python failed — bugs 9 + 10 below. Demo stdout is block-buffered when
  redirected (unittest goes to stderr, so it shows immediately); watch
  progress via the API job list, not the log tail.

## Bug 9 — build timeout 900 s too short for the Python cold build on 1 OCPU
The Python job (`3b86139a53ad`) was killed at the 900 s driver timeout
during image export — after nix-env (~511 s) and pip install (78 s,
Django etc. through the egress proxy at 6 MB/s) had both completed, i.e.
~95% done. The Node cold build took ~10 min; Python needs ~16–18 min on
this host. Fix: `build_timeout_seconds` 900 → 1800 in config.yaml
(validation allows 60–3600), and the demo's `wait_for_status` default
1200 → 1800 so the demo waits at least as long as the driver allows.
No test pinned either value. The retry resumes from BuildKit's cached
layers (nix + pip layers completed), so it should finish well inside
the new timeout.

## Bug 10 — demo crashed on the first repo failure (+ a misleading PASS)
`exercise_repository` raised DemoError when the sandbox status was not
`running`, and the raise propagated out of `main()` — killing the demo
before the Dockerfile repo, failure cleanup, and benchmark. Worse,
`record()` had already printed `PASS Python: GitHub clone, detect,
ARM64 build, and start` because it only catches exceptions, while
`create_repository` returned the failed item normally. Fix (demo.py):
`create_repository` now raises DemoError on a non-running status, so
`record()` prints a FAIL line and the demo continues; the post-create
checks in `exercise_repository` catch DemoError and record it instead
of crashing. Three new tests in `tests/test_phase1_demo.py` (failed
status raises; failed build is recorded with return None; build-type
mismatch is recorded, destroy still runs). Suite is now 59 tests.
- Demo re-run launched with the fixes (pid 425046, log
  /tmp/demo-phase1b.log) on the restarted server (pid 424803, 1800 s
  timeout).
- Demo run 2 verdict: **DEMO FAIL, 6 checks failed** (full output flushed
  at exit). Node exercise 7/7 PASS again. Python built but the app died
  (bugs 11 + 12). Dockerfile build failed (bug 13). Failure-cleanup
  counts changed 1 → 2 containers (bug 14). Benchmark: **n=4, p50
  36.057 s, p95 38.123 s vs target p50 < 3 s, p95 < 5 s** — see the
  benchmark note below. (The 3 stray lines at the top of the demo log —
  PASS Python + FAIL detected 'dockerfile' + FAIL 900 s timeout — are
  the new `test_phase1_demo.py` unit tests printing during the unittest
  phase, not demo output.)
- Demo run 3 launched with bugs 11–15 fixed (pid 458545, log
  /tmp/demo-phase1c.log) on the restarted server (pid 458300).

## Bug 11 — nixpacks PATH never reaches the non-root app user (Python `gunicorn: command not found`, exit 127)
The Python image exported and the app container started, then exited
127: `/bin/bash: line 1: gunicorn: command not found` (reproduced
manually with the cached image). Root cause: nixpacks puts provider
binaries in NIXPACKS_PATH (`/opt/venv/bin:` for Python) and merges it
into PATH only via `/root/.profile`, which a non-root login shell never
reads. The image's own PATH is the nix-profile path only. Node worked
by luck: `node` lives in the nix profile. Fix (`start()`, buildpack
plans only, so Dockerfile/image PATHs are untouched): inspect the
image's Env, merge NIXPACKS_PATH into PATH, pass it as `--env`. New
unit test asserts the merged PATH in the create argv; the existing
dockerfile start test now asserts no PATH env is added there.

## Bug 12 — health gate counted a 502 as healthy (dead app reported "running")
`_wait_healthy` returned on ANY HTTP response, including HTTPError —
so when the app was dead, the ingress proxy's 502 counted as healthy
and the job went "running" with "Sandbox is ready" in the logs. That
masked bug 11 and caused the benchmark sample-5 502. Fix: only a
gateway error (502/503/504, which the ingress returns when the app is
down) keeps the gate waiting; any other status — including the app's
own 500 — is the app itself answering. Three unit tests (502 then 200
passes; 502-only fails after the deadline; the app's own 500 passes).
- Refinement, found in run 3: the first version used "any non-5xx",
  which made the Python job fail the gate even though gunicorn was
  serving — the app returns 500 on `/` (see the Python 500 note below).
  The gateway-error distinction is the correct one: a 500 is the app
  answering, a 502 is the proxy saying the app is not there.

## Python 500 note — the app runs but its homepage 500s (nixpacks does not run Django's collectstatic)
In run 3 the Python job reached gunicorn (bug 11 fixed) but Django
returns 500 on `/`: `ValueError: Missing staticfiles manifest entry
for 'lang-logo.png'`. Heroku's Python buildpack runs `collectstatic`
at build time; nixpacks does not, so the manifest was never built.
This is an app/build difference, not a mesh failure: the mesh runs the
repo as declared, and the app answers through the proxy (with its
error page). A build-time collectstatic step for Django apps would
need Dockerfile surgery or a runtime write (the container is
read-only) — follow-up scope, not Phase 1. Reported as a known
limitation in the final report; the demo's proxy check (non-empty
body) passes it.

## Bug 13 — egress allowlist typo: `production.cloudflare.docker.com` (does not exist)
The whoami Dockerfile build failed pulling `golang:1-alpine`:
CloudFront blob fetch Forbidden. The allowlist already had
`registry-1.docker.io` and `auth.docker.io`, but the blob-CDN entry
was misspelled `cloudflare` for `cloudfront`. Direct `docker pull`
on foundry worked, and the signed URL was not expired (Expires
17:06 UTC, build at 16:16) — the proxy was the blocker. Fix: one-word
typo correction in config.yaml. (whoami also needs proxy.golang.org
and dl-cdn.alpinelinux.org, both already listed.)

## Bug 14 — failure-cleanup snapshot raced the monitor's async cleanup
The "after" snapshot is taken the instant the fixture reports failed,
but the monitor removes the fixture's edge container/network on its
own pass — so the counts differed (1 → 2 containers) with no real
leak (`docker ps -a` afterwards showed no leftovers). Fix (demo.py):
poll the snapshot until it matches the baseline (up to 10 s) before
comparing. New unit test with a scripted driver.

## Bug 15 — destroy raced the monitor's cleanup and failed opaquely
The Python job's destroy returned RemoteDisconnected: the server
traceback showed `driver.destroy` → `_cleanup` →
"one or more sandbox-owned Docker resources could not be removed".
The monitor's `reap_expired` destroys failed jobs' leftovers on its
own pass and swallows errors, while the API `destroy()` raised — two
concurrent cleanups of the same resources, one losing transiently.
Fixes: (a) `_cleanup` now names the failing resource in its error
instead of swallowing it; (b) API `destroy()` retries once after 1 s
on DriverError (still surfacing a persistent failure, and still
marking a running job failed on failure).

## Benchmark note — cached commit to running: p50 36 s vs target p50 < 3 s
Measured on run 2 (n=4, the 5th sample failed on bug 12's 502):
p50 36.057 s, p95 38.123 s. Breakdown: the edge proxy (0.1-CPU cap)
takes ~20 s to bind under load, then container start + health check.
The p50 < 3 s / p95 < 5 s target cannot be met on foundry as built —
the proxy bind alone exceeds it. NOT changed (spec target + resource
policy are Jason's call). Options for his ruling: recalibrate the
target for foundry, raise the proxy CPU cap, or accept the measured
timing. The warm pool does not help here (it pre-warms the app image,
not the per-job proxy).

## Demo run 3 — verdict: DEMO FAIL, 2 checks (both expected)
Log /tmp/demo-phase1c.log on foundry. Node exercise 7/7 PASS. Dockerfile
(whoami) 4/4 PASS — bug 13's cloudfront allowlist fix verified in practice
(golang:1-alpine pulled through the proxy, go build, running). Failure-cleanup
fixture all PASS — bug 14's snapshot-poll fix verified. Python exercise
FAILED at the health-check deadline: expected, because run 3 ran on the
PRE-refinement gate (any non-5xx counts as healthy) and the app returns 500
on `/` (Python 500 note). Benchmark: n=5, p50 40.334 s, p95 44.438 s vs
target p50 < 3 s / p95 < 5 s — target miss stands, Jason's ruling owed.
So run 3's two failures are: (1) the gate version that was superseded by
the refinement, (2) the benchmark target.

## Bug-12 refinement deployed to foundry (demo run 4)
The refined gate (only 502/503/504 keep waiting; any other status,
including the app's own 500, counts as healthy) was copied to
~/sandbox-mesh/mesh/orchestrator.py on foundry (md5
dab2553e77feb2bcb39b23a389053213). Note: `pkill -f "mesh.server
--project-root"` self-matches the ssh command line that carries it and
kills the session — use the bracket form `mesh[.]server` or check with
`ps` first. The old server (pid 458300) was stopped by that pkill;
new server started pid 492781 with the refined code, /api/health
responding (auth required, as expected without a Bearer). Demo run 4
launched pid 495224, log /tmp/demo-phase1d.log — all three images cached,
so it should be fast. Expected run-4 result: everything passes except
the benchmark speed target.

## Demo run 4 — verdict: DEMO FAIL, 2 checks (mesh verified; both failures are Jason's calls)
Log /tmp/demo-phase1d.log on foundry, server pid 492781 with the refined
gate. Node 7/7 PASS. Dockerfile (whoami) 4/4 PASS. Failure fixture 6/6
PASS. Benchmark 5/5 samples PASS, p50 36.884 s / p95 37.519 s vs target
p50 < 3 s / p95 < 5 s — target miss stands, ruling owed.
- **Python: build + start PASS** — the refined bug-12 gate is verified on
  foundry: the job reached `running` even though the app returns 500 on
  `/`. Logs PASS, destroy PASS.
- **Python: reverse-proxy HTTP response FAIL** — `DemoError: reverse
  proxy returned HTTP 500`. This is the Python 500 limitation in its
  last hiding place, NOT a mesh bug: demo.py `proxy()` (line 84) raises
  on ANY HTTPError, so the app's own 500 fails the check even though the
  app is serving its error page through the proxy. The gate's job is
  "is the sandbox alive" (a 500 proves it is); the proxy check's job is
  "does the app load" (a 500 means it does not). Do NOT relax the proxy
  check unilaterally — that would report an error page as a loaded app.
  The fix is Jason's call: (a) a build-time `collectstatic` step for
  Django apps (Dockerfile surgery in the nixpacks path), (b) accept the
  error page as "loads" (change the demo), or (c) swap the Python demo
  repo. The mesh did everything right: build, start, gate, logs,
  destroy all pass.
- **Zero orphans verified after run 4:** `docker ps -a` shows only
  sandbox-mesh-warm + buildkit builder (+ the unrelated Supabase stack);
  no sandbox networks; no sandbox volumes (only the buildkit _state
  volume, expected).
- Run-4 note: the demo log stays 0 lines until the buffer flushes near
  the end (stdout is block-buffered when redirected). Progress is visible
  via `docker ps` (edge proxy containers appear per job) and the server
  log, not the demo log tail.

## Tailscale Serve check — VERIFIED (private route live, no Funnel)
`tailscale serve status` on foundry initially showed "No serve config":
install.sh's `mesh.tailscale` step had failed silently at install time
because serve config changes need operator rights ("Access denied: serve
config denied"). Fixed with one command on foundry:
`sudo tailscale set --operator=ubuntu` (passwordless sudo works), then
`python3 -m mesh.tailscale` → "Tailscale Serve is configured privately
on HTTPS port 8787." Serve status now:
`https://foundry.tail722c53.ts.net:8787 (tailnet only)` → proxy
`http://127.0.0.1:8787`. Funnel status shows the same route marked
tailnet-only — no public exposure. /api/health fetched through the
tailnet URL returns HTTP 401 `{"error":"authentication required"}` —
verified both from foundry itself and from this Muse VM via the egress
proxy. Finding for Zo's tree (not a code change here): install.sh warns
when the serve step fails but does not surface the one-line fix
(`sudo tailscale set --operator=$USER`); the next prompt should have
install.sh print that exact command on failure, or attempt it when
sudo is available.
