# Phase 2 verification — service and port health checks

**Date:** 2026-11-09 (America/Denver)

## Results

- `npx tsc -p tsconfig.json`: passed under strict TypeScript.
- `npm test`: 44 passed, 0 failed. This includes Phase 1 parser/registry tests plus six Phase 2 tests for schema migration, live-to-dead transitions, unknown and UDP handling, HEAD-to-GET fallback, credential-bearing URL refusal, and HTTP timeout handling.
- Exact Phase 1 Markdown acceptance: source SHA-256 `387420fdbe07cf16790b06b3dab95d2df48ac71433d8726589296e54900a5f12`; seed exit 0; counts 10 accounts, 6 machines, 315 services, 3 ports, 5 gaps, 10 reference notes, and 3 warnings; verification PASS with 12 hard checks and 0 failures.
- Empty-map control: seeding required `--allow-empty`; verification exited 1 with the expected `extraction-nonzero` and `registry-nonzero` failures.
- Local loopback demonstration: a temporary HTTP service and TCP port changed from live/up to dead/down after the server stopped. The same rows and IDs were updated; the map's original service status remained unchanged. The periodic runner emitted check summaries and exited cleanly on `SIGTERM` in a no-target smoke test.
- `ao live-status` against the freshly seeded exact map showed 315 services and 3 ports with live state `unknown` before a check had been run; it did not alter the database.

## Operational limits

The Oracle VM was not reachable from this sandbox, so the systemd unit was not installed or started. Tailnet reachability, the actual Foundry/gateway/Hub status, and a Node 24 ARM64 native-module build remain unverified and require a run on the VM. `systemd-analyze verify` could not resolve the unit's configured remote Node executable at `/home/ubuntu/node-v24.22.2-linux-arm64/bin/node`; the service file is provided as an install artifact only. No service was registered or exposed from Zo.

The Phase 2 checker is outbound-only. It does not open a listener. It preserves Phase 1 map status and updates separate live-health fields atomically; endpoints it cannot safely or meaningfully probe remain `unknown`.
