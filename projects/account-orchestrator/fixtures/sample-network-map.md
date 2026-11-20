# SYNTHETIC FIXTURE — not Jason's map

**This file is not the network map.** It is a small, deliberately messy stand-in
used to exercise the parser and the verification report. Every account, address,
and host here is invented for testing. The real map
(`jason-network-map-2026-11-09.md`) must be supplied separately.

It contains, on purpose: a machine table, an account table, a service table, a
port table, bullet-style repos, a bullet-style account, a paragraph with URLs, a
fenced block in an unsupported language, and a section the parser will not
recognise.

## Machines

| Machine | Tailnet IP | OS | Role | Status |
| --- | --- | --- | --- | --- |
| Acer | 100.64.1.10 | Windows 11 | main rig, runs Praxis | live |
| iknow laptop | 100.64.1.11 | Ubuntu 24.04 | travel machine | live |
| T340 | 100.64.1.12 | Debian 12 | always-on box | live |
| Oracle VM | 100.64.1.20 | Ubuntu 22.04 | public services | live |

## Accounts

| Provider | Login | Purpose | Status | Linked to | Secret ref |
| --- | --- | --- | --- | --- | --- |
| GitHub | fixture-user@example.com | code hosting, 290 repos | live | google, cloudflare | env:GITHUB_TOKEN |
| Google | fixture-user@example.com | mail, drive, oauth for everything | live | github | 1password:google |
| Cloudflare | fixture-user@example.com | DNS and tunnels | live | | env:CLOUDFLARE_TOKEN |
| Stripe | fixture-user@example.com | payments, test mode only | live | | |
| AWS | fixture-user@example.com | old experiments | dead | | |
| DigitalOcean | fixture-user@example.com | droplets, mostly retired | dead | | |
| Namecheap | fixture-user@example.com | domains | live | cloudflare | |
| npm | fixture-user | package publishing | unknown | github | |
| Hugging Face | fixture-user | model downloads | live | | |
| Sendgrid | fixture-user@example.com | transactional mail | unknown | | |

### Bullet-style entries

- Notion: fixture-user@example.com (purpose: notes and planning)
- Vercel: fixture-user@example.com (purpose: preview deploys, dead)

## Services

| Service | Kind | Machine | URL | Status |
| --- | --- | --- | --- | --- |
| Hub | app | Acer | http://100.64.1.10:8471 | live |
| integration-gateway | api | Acer | http://100.64.1.10:3456 | live |
| Foundry | app | T340 | http://100.64.1.12:3000 | live |
| Praxis | app | Acer | | live |
| The Room | app | T340 | | unknown |
| Intel Hub mirror | database | Oracle VM | | unknown |

## Ports

| Machine | Port | Protocol | Service | Health |
| --- | --- | --- | --- | --- |
| Acer | 8471 | tcp | Hub | up |
| Acer | 3456 | tcp | integration-gateway | up |
| Acer | 3000 | tcp | Foundry | down |
| T340 | 22 | tcp | | up |
| Oracle VM | 443 | tcp | | up |
| Oracle VM | 5432 | tcp | Intel Hub mirror | unknown |

The Hub answers at http://100.64.1.10:8471/status and the gateway health check
lives at http://100.64.1.10:3456/healthz.

## Repositories

- fixture-owner/orchestrator — this project
- fixture-owner/network-map — the map generator
- fixture-owner/praxis — skills
- fixture-owner/room — multi-agent sandbox

## Retired, in a format the tool does not read

```yaml
retired:
  - provider: old-host
    login: fixture-user
    reason: account closed 2023
```

## Loose ends

- Something about a DNS zone that was never written down
- The NAS that may or may not still exist
