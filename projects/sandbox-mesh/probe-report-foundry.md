# Sandbox Mesh capability probe — foundry

- Observed hostname: `foundry`
- OS/kernel: Ubuntu 24.04.5 LTS; `Linux 7.0.0-1013-oracle` (aarch64)
- CPU: 1.00 effective cores (1 online); memory: 4.10 GiB available; disk: 32.09 GiB free
- Derived concurrency cap: **1**
- Open/listening ports: tcp/22, tcp/53, tcp/111, tcp/3000, tcp/3100, tcp/3101, tcp/3105, tcp/3106, tcp/3306, tcp/4173, tcp/5432, tcp/8000, tcp/8001, tcp/46209, tcp/65300, udp/53, udp/68, udp/111, udp/41641

| Capability | Result |
|---|---|
| Docker | ready |
| Rootless Podman | not installed (rootless=False) |
| bubblewrap | not installed |
| nsjail | not installed |
| firejail | not available |
| gVisor/runsc | not installed |
| KVM | not available |
| User + private network namespaces | not verified |
| cgroups | v2 (writable delegated v2=False) |
| systemd | active |

**Selected driver:** `docker`/`runc` — the Docker daemon was responding. No gVisor or KVM runtime was available on Foundry at probe time.

**Repository execution:** not enabled by this Phase 0 package
