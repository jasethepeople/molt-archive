# Sandbox Mesh Phase 0 — independent verification (Mon Nov 16, 2026)

Verifier: Muse. Zo's zip received 15:16 MST as
`~/workspace/user/files/sandbox-mesh-phase0_2_kt2g.zip`, extracted to
`~/workspace/sandbox-mesh-phase0-verify/`.

## What Zo delivered
probe.sh, scripts/probe.py, scripts/benchmark.py, tests/test_probe.py,
benchmark.sh, config.yaml, host-profile.json, probe-report.md, README.md,
BUILD-PROMPT.md, .gitignore.

## Checks I ran on the deliverable itself
- Unit tests: `python3 -m unittest discover -s tests -v` → **3 passed**,
  here and in Zo's report. OK.
- BUILD-PROMPT.md is **byte-identical** to the prompt handed to Jason
  (~/workspace/zo-prompts-sandbox-mesh-2026-11-16/.../PROMPT.md). OK.
- probe.py read in full: read-only capability checks only (which, docker
  info, unshare/bwrap smoke tests, /proc + /sys reads). Writes only
  host-profile.json + probe-report.md inside the project dir. No installs,
  no network access, no untrusted code executed. Safe to run on the fleet.

## The big caveat (Zo stated it honestly; it matters)
Zo's probe ran in a **Modal tool-execution container** (hostname `modal`,
Debian 12, kernel `4.19.0-gvisor`), NOT on the actual Zo server. Its
README says so plainly: the values "must not be substituted for a
host-level `zo` probe." So even `zo` itself is still unprobed. Only Zo
(or Jason, on the Zo server) can run the real `zo` probe.

## Fleet probes — I ran probe.sh on all three real hosts (Nov 16 ~15:20 MST)

| Host | Driver | Cap | Key facts |
|---|---|---|---|
| foundry (Oracle VM) | **docker** | 1 | Docker 29.9.3 daemon responding (runc); Supabase 5 containers Up 4d healthy. 1 CPU, 4.10 GiB avail, 32 GiB free disk. systemd active. Fallback netns test FAILED (unprivileged userns restricted on this kernel) — no fallback if Docker dies. |
| omarchy (Acer) | **userns-ulimits-private-netns (option 3, degraded)** | 1 | Docker 29.9.2 client installed but daemon INACTIVE and user NOT in docker group — fixable (start daemon + add user to docker group). bubblewrap installed, unusable. User namespaces work. 4 CPUs but only 1.49 GiB avail → cap 1 (memory-bound). |
| iknow (Kali) | **docker** | 1 | Docker 28.5.2 responding. KVM available (/dev/kvm exists) → microVM driver possible later. 8 CPUs, 4.82 GiB avail, but disk **98% full — 5.4 GiB free of 219 GiB**. Cap 1 (memory-bound). |

Per-host profiles saved here: host-profile-foundry.json,
host-profile-omarchy.json, host-profile-iknow.json. The probe project
itself was copied to `~/sandbox-mesh/` on each host (probe.sh +
host-profile.json + probe-report.md live there now).

## Consequences for the plan
- **Phase 1 can run with real isolation on foundry or iknow (Docker).**
  No degraded-isolation approval needed there.
- **Cap is 1 on every host** (memory-bound everywhere). Phase 2's
  "3 at once" cannot run on any single host — it will need remote
  runners across hosts, or a deliberate cap change. Flagged, not decided.
- **omarchy needs either a Docker fix or explicit degraded approval.**
  The Docker fix is small: start the daemon, add the user to the docker
  group, re-run ./probe.sh.
- **iknow's disk is 98% full.** Sandbox images will eat the remaining
  5.4 GiB fast; do not pick iknow as the main sandbox host without
  clearing disk first.
- **`zo` (the actual server) is still unprobed.** Only Zo can run the
  probe there.

## Decision owed by Jason
Which host runs Phase 1: foundry (Docker, always on, 32 GiB free) or
iknow (Docker + KVM, laptop, disk nearly full)? Recommendation:
foundry. omarchy joins after the Docker fix; zo joins after its own probe.
