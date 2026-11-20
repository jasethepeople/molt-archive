# What the parser reads

The network map is prose written for a person, so the parser is tolerant about
shape and strict about meaning. This page is the contract: the shapes it reads, the
shapes it refuses, and how to see what it skipped.

Nothing is guessed. If the map does not say a purpose, the record says `unknown`.
If the map says something the parser cannot place, it appears in `ao gaps` with a
`file:line`. If it is explanation rather than data — a status key, a legend — it is
kept as text and shown by `ao reference`.

## 1. The three main tables

The map's workhorse shape is a name, a sentence about it, and a status. The first
header decides what the rows are.

```markdown
## Identity
| Thing | What it is | Status |
| --- | --- | --- |
| Gmail — personal | jason.personal@example.com, mail since 2011 | live |

## Machines
| Machine | What it is | Status |
| --- | --- | --- |
| Acer | main rig, tailnet 100.64.1.10, Windows 11 | live |

## Live sites
| Site | What it is | Status |
| --- | --- | --- |
| Hub | the internal hub, [open hub](http://100.64.1.10:8471) | live |
```

- `Thing` / `Account` / `Identity` → **accounts**. `Gmail — personal` gives provider
  `Gmail` and qualifier `personal`. The login is, in order of preference: an email
  address anywhere in the row, an `@handle`, the qualifier, or a single bare token
  at the start of the description (`| GitHub | jason-handle, repositories |`). If
  none of those exist the label itself is recorded and flagged in `ao warnings` —
  no address is invented.
- `Machine` / `Host` / `Device` → **machines**. A tailnet address is preferred when
  the row contains both public and tailnet IPv4 addresses; the remaining description
  becomes its role. In a mixed machine table, provider/service rows such as Supabase,
  Vercel, Netlify and Google Play are not treated as physical machines.
- `Site` / `Service` / `App` → **services**, kind `site`. A URL anywhere in the row
  becomes the service's URL, and a port inside that URL becomes a port record.

The column header does not have to be exactly `What it is`: `Description`, `Details`,
`Notes` and `Summary` work too. The heading above the table decides what the rows
are when the header is ambiguous (`## Identity` beats a generic `Thing` column).

## 2. Classic column tables

Still supported, and still the most precise way to write a row:

```markdown
| Provider | Login | Purpose | Status | Linked to | Secret ref |
| --- | --- | --- | --- | --- | --- |
| GitHub | jason@example.com | code hosting | live | google | env:GITHUB_TOKEN |
```

- `Provider` — provider, platform, vendor, company, service, site
- `Login` — login, email, handle, username, user, account, identity
- `Purpose` — purpose, use, function, role, description, notes
- `Status` — status, state, health → `live` / `dead` / `unknown`; any other word becomes `unknown` and is kept in `notes`
- `Linked to` — linked, related, connected to, ties → resolved against the other accounts by login, provider, or `provider:login`
- `Secret ref` — must be `scheme:path`, never a value

Machines: a table with a machine column plus an IP/OS column becomes machines.
Services: a table with a name and URL column becomes services.
Ports: any table with a `Port` column becomes ports, attached to the machine column
or to the enclosing machine section, and to the named service if there is one. A port
cell may list several ports separated by `+`, commas, slashes or `and`.

## 3. Markdown links

Every URL may be written as `[text](url)`. The link target is the URL; the text is
the name. This works in tables, bullets and prose:

```markdown
| Site | What it is | Status |
| --- | --- | --- |
| Hub | the hub, [open hub](http://100.64.1.10:8471) | live |

- [network-map](https://github.com/owner/network-map) — the map generator
```

The bullet above becomes a service named `network-map`, kind `repo` (the URL is a
GitHub/GitLab URL), with that URL.

## 4. Ports in prose

All of these are read:

| Written as | Becomes |
| --- | --- |
| `100.64.1.10:8471` | port 8471 on the machine that owns that address |
| `129.146.21.52:443` | port 443 on the machine that owns that public address |
| `Hub (8471)` | a service named Hub, with port 8471, on the machine named nearby or in the section |
| `Metrics UI (8428) sits on the T340` | service Metrics UI on the T340, port 8428 |
| `http://100.64.1.10:8471/status` | a service for that URL, plus port 8471 |

Ports are also harvested from bullet sentences and machine-row descriptions when
written as `port 3000`, `ports 3000+3100`, or `host:port`; matching known machine names
attach them to the correct machine. An IP URL joins an existing machine when the map
lists that address; otherwise a non-loopback IP may be retained as an explicitly
unidentified IP-only machine record, never as a machine named after the service.

## 5. Bullets

Under a recognised section heading, bullets work. Sections are recognised by their
heading text: *machine(s), host, device, box, node, hardware, topology* · *account(s),
credential, login, identity, subscription, ownership* · *service(s), site(s), endpoint,
stack, runtime, app(s), deployment* · *repo(s), repository, code, project(s)* · *port(s),
listener* · *link(s), relationship, dependency*.

Fields may be separated by `|`, by an em/en dash, or by `Provider: login`. A bullet
outside any recognised section still yields a service when it carries a link, and a
machine when it carries an address; only genuinely unreadable bullets become gaps.

## 6. Reference text (keys, legends)

A section whose heading says *how to read this*, *status key*, *legend*, *notation*
or *key to* is kept as text: the bullets land in `import_note` at level `reference`
and print with `ao reference`. They never become accounts. The same applies to any
bullet that opens with a status word and a dash (`- **live** — running`), including
multiword legend keys such as `BUILT`, `IN STORAGE`, `IN TESTING`, and `EXISTS — NOT
RUNNING`.

## 7. Structured blocks (the exact form)

If you would rather be explicit, put a fenced `json` block anywhere in the map:

````markdown
```json
{
  "machines": [{ "name": "Acer", "tailnetIp": "100.64.1.10", "os": "Windows 11", "role": "main rig", "status": "live" }],
  "accounts": [{ "provider": "GitHub", "login": "jason@example.com", "purpose": "code hosting", "status": "live", "secretRef": "env:GITHUB_TOKEN", "linkedTo": ["google"] }],
  "services": [{ "name": "Hub", "kind": "app", "machine": "Acer", "url": "http://100.64.1.10:8471", "status": "live", "account": "GitHub:jason@example.com" }],
  "ports": [{ "machine": "Acer", "port": 8471, "protocol": "tcp", "service": "Hub", "health": "up" }],
  "links": [{ "from": "github:jason-example-com", "to": "google:jason-example-com", "relation": "sso" }]
}
```
````

## 8. What is refused, on purpose

| Input | What happens |
| --- | --- |
| A fenced block in `yaml`, `toml`, or an unlabelled block of `key: value` lines | **gap**, with the reason "unsupported block language — convert to JSON". A half-working YAML parser would be a silent source of wrong records |
| A table whose columns match nothing | **gap**, quoting the header |
| `Linked to` naming an account the map does not define | **gap**, quoting the target |
| An account row with no name, or no provider | **gap** |
| A `secret_ref` that is not `scheme:path` | **gap**, with the parser's objection |
| A port whose machine cannot be identified | **gap** |
| A bullet with no section, no link and no address | **gap** |
| Any credential-looking value anywhere in the map | **the seed refuses to run**, printing `file:line` per hit with the value masked |

Gaps are stored in the registry and printed by `ao gaps`. They are not errors in the
map — they are the parts a human still has to decide about.

## 9. Two guarantees worth knowing

1. **An unreadable map cannot pass.** `verify` has a hard check that the map yielded
   at least one account, one machine and one port. A parser that reads nothing fails
   with `extraction-nonzero`, even though 0 = 0 would otherwise reconcile perfectly.
2. **An unreadable map cannot erase a good registry.** `seed` refuses to overwrite a
   populated registry when the map yields nothing, unless `--allow-empty` says so.

## 10. Working with the supplied line-oriented outline

Round 3 adds a direct parser path for the pasted `Jason's Network Map` text export.
It recognizes the repeated label / description / status triples for identity and
machines, named project/repository lines, the repository appendix, the service
platform entries, and the machine-local ports. It keeps original file line numbers;
it does not create a normalized copy. Unknown statuses remain `unknown` with the
source wording retained as notes. Status-key lines are reference text. For the
pasted source, the two planned MOLT items and three SaaS shelf notes are reported as
gaps by design.

```bash
MAP=/path/to/pasted-network-map.txt
npm run seed -- --map "$MAP" --dry-run   # parse and count, write nothing
npm run seed -- --map "$MAP"             # write
npm run ao -- gaps                        # the five unplaced notes
npm run ao -- reference                   # status legend kept as text
npm run ao -- warnings                    # merged entries
npm run verify -- --map "$MAP"           # reconciliation report
```

Round 4 acceptance was run against the exact 468-line Markdown source, SHA-256
`387420fdbe07cf16790b06b3dab95d2df48ac71433d8726589296e54900a5f12`. It seeds
10 accounts, 6 machines, 315 services, 3 ports, and 5 gaps; verification passes all
12 hard checks. See `reports/round4-verification-2026-11-09.md`.
