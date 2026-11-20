# Sandbox Mesh — Phase 2

Sandbox Mesh accepts public or private GitHub repositories and runs their build and app processes in isolated ARM64 Docker sandboxes. The control plane is Python/SQLite; Docker Buildx and Nixpacks build images; a loopback-only authenticated proxy exposes app HTTP. Phase 2 adds a durable FIFO queue, per-host scheduling, a global host-capacity guard, and a heartbeat agent supervised inside each sandbox. It does not add agent task execution or agent-to-agent networking.

## Scheduling and agent health

The configured default scheduler cap is 3. Runtime capacity is derived from the host profile, measured CPU and available memory, configured limits, and any host-specific cap; Foundry is explicitly capped at 1. The current Zo sandbox profile derives a cap of 2. The warm pool remains one container. Queued work is persistent and FIFO; a queued job can be cancelled, and the scheduler starts the next job when a slot is released.

Before a start, the host guard reads `/proc/loadavg` and `/proc/meminfo`. Its thresholds are in `config.yaml`; it holds queued work when host load or available memory is too high, fails closed if measurements are unavailable, and never kills a running job. Guard decisions are logged.

Every app container starts the bundled, SHA-256-pinned ARM64 supervisor as PID 1. It runs the app and a small sibling heartbeat process as the configured non-root UID. The agent receives only a per-job token and a fixed heartbeat task; those credentials are filtered out of the app environment. It writes `agent-heartbeat <job-id> <unix-ts>` every 5 seconds; no heartbeat for 15 seconds while the container remains up is reported as `silent`, not auto-restarted. If either child exits, the supervisor stops the other and exits so the control plane can mark the sandbox failed and clean its network resources. The agent only reports presence and health: it does not execute tasks, read host secrets, or communicate with other agents.

## Install and run on Foundry

Run from `~/sandbox-mesh`; keep the bearer token in the shell environment, never a file:

```bash
cd ~/sandbox-mesh
read -rsp 'Sandbox Mesh bearer token: ' SANDBOX_MESH_TOKEN; printf '\n'; export SANDBOX_MESH_TOKEN
./install.sh
./demo.sh
```

`install.sh` verifies the Foundry profile, Docker architecture, pinned Buildx and Nixpacks checksums, the bundled supervisor SHA-256, bounded BuildKit settings, and the user systemd service. It does not fall back to a weaker container driver. `GITHUB_TOKEN` may be exported for private repositories; it is used host-side for Git and is never passed to a sandbox.

`demo.sh` runs the automated suite, submits the three public acceptance repositories together, checks FIFO ordering and peak concurrency allowed by the host cap, ARM64 builds, app proxy, authenticated logs, and recent agent heartbeats, then exercises stop/restart and a supervised app crash. The crash check waits for exactly one managed container and network to be removed, confirms the next FIFO job advances when work is queued, and verifies other running sandboxes stay healthy. It also checks failed-build cleanup and cached-start timings. The run requires working Docker/BuildKit and may take several minutes. Set `SANDBOX_MESH_URL` when running the acceptance client from another machine; leave it unset to use `http://127.0.0.1:8787`.

The Phase 2-only acceptance client is also available as `python3 -m mesh.phase2_demo`. It refuses to run while other sandboxes are queued or active and cleans up its own jobs.

## Control plane and UI

The control plane binds only to `127.0.0.1:8787`. If Tailscale is available and port 8787 has no conflicting Serve/Funnel configuration, the installer adds a **private** HTTPS Tailscale Serve route; it preserves unrelated routes and never enables Funnel. Otherwise, the service remains loopback-only. If setup reports that this user lacks Tailscale operator permission, run `sudo tailscale set --operator=$USER` and rerun the installer; the installer does not invoke `sudo` automatically. Check Tailscale exposure with `tailscale serve status`.

Sign in to the UI with the bearer token. The responsive card grid shows sandbox state, queue position/reason, agent heartbeat status, CPU/memory/PID metrics, and live logs; queued jobs can be cancelled. The API includes `GET /api/scheduler`, `GET /api/queue`, and authenticated `GET /api/resources` (managed-resource counts only). Mutations require an authenticated session and CSRF token. The acceptance-only `POST /api/sandboxes/{id}/crash-app` and `POST /api/sandboxes/{id}/kill-agent` actions terminate one supervised child to verify sibling teardown and resource cleanup; they do not execute arbitrary commands.

## Isolation and limits

- The host resolves Git commits and checks out source but does not execute repository build scripts directly.
- BuildKit is resource-limited, disconnected from networks while idle, and connected only to the job's internal network during a build. Egress is default-deny except through the allowlisted proxy in `config.yaml`.
- App containers run as UID 65532 with a read-only root, bounded tmpfs, CPU/RAM/PID limits, a 30-minute TTL, and a 2 GiB image cap. They have no host mounts, Docker socket, published app port, added capabilities, or direct external route.
- The authenticated edge proxy binds only to loopback ports 8900–8999. App traffic is HTTP-only; app WebSockets and app-owned cookies are not supported. Control-plane logs use authenticated WebSockets.
- **BuildKit caveat:** Docker Buildx's `docker-container` driver uses a privileged builder helper. It has resource caps, no host bind mount or Docker socket, and only the per-job internal network while building. App and edge-proxy containers are non-privileged. If the no-privileged rule is meant to include the BuildKit helper, do not run untrusted repositories on Foundry; there is no verified rootless or VM build backend on that host.
- There is no mesh between agents in Phase 2. The UI does not show a live Tailscale route state. Foundry deployment, Docker builds, and remote acceptance still require verification on the actual Foundry host.

## Supervisor build

The release tree includes `mesh/supervisor/bin/sandbox-supervisor-linux-arm64`, pinned by `agent.supervisor_sha256` in `config.yaml`. To rebuild from source with Go installed:

```bash
python3 scripts/build_supervisor.py --arch arm64
GO111MODULE=off go test -race ./mesh/supervisor
```

The build script verifies the ELF architecture and updates the config hash. Build code and regression tests are in `tests/`.
