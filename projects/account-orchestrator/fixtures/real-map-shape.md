# Shape stand-in for the production network map

**This is not the production map.** It is a reconstruction of its *shapes*, built
from the five defects reported by the verifier: `Thing | What it is | Status`
tables, `Machine | What it is | Status` tables, `Site | What it is | Status`
tables, Markdown-link URLs, ports written in prose, and status-key bullets at the
top. Every name, address and account here is invented for testing.

The production map (`jason-network-map-2026-11-09.md`) must be run against
separately; nothing in this file is evidence about Jason's real accounts.

## How to read this

- **live** — running and verified this week
- **dead** — retired, cancelled, or gone
- **unknown** — nobody has checked
- **linked** — shares an identity with another row
- **solo** — its own login, not shared
- **tailnet** — reachable on the private network only
- **public** — reachable from the internet
- **local** — loopback only
- **archived** — kept for reference, not running
- **handover** — someone else owns it now

## Identity

| Thing | What it is | Status |
| --- | --- | --- |
| Gmail — personal | fixture.personal@example.com, mail since 2011 | live |
| Gmail — work | fixture.work@example.com, forwarding to personal | live |
| Gmail — signups | fixture.signups@example.com, throwaway for trials | live |
| Gmail — finance | fixture.finance@example.com, statements only | live |
| Gmail — old | fixture.old@example.com, abandoned at migration | dead |
| Gmail — shared | fixture.shared@example.com, family calendar | live |
| Gmail — robot | fixture.robot@example.com, app notifications | unknown |
| GitHub | fixture-owner, personal and work repositories | live |
| Cloudflare | fixture-owner@example.com, DNS for every domain | live |
| Stripe | fixture-owner@example.com, test keys only | unknown |

## Machines

| Machine | What it is | Status |
| --- | --- | --- |
| Acer | main rig, tailnet 100.64.1.10, Windows 11 | live |
| iknow laptop | travel machine, tailnet 100.64.1.11, Ubuntu 24.04 | live |
| T340 | always-on box, tailnet 100.64.1.12, Debian 12 | live |
| Oracle VM | public services, 129.146.21.52, Ubuntu 22.04 | live |
| ThinkCentre | spare desktop, 100.64.1.13, Windows 10 | dead |
| Pi 4 | DNS and DHCP, 100.64.1.14, Raspberry Pi OS | live |
| NAS | backups, 100.64.1.15, TrueNAS | live |
| Steam Deck | handheld, 100.64.1.16 | unknown |
| Work laptop | employer-managed, 100.64.1.17, WSL | live |
| Old tower | 100.64.1.18, parts machine | dead |
| Phone | 100.64.1.19, Android | live |

## Live sites

| Site | What it is | Status |
| --- | --- | --- |
| Hub | the internal hub, [open hub](http://100.64.1.10:8471) | live |
| integration-gateway | execution layer, [gateway](http://100.64.1.10:3456) | live |
| Praxis | signed skills, [praxis](http://100.64.1.10:8472) | live |
| Foundry | trust layer, [foundry](http://100.64.1.12:3000) | live |
| The Room | multi-agent sandbox, [room](http://100.64.1.12:3100) | unknown |
| Intel Hub | evidence search, [intel hub](http://100.64.1.12:8473) | live |
| Oracle mirror | public mirror at 129.146.21.52 | live |
| Notes | personal wiki, [notes](http://100.64.1.11:8080) | live |
| Backups | nightly snapshots, [backups](http://100.64.1.15:5000) | live |
| Dashboard | status page, [status](http://100.64.1.10:9090) | dead |
| Old blog | retired, [blog](https://blog.example.com) | dead |
| Mail relay | transactional mail, [relay](https://relay.example.com) | unknown |
| Docs | reference docs, [docs](https://docs.example.com) | live |
| Registry mirror | container images, [registry](http://100.64.1.12:5001) | live |
| Metrics | time series, [metrics](http://100.64.1.12:8428) | live |

The Hub answers on port 8471 and the gateway health check on 3456. Praxis
listens on 8472, Foundry on 3000 and the Room on 3100. The Oracle mirror is
reachable at 129.146.21.52:443 and its database at 129.146.21.52:5432.

## Ports worth writing down

- Metrics UI (8428) sits on the T340, next to the registry mirror.
- Pi hole admin (80) is on the Pi 4.

## Projects

- [network-map](https://github.com/fixture-owner/network-map) — generates this map
- [account-orchestrator](https://github.com/fixture-owner/account-orchestrator) — this project
- [praxis](https://github.com/fixture-owner/praxis) — skill layer
- [foundry](https://github.com/fixture-owner/foundry) — signing and validation
- [room](https://github.com/fixture-owner/room) — multi-agent sandbox
- [intel-hub](https://github.com/fixture-owner/intel-hub) — evidence search
- [old-scraper](https://github.com/fixture-owner/old-scraper) — retired
- [dotfiles](https://github.com/fixture-owner/dotfiles) — machine setup

## Odds and ends

- The NAS may need a new drive before winter
- A domain transfer nobody finished
