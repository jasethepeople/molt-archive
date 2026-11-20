import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseNetworkMap } from '../src/parse/map.js';

const PASTED_SHAPE = [
  "Jason's Network Map",
  'Date: 2026-11-09',
  'What this is: a network inventory.',
  'How to read statuses.',
  'LIVE — up.',
  'RUNNING — process active.',
  'BUILT — ready, not running.',
  'EXISTS — NOT RUNNING — present but down.',
  'IN STORAGE — parked hardware.',
  'SUBMITTED — awaiting result.',
  'IN TESTING — private test.',
  'PARKED — paused.',
  'PLANNED — not built.',
  'UNCHECKED — not inspected.',
  '',
  '1. Identity roots',
  'Thing',
  'What it is',
  'Status',
  'jason@example.com',
  'Root account.',
  'LIVE',
  '',
  '11. Machines and servers',
  'Machine',
  'What it is',
  'Status',
  'Oracle VM "foundry" — 129.146.21.52, tailnet 100.83.138.76, Ubuntu 24.04',
  'Foundry and gateway run as user services.',
  'RUNNING — resident services',
  'T340 (Dell PowerEdge) — Kali, LAN 192.168.12.193, Tailscale 100.115.192.41',
  'Air-gapped lab host.',
  'IN STORAGE since Oct 31',
  'Acer (Aspire 5) — main dev rig',
  'Intel Hub deploy target.',
  'RUNNING — main machine',
  'iknow (Kali laptop) — tailnet 100.116.98.86',
  'Build laptop.',
  'RUNNING — workloads idle',
  "Mom's Windows 11 laptop",
  "His mom's laptop.",
  'EXISTS — NOT TO BE USED without her go',
  'Phone — S22 Ultra',
  'His phone.',
  'RUNNING',
  '',
  '2. Project overview',
  'The projects section follows the identity and machine sections.',
  'The named projects precede the repository appendix.',
  '',
  '3. Service entries',
  'MOLT Forge — github.com/jasethepeople/molt-forge — hackathon entry.',
  'Echoes / other-repo — github.com/jasethepeople/other-repo — alias for the repo.',
  'Foundry (Agent Foundry) — dashboard http://100.83.138.76:3000, port 3000 on Oracle VM',
  'integration-gateway — gateway on the Oracle VM, port 3100',
  'intel-hub — verified at http://127.0.0.1:8471; deploy to Acer, port 8471',
  'Supabase — The Bet',
  "The Bet's database.",
  'LIVE',
  'Supabase — undercurrent-web',
  'Undercurrent database.',
  'LIVE',
  'Vercel',
  'Hosts a site.',
  'LIVE',
  'Netlify (account)',
  'Hosts sites.',
  'LIVE',
  'Google Play — BugSplat',
  'Developer account.',
  'LIVE',
  'moltcoin — internal token. Status: PLANNED',
  'molt-verify — planned verifier. Status: PLANNED',
  '23 single-user browser apps, built and wrapped.',
  'Ledger Notebook, Booking Page, Stock Tracker.',
  'Three carry $4.99 one-time Pro unlocks.',
  '',
  'Appendix — full repo estate',
  'This is every public repo under jasethepeople.',
  'molt-forge — Python',
  'other-repo — TypeScript',
].join('\n');

test('round 3: pasted line-oriented map gives 6 machines and keeps service bullets out of machines', () => {
  const parsed = parseNetworkMap(PASTED_SHAPE, 'pasted-shape.txt');
  assert.equal(parsed.machines.length, 6);
  assert.equal(parsed.machines.some((machine) => machine.name === '100.83.138.76'), false);
  assert.equal(parsed.machines.some((machine) => machine.name === 'Supabase — The Bet'), false);
  assert.ok(parsed.services.some((service) => service.name === 'Supabase — The Bet'));
  assert.equal(parsed.accounts.length, 1);
});

test('round 3: all three stated ports are captured and joined to their machines', () => {
  const parsed = parseNetworkMap(PASTED_SHAPE, 'pasted-shape.txt');
  assert.equal(parsed.ports.length, 3);
  assert.ok(parsed.ports.some((port) => port.port === 3000 && port.machineId === 'oracle-vm-foundry'));
  assert.ok(parsed.ports.some((port) => port.port === 3100 && port.machineId === 'oracle-vm-foundry'));
  assert.ok(parsed.ports.some((port) => port.port === 8471 && port.machineId === 'acer'));
});

test('round 3: status legend is reference text and only the five named items remain gaps', () => {
  const parsed = parseNetworkMap(PASTED_SHAPE, 'pasted-shape.txt');
  assert.equal(parsed.references.length, 10);
  assert.equal(parsed.unparsed.length, 5);
  assert.deepEqual(parsed.unparsed.map((gap) => gap.text.split(/[—–]/, 1)[0]!.trim()), [
    'moltcoin',
    'molt-verify',
    '23 single-user browser apps, built and wrapped.',
    'Ledger Notebook, Booking Page, Stock Tracker.',
    'Three carry $4.99 one-time Pro unlocks.',
  ]);
});

test('round 3: separate repository aliases remain separate while repeated site mentions merge', () => {
  const parsed = parseNetworkMap(PASTED_SHAPE, 'pasted-shape.txt');
  assert.ok(parsed.services.some((service) => service.name === 'Echoes / other-repo'));
  assert.ok(parsed.services.some((service) => service.name === 'other-repo'));
});
