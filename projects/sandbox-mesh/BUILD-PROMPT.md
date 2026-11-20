# BUILD PROMPT — Sandbox Mesh PHASE 1: One repo, one sandbox (on foundry)

## Role
You are a senior systems engineer. Phase 0 is done and verified. Build
Phase 1 only. Finish it with working code, tests, and a README section
before anything else. Do not stub. Do not skip ahead. Do not start
Phases 2–5.

## Target host — foundry (MEASURED, not guessed)
Everything in this section was measured on the real host on Nov 16, 2026
(see the attached `host-profile-foundry.json` and
`probe-report-foundry.md`). Do not re-probe from your sandbox — you
cannot reach this host. Build for these facts:

- **Host:** Oracle Always Free VM `foundry`, Ubuntu 24.04.5 LTS, kernel
  `7.0.0-1013-oracle`, **aarch64 (ARM64)**, 1 OCPU, 4.10 GiB available,
  32.09 GiB free disk. Tailnet IP `100.83.138.76`.
- **Sandbox driver: Docker, ready.** Docker 29.9.3, runc runtime.
  **No runsc / gVisor.** No Podman, no nsjail, no bubblewrap, no KVM.
  systemd is active. cgroups v2.
- **Concurrency cap: 1.** One sandbox job at a time. Design for it; do
  not assume 3.
- **Fallback user-namespaces FAILED on this host** (unprivileged user
  namespaces are restricted on this kernel). Docker is the only driver.
  If Docker is down, the control plane must refuse to run a repo and say
  so — never silently fall back to a weaker driver.
- **Ports:** control plane **8787** (free on foundry). Sandbox ports
  **8900–8999**. These are the only ports you may use; the probe's
  open-port list shows what is already taken (3000, 3100, 3101, 3105,
  3106, 3306, 4173, 5432, 8000, 8001, …).
- **Phase 0 files already live at `~/sandbox-mesh/` on foundry:**
  `probe.sh`, `scripts/probe.py`, `tests/`, `config.yaml`,
  `host-profile.json`, `probe-report.md`. Build on that structure. Do
  not redesign it. Update `config.yaml`: `phase: 1`, `phase_1: true`,
  phases 2–5 stay `false`.

## How this gets deployed (read this — it shapes the deliverable)
You cannot reach foundry. A second agent (Muse) will copy your zip to
`~/sandbox-mesh/` on foundry, run your install script, and run your
acceptance checks there. So your deliverable must be deployable and
verifiable by someone else, with no access to your environment:

- **One-command idempotent install:** `./install.sh` — installs
  dependencies, verifies Docker is responding (fail loudly if not),
  verifies the probe's host-profile matches (driver `docker`, cap 1),
  writes the systemd unit, starts the service. Running it twice must be
  safe.
- **One-command demo/acceptance:** `./demo.sh` — runs the three sample
  repos end to end (see Acceptance) and prints PASS/FAIL per check.
  Muse will run this on foundry.
- **Everything lives in `~/sandbox-mesh/`.** One `config.yaml`. Never
  touch anything outside it. No commit, no push.

## Stack
- Control plane: **Python** (Phase 0 is Python stdlib — stay consistent).
  SQLite for state. REST + WebSocket API. Small web UI (plain HTML/JS,
  mobile-friendly, phone-first).
- `SandboxDriver` interface; implement the **Docker driver** now. Leave
  a stub for a Firecracker driver (KVM exists on another fleet host,
  not on foundry — future work, not this phase).
- Dependencies: only what the job needs, justified in REPORT.md.
  Prefer stdlib + `httpx` where possible.

## PHASE 1: One repo, one sandbox
Input: a GitHub URL (public; optional token for private repos — token
from an environment variable only, never in a file, never logged).

Flow: paste URL → shallow clone (`depth=1`) → detect → build → run →
reverse-proxy → live logs.

1. **Detection order:** Dockerfile > devcontainer.json >
   docker-compose.yml > package.json / pyproject.toml /
   requirements.txt / go.mod / Cargo.toml. Use Nixpacks or Cloud Native
   Buildpacks for the non-Dockerfile cases. Allow an optional
   `sandbox.yaml` in the repo to override build/run/port.
2. **aarch64 is real:** images must be arm64 or multi-arch. If a repo's
   published image is x86-only, build from its Dockerfile on-arch —
   never substitute an x86 image and never silently skip the repo.
   Your three acceptance repos must all work on arm64; name them in
   REPORT.md.
3. **Speed:** cache clones by commit SHA, cache image layers, keep a
   warm pool of **1** pre-started base container (cap is 1 — a pool of 2
   would exceed the host). Target: cached repo to running, p50 < 3 s,
   p95 < 5 s, measured on foundry and reported in the benchmark table.
4. **Isolation (non-negotiable):** non-root user inside the container,
   read-only root FS where possible, CPU/mem/pid limits, no host mounts,
   egress default-deny with a configurable allowlist, auto-destroy TTL
   (default 30 min). No `--privileged`, no Docker socket inside a
   sandbox, no host network mode.
5. **Access:** reverse proxy gives each sandbox a URL through the
   control plane. Live log streaming over WebSocket. Start/stop/destroy
   from the UI and the API. **Never expose a sandbox port directly.**
6. **UI:** paste URL, press Run, see status
   (cloning/building/running/failed), logs, and an Open button. Runs on
   the tailnet only.
7. **Auth:** the control plane and UI require auth. A bearer token from
   the environment (`SANDBOX_MESH_TOKEN`, set by the owner at install).
   Never hardcode a token, never write one into a file, never log it.
   The UI is tailnet-only and **never public without login**.
8. **Service:** systemd unit, auto-restart, binds `127.0.0.1:8787`
   only. State in SQLite must survive a control-plane restart.

## Reliability contract (Phase 1 items — these are acceptance, not wishes)
1. A sandbox that fails to build leaves **zero** containers, volumes,
   or networks behind. Prove it: the acceptance run counts Docker
   objects before and after a failed build.
2. `install.sh` is idempotent. Running it twice changes nothing the
   second time.
3. `demo.sh` is one command. It either passes end to end or prints
   exactly what failed.
4. State survives a control-plane restart (SQLite). A restart must not
   orphan a running sandbox: on start, the control plane reconciles
   (adopts or destroys) anything Docker reports.
5. Every public operation (run, stop, destroy, logs, proxy) has a test
   that fails when the operation breaks. No untested operation ships.

## Acceptance (must pass on foundry, verified there by Muse)
Run three different sample repos, one at a time, each reachable in the
browser through the proxy:
- a Node repo, a Python repo, and a Dockerfile-based repo (all arm64-safe).
Killing a sandbox leaves no orphan containers, volumes, or networks
(`docker ps -a`, `docker volume ls`, `docker network ls` clean).
Cached repo to running: p50 < 3 s, p95 < 5 s (benchmark table in REPORT.md).

## Safety rules
- Safety beats speed. Ambiguity: choose the safer option, log it in README.
- Never run a repo's code on the host. All repo code runs inside the
  sandbox container only.
- Never trust a repo's `sandbox.yaml` blindly: it may override
  build/run/port, never isolation (no privileged, no host mounts, no
  extra capabilities, no egress allowlist widening).

## Deliverables (zip, same as Phase 0)
1. Working code under `~/sandbox-mesh/` structure (control plane,
   Docker driver, UI, install.sh, demo.sh, systemd unit, tests).
2. Updated `config.yaml` (`phase: 1`, `phase_1: true`).
3. `REPORT.md` at the root: file tree, test results, benchmark table
   (measured where you ran it, and the expected foundry numbers),
   the three acceptance repos named, "Phase 1 done" checklist, and
   anything that could not be verified from your environment (say so
   plainly — Muse verifies it on foundry).
4. `README.md` section for Phase 1: how to install, how to run the
   demo, how the proxy/auth work, the known limits (cap 1, no gVisor,
   aarch64).
