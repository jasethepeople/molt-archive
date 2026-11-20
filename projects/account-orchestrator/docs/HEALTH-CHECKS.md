# Phase 2: live health checks

The checker is an outbound-only process: it opens no listener and exposes no API. It probes each service URL with HTTP `HEAD` (retrying a `405` or `501` with a one-byte-range `GET`) and each registered TCP port with a TCP connect. UDP ports, invalid or credential-bearing URLs, and endpoints with no usable target remain `unknown`; failures never become guessed successes. HTTP success means a final 2xx response. TCP success means the connection completes.

The Phase 1 `service.status` value is retained as the map's snapshot. Checks update `service.live_status` (`live` / `dead` / `unknown`), `service.health` (`up` / `down` / `unknown`), and `service.last_seen`. Ports update their existing `health` and `last_seen` fields. Services with a URL use that HTTP result; a service without a URL derives its status from linked ports (any `up` means `live`, all `down` means `dead`, otherwise `unknown`). A service with neither URL nor linked ports stays `unknown` with no last-seen time.

A cycle probes outside a database transaction, then updates all result rows and `last_health_check_at` in one SQLite transaction. An interrupted probe cycle therefore leaves the previous complete snapshot intact; repeated cycles update existing rows instead of adding history rows. The schema migration adds only live-health columns and preserves the original seeded map status and registry records.

## Commands

```sh
npm run ao -- check
npm run ao -- live-status
npm run ao -- run
```

`check` runs once. `run` checks immediately and repeats every five minutes. `--timeout-ms` defaults to 3000 and accepts 1–60000; `--concurrency` defaults to 16 and accepts 1–64. `run --interval-ms` defaults to 300000 and accepts 1000–86400000. The environment equivalents are `AO_CHECK_TIMEOUT_MS`, `AO_CHECK_CONCURRENCY`, and `AO_CHECK_INTERVAL_MS`. All commands also accept `--db <path>` and `--json`.

The first `check` or a `seed` applies the additive schema migration to an existing Phase 1 database. `live-status` remains read-only and reports an actionable error until that migration has run. A health check never reseeds or rewrites the map-derived records.

## Install on the Oracle VM (instructions only; not deployed)

The supplied VM facts are user `ubuntu`, Node 24 at `$HOME/node-v24.22.2-linux-arm64/bin`, tailnet address `100.83.138.76`, and `XDG_RUNTIME_DIR=/run/user/1000` for non-interactive `systemctl --user` commands. No public listener is created; the checker only makes outbound probes to the registered URLs and literal machine IPs.

1. Copy the Phase 2 ZIP and the exact network-map Markdown to the VM over a private channel. Do not publish the map; it contains private network details. Then unpack the single `account-orchestrator/` tree:

   ```sh
   mkdir -p "$HOME/.local/share"
   unzip Account_Orchestrator_Build_Plan__5.zip -d "$HOME/.local/share"
   cd "$HOME/.local/share/account-orchestrator"
   export PATH="$HOME/node-v24.22.2-linux-arm64/bin:$PATH"
   npm ci
   npm run build
   ```

   `better-sqlite3` is the only runtime dependency. If npm cannot use a Node 24 ARM64 prebuild, the VM needs `python3`, `make`, and a C++ compiler to build that existing dependency; this was not testable in the local x86_64 environment.

2. Set the registry path and decide whether a Phase 1 database already exists:

   ```sh
   APP="$HOME/.local/share/account-orchestrator"
   export AO_DB="$APP/registry.sqlite"
   MAP="$HOME/path/to/jason-network-map-2026-11-09.md"
   ```

   If `registry.sqlite` already exists, back it up before changes. Run `node dist/src/cli.js check --db "$AO_DB"` once; this migrates a Phase 1 database in place and records the first health snapshot without reseeding. If no database exists, dry-run and then seed it from the exact map before enabling the service:

   ```sh
   node dist/src/seed.js --map "$MAP" --db "$AO_DB" --dry-run
   node dist/src/seed.js --map "$MAP" --db "$AO_DB"
   node dist/src/verify.js --map "$MAP" --db "$AO_DB" --no-report
   ```

   Do not reseed an existing registry just to install the checker; seeding intentionally rebuilds the map-derived snapshot.

3. Install and start the user unit:

   ```sh
   mkdir -p "$HOME/.config/systemd/user"
   cp "$APP/systemd/account-orchestrator-health.service" "$HOME/.config/systemd/user/"
   export XDG_RUNTIME_DIR=/run/user/1000
   systemctl --user daemon-reload
   systemctl --user enable --now account-orchestrator-health.service
   systemctl --user status account-orchestrator-health.service
   journalctl --user -u account-orchestrator-health.service -f
   ```

   If the service must continue after the last login session ends, an administrator can enable lingering for `ubuntu` once with `sudo loginctl enable-linger ubuntu`. This deployment was not performed or verified from this environment; only the local loopback test is evidence for probe behavior. Tailnet reachability and the real Foundry, gateway, and Hub statuses require a run on the Oracle VM.
