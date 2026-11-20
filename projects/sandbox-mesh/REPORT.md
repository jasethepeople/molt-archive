# Sandbox Mesh — Phase 2 Report

**Status:** Phase 2 implementation and local automated checks pass. The Foundry install and live Docker/BuildKit acceptance remain unverified; this is not a deployment sign-off.

## Built

- Persistent SQLite FIFO queue with `queued` state, queue positions, cancellation, restart requeueing, and automatic scheduling when capacity is released.
- Per-host concurrency derived from the configured cap, measured host profile, effective CPU and available memory. Foundry's configured cap is 1; the current Zo sandbox derives a cap of 2. The global host guard holds new work when load or available memory is over its configured limit, fails closed when measurements are missing, logs decisions, and does not kill running jobs.
- A SHA-256-pinned static ARM64 supervisor baked into each app image as PID 1. It runs the app and a sibling heartbeat-only agent as the configured non-root UID, forwards stop signals, and tears down the sibling when either child exits. The agent gets only a per-job token and task; the application is explicitly stripped of those agent variables.
- Authenticated API and responsive card UI for scheduler health, queue position/reason, agent heartbeat, CPU/memory/PID metrics, live logs, cancellation, and lifecycle actions; authenticated managed-resource counts and restricted `crash-app`/`kill-agent` test actions verify sibling teardown and cleanup.
- Three-repository acceptance routine verifies app-crash cleanup, that other running sandboxes remain healthy, and—at host cap 1—that queued work advances after cleanup.

## Semantics and limits

Jobs persist as `queued` and are claimed FIFO only when both configured capacity and the host guard allow a start. Cancelling a queued job creates no Docker resources. Queue state and reasons survive service restarts. Foundry remains capped at 1; the local Zo sandbox derives a cap of 2 from its current measured resources. The configured default remains 3 and runtime measurements can reduce it.

The agent emits `agent-heartbeat <job-id> <unix-ts>` every 5 seconds. If a running container remains up but has no heartbeat for 15 seconds, its agent state becomes `silent`; the app is not restarted. If either child exits, the supervisor stops the sibling and exits, after which the control plane marks that sandbox failed and attempts to remove its container, egress helper, and network. Failed cleanup retains resource identifiers and its capacity reservation for retry. The host guard only gates new starts.

The agent has no Phase 3 task execution or agent-to-agent communication. The BuildKit helper remains Docker Buildx's privileged `docker-container` builder; its resource, network, mount, and socket constraints and the remaining isolation caveat are documented in `README.md`. No gVisor or VM-grade isolation is claimed.

## Verification run

- `python3 -m py_compile mesh/*.py scripts/*.py tests/*.py` — passed.
- `python3 -m unittest discover -s tests -q` — **87 tests passed**.
- `GO111MODULE=off go test -race ./mesh/supervisor` — passed, including heartbeat cadence, app/agent sibling teardown, and agent-environment isolation.
- `python3 scripts/build_supervisor.py --arch arm64` — passed; produced a static Linux ARM64 ELF and synchronized its SHA-256 to `config.yaml`.
- `bash -n install.sh demo.sh` — passed; the release runner is executable and the installer prints the exact Tailscale operator-permission repair command.
- `python3 -m mesh.server --project-root . --check-config` — passed.
- Dashboard inline JavaScript extracted from `mesh/web/index.html` and checked with `node --check` — passed.
- Runtime and acceptance-helper tests — passed for offline wrapper-image build arguments, pinned supervisor, constrained container startup, per-job credentials, process-kill control, authenticated crash/resource API routes, FIFO progress after app crash, managed-resource cleanup assertions, and dashboard behavior.
- Foundry profile check — concurrency cap 1. Local profile check — derived concurrency cap 2.

## Not verified

Docker and Tailscale are not installed in this Zo sandbox (`docker: command not found`; `tailscale: command not found`). I could not run the Foundry installer, build or start real app containers, validate Buildx flags or cleanup against Docker, measure cached-start timing, inspect Tailscale Serve/Funnel state, or run the three real GitHub repositories and crash acceptance. `demo.sh` contains those live checks and must be run on the configured Foundry host before deployment is considered accepted. No deployment or public-exposure change was made here.
