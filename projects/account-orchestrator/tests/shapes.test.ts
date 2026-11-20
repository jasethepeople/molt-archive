/**
 * The shapes the production map actually uses. Each test names the defect it
 * covers, so a regression says which tool broke which promise.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseNetworkMap } from '../src/parse/map.js';

const IDENTITY = [
  '## Identity',
  '',
  '| Thing | What it is | Status |',
  '| --- | --- | --- |',
  '| Gmail — personal | jason.personal@example.com, mail since 2011 | live |',
  '| Gmail — work | jason.work@example.com, forwarding | live |',
  '| GitHub | jason-handle, repositories | live |',
  '| Cloudflare | jason-handle@example.com, DNS | unknown |',
].join('\n');

test('defect 1: Thing | What it is | Status tables become accounts', () => {
  const parsed = parseNetworkMap(IDENTITY, 'shape.md');
  assert.equal(parsed.accounts.length, 4);
  assert.equal(parsed.unparsed.length, 0, 'no row should become a gap');

  const personal = parsed.accounts.find((account) => account.login === 'jason.personal@example.com');
  assert.ok(personal, 'the address in the description is the login');
  assert.equal(personal.provider, 'Gmail');
  assert.equal(personal.purpose, 'mail since 2011');
  assert.equal(personal.status, 'live');

  const github = parsed.accounts.find((account) => account.provider === 'GitHub');
  assert.ok(github);
  assert.equal(github.login, 'jason-handle', 'a bare handle in the description is the login');
  assert.equal(github.purpose, 'repositories');

  const gmail = parsed.accounts.filter((account) => account.provider === 'Gmail');
  assert.equal(gmail.length, 2, 'two Gmail rows stay two accounts');
});

test('defect 1: Machine | What it is | Status tables become machines', () => {
  const parsed = parseNetworkMap(
    [
      '## Machines',
      '| Machine | What it is | Status |',
      '| --- | --- | --- |',
      '| Acer | main rig, tailnet 100.64.1.10, Windows 11 | live |',
      '| Oracle VM | public services, 129.146.21.52 | live |',
      '| Old tower | parts machine | dead |',
    ].join('\n'),
    'shape.md',
  );
  assert.equal(parsed.machines.length, 3);
  const acer = parsed.machines.find((machine) => machine.name === 'Acer')!;
  assert.equal(acer.tailnetIp, '100.64.1.10');
  assert.equal(acer.status, 'live');
  assert.match(acer.role ?? '', /main rig/);
  const oracle = parsed.machines.find((machine) => machine.name === 'Oracle VM')!;
  assert.equal(oracle.tailnetIp, '129.146.21.52', 'a public address is recorded too');
  assert.equal(parsed.machines.find((machine) => machine.name === 'Old tower')!.status, 'dead');
});

test('defect 1: Site | What it is | Status tables become services', () => {
  const parsed = parseNetworkMap(
    [
      '## Live sites',
      '| Site | What it is | Status |',
      '| --- | --- | --- |',
      '| Hub | the internal hub | live |',
      '| Docs | reference docs | unknown |',
    ].join('\n'),
    'shape.md',
  );
  assert.equal(parsed.services.length, 2);
  assert.equal(parsed.services[0]!.kind, 'site');
  assert.equal(parsed.services[0]!.name, 'Hub');
  assert.equal(parsed.services[0]!.status, 'live');
});

test('defect 2: Markdown links give services their URL, and the text stays the name', () => {
  const parsed = parseNetworkMap(
    [
      '## Live sites',
      '| Site | What it is | Status |',
      '| --- | --- | --- |',
      '| Hub | the hub, [open](http://100.64.1.10:8471) | live |',
      '| Praxis | skills, [praxis](https://praxis.example.com/skills) | live |',
    ].join('\n'),
    'shape.md',
  );
  const hub = parsed.services.find((service) => service.name === 'Hub')!;
  assert.equal(hub.url, 'http://100.64.1.10:8471');
  assert.ok(parsed.ports.some((port) => port.port === 8471), 'the URL port is recorded');
  const praxis = parsed.services.find((service) => service.name === 'Praxis')!;
  assert.equal(praxis.url, 'https://praxis.example.com/skills');
});

test('defect 2: links in bullets are URLs, not dropped text', () => {
  const parsed = parseNetworkMap(
    ['## Projects', '- [network-map](https://github.com/owner/network-map) — the map generator'].join('\n'),
    'shape.md',
  );
  assert.equal(parsed.services.length, 1);
  assert.equal(parsed.services[0]!.name, 'network-map');
  assert.equal(parsed.services[0]!.url, 'https://github.com/owner/network-map');
  assert.equal(parsed.services[0]!.kind, 'repo');
});

test('defect 3: ports written in prose are harvested', () => {
  const parsed = parseNetworkMap(
    [
      '## Machines',
      '| Machine | What it is | Status |',
      '| --- | --- | --- |',
      '| Acer | main rig, 100.64.1.10 | live |',
      '| Oracle VM | public, 129.146.21.52 | live |',
      '',
      'The Hub answers at 100.64.1.10:8471 and the mirror at 129.146.21.52:443.',
      '',
      '## Ports worth writing down',
      '- Metrics UI (8428) sits on the Acer.',
    ].join('\n'),
    'shape.md',
  );
  const ports = parsed.ports.map((port) => `${port.id}`);
  assert.ok(ports.includes('acer:8471/tcp'), `expected the host:port port, got ${ports.join(', ')}`);
  assert.ok(ports.includes('oracle-vm:443/tcp'), `expected the public-IP port, got ${ports.join(', ')}`);
  assert.ok(ports.includes('acer:8428/tcp'), `expected the prose port, got ${ports.join(', ')}`);
  const metrics = parsed.services.find((service) => service.name === 'Metrics UI');
  assert.ok(metrics, 'the prose port also names a service');
  assert.equal(metrics.machineId, 'acer');
});

test('defect 4: status-key bullets are reference text, not accounts', () => {
  const parsed = parseNetworkMap(
    [
      '## How to read this',
      '',
      '- **live** — running and verified',
      '- **dead** — retired or gone',
      '- **unknown** — nobody has checked',
      '- **tailnet** — private network only',
      '',
      '## Identity',
      '| Thing | What it is | Status |',
      '| --- | --- | --- |',
      '| Gmail | a@example.com | live |',
    ].join('\n'),
    'shape.md',
  );
  assert.equal(parsed.accounts.length, 1, 'the key must not become accounts');
  assert.equal(parsed.references.length, 4);
  assert.equal(parsed.unparsed.length, 0, 'the key is kept, not reported as a gap');
});

test('round 4: mixed machine and service-provider rows keep only the six devices', () => {
  const parsed = parseNetworkMap(
    [
      '## Machines and servers',
      '| Machine | What it is | Status |',
      '| --- | --- | --- |',
      '| Oracle VM "foundry" — 129.146.21.52, tailnet 100.83.138.76, Ubuntu 24.04 | Always Free cloud VM | running |',
      '| T340 (Dell PowerEdge T340) — Kali, LAN 192.168.12.193, Tailscale 100.115.192.41 | The big server | in storage |',
      '| Acer (Aspire 5, Ryzen 3, 6 GB, Omarchy) — main dev rig | Intel Hub deploys here | running |',
      '| iknow (Kali laptop) — tailnet 100.116.98.86 | AAB builds and Supabase CLI | running |',
      '| Mom\'s Windows 11 laptop | His old Windows laptop | exists |',
      '| Phone — S22 Ultra | His phone | running |',
      '| Supabase — The Bet (`uechkunlijbtkufubxdo`) | The Bet\'s database + Edge Functions | live |',
      '| Supabase — undercurrent-web (`stildvovnznsvsfxsnzv`) | Undercurrent\'s database | live |',
      '| Vercel | Hosts meshpointsystems.com | live |',
      '| Netlify (isitlocated login) | Hosts landing pages | live |',
      '| Google Play — BugSplat (jasonclarkagain) | Developer account | live |',
    ].join('\n'),
    'round4-shape.md',
  );
  assert.deepEqual(
    parsed.machines.map((machine) => machine.name),
    [
      'Oracle VM "foundry"',
      'T340 (Dell PowerEdge T340)',
      'Acer Aspire 5',
      'iknow (Kali laptop)',
      "Mom's Windows 11 laptop",
      'Phone S22 Ultra',
    ],
  );
  assert.equal(parsed.unparsed.length, 0);
});

test('round 4: prose bullets and port-table lists attach three ports to the right machines', () => {
  const parsed = parseNetworkMap(
    [
      '## Machines and servers',
      '| Machine | What it is | Status |',
      '| --- | --- | --- |',
      '| Oracle VM "foundry" — 129.146.21.52, tailnet 100.83.138.76 | Foundry dashboard on port 3000, integration-gateway on port 3100 | running |',
      '| Acer (Aspire 5, Ryzen 3) — main dev rig | Intel Hub deploys here on port 8471 | running |',
      '',
      '## Agent stack',
      '- **Foundry** — dashboard on port 3000 at http://100.83.138.76:3000, on the Oracle VM.',
      '- **integration-gateway** — health and echo on port 3100, on the Oracle VM.',
      '- **intel-hub** — dashboard at http://127.0.0.1:8471; deployment is on the Acer Aspire 5.',
      '',
      '## Ports',
      '| Machine | Ports |',
      '| --- | --- |',
      '| Oracle VM "foundry" | 3000+3100 |',
      '| Acer Aspire 5 | 8471 |',
    ].join('\n'),
    'round4-ports.md',
  );
  assert.deepEqual(
    parsed.ports.map((port) => `${port.machineId}:${port.port}/${port.protocol}`).sort(),
    ['acer-aspire-5:8471/tcp', 'oracle-vm-foundry:3000/tcp', 'oracle-vm-foundry:3100/tcp'],
  );
  assert.equal(parsed.machines.length, 2, 'URL addresses do not create duplicate localhost or bare-IP machines');
});

test('round 4: every multiword status legend entry is retained as reference text', () => {
  const parsed = parseNetworkMap(
    [
      '- **BUILT** — code is done',
      '- **EXISTS — NOT RUNNING** — present but stopped',
      '- **IN STORAGE** — parked hardware',
      '- **SUBMITTED** — waiting on a result',
      '- **IN TESTING** — not public',
      '- **PARKED** — stopped by decision',
      '- **PLANNED** — not built yet',
      '- **UNCHECKED** — not verified',
    ].join('\n'),
    'round4-statuses.md',
  );
  assert.equal(parsed.references.length, 8);
  assert.equal(parsed.accounts.length, 0);
  assert.equal(parsed.unparsed.length, 0);
});

test('defect 4: a bullet outside any section keeps its link', () => {
  const parsed = parseNetworkMap('- [dotfiles](https://github.com/owner/dotfiles) — machine setup', 'shape.md');
  assert.equal(parsed.services.length, 1);
  assert.equal(parsed.services[0]!.url, 'https://github.com/owner/dotfiles');
  assert.equal(parsed.unparsed.length, 0);
});
