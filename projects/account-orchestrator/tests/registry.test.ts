import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import Database from 'better-sqlite3';
import { parseNetworkMap } from '../src/parse/map.js';

const ROOT = process.cwd();
const FIXTURE = join(ROOT, 'fixtures', 'sample-network-map.md');

function run(script: 'seed' | 'verify' | 'cli', args: string[]): { status: number; stdout: string; stderr: string } {
  const result = spawnSync('node', [`dist/src/${script}.js`, ...args], { cwd: ROOT, encoding: 'utf8' });
  return { status: result.status ?? -1, stdout: result.stdout, stderr: result.stderr };
}

function tempDir(): string {
  return mkdtempSync(join(tmpdir(), 'ao-test-'));
}

function dump(dbPath: string): string {
  const db = new Database(dbPath, { readonly: true });
  try {
    const tables = ['machine', 'account', 'service', 'port', 'account_link'];
    return JSON.stringify(
      tables.map((table) => db.prepare(`SELECT * FROM ${table} ORDER BY rowid`).all()),
    );
  } finally {
    db.close();
  }
}

/* ------------------------------------------------------------------ */

test('a purpose the map does not state stays "unknown"', () => {
  const parsed = parseNetworkMap(
    [
      '## Accounts',
      '| Provider | Login | Purpose | Status |',
      '| --- | --- | --- | --- |',
      '| Thing | u@example.com | | |',
      '| Other | v@example.com | | active |',
    ].join('\n'),
    'inline.md',
  );
  assert.equal(parsed.accounts.length, 2);
  const thing = parsed.accounts.find((account) => account.provider === 'Thing')!;
  const other = parsed.accounts.find((account) => account.provider === 'Other')!;
  assert.equal(thing.purpose, 'unknown');
  assert.equal(thing.status, 'unknown');
  assert.equal(other.purpose, 'unknown');
  assert.equal(other.status, 'live');
});

test('duplicate map entries merge into one account and record a warning', () => {
  const parsed = parseNetworkMap(
    [
      '## Accounts',
      '| Provider | Login | Purpose | Status |',
      '| --- | --- | --- | --- |',
      '| GitHub | u@example.com | | live |',
      '| GitHub | u@example.com | code | live |',
    ].join('\n'),
    'inline.md',
  );
  assert.equal(parsed.accounts.length, 1);
  assert.equal(parsed.accounts[0]!.purpose, 'code', 'the stated purpose should fill the blank');
  assert.equal(parsed.warnings.length, 1);
});

test('unplaceable map material is reported, not dropped silently', () => {
  const parsed = parseNetworkMap(
    ['## Odds and ends', '- a thing nobody classified', '```yaml', 'a: b', '```'].join('\n'),
    'inline.md',
  );
  assert.ok(parsed.unparsed.length >= 2);
  for (const gap of parsed.unparsed) assert.match(gap.sourceRef, /^inline\.md:\d+$/);
});

test('seed refuses a map containing a credential', () => {
  const dir = tempDir();
  const map = join(dir, 'leaky.md');
  writeFileSync(map, ['## Accounts', '', '- GitHub: u@example.com password: hunter2hunter2'].join('\n'));
  const result = run('seed', ['--map', map, '--db', join(dir, 'r.sqlite')]);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /REFUSING TO SEED/);
});

test('seeding the same map twice produces an identical registry', () => {
  const dir = tempDir();
  const dbPath = join(dir, 'registry.sqlite');
  const first = run('seed', ['--map', FIXTURE, '--db', dbPath]);
  assert.equal(first.status, 0, first.stderr);
  const before = dump(dbPath);
  const second = run('seed', ['--map', FIXTURE, '--db', dbPath]);
  assert.equal(second.status, 0, second.stderr);
  assert.equal(dump(dbPath), before);
});

test('verify passes against the map and reports every account resolved', () => {
  const dir = tempDir();
  const dbPath = join(dir, 'registry.sqlite');
  assert.equal(run('seed', ['--map', FIXTURE, '--db', dbPath]).status, 0);
  const result = run('verify', ['--db', dbPath, '--map', FIXTURE, '--no-report']);
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stdout, /Verdict: PASS/);
  assert.match(result.stdout, /accounts-resolve\s+hard\s+PASS/);
});

test('verify cannot claim a pass without the map', () => {
  const dir = tempDir();
  const dbPath = join(dir, 'registry.sqlite');
  assert.equal(run('seed', ['--map', FIXTURE, '--db', dbPath]).status, 0);
  const result = run('verify', ['--db', dbPath, '--map', join(dir, 'missing.md'), '--no-report']);
  assert.equal(result.status, 2);
  assert.match(result.stdout, /NOT VERIFIED/);
});

test('the CLI answers the questions Phase 1 is judged on', () => {
  const dir = tempDir();
  const dbPath = join(dir, 'registry.sqlite');
  assert.equal(run('seed', ['--map', FIXTURE, '--db', dbPath]).status, 0);

  const accounts = run('cli', ['accounts', '--db', dbPath, '--limit', '100']);
  assert.equal(accounts.status, 0);
  assert.match(accounts.stdout, /12 shown/);

  const one = run('cli', ['account', 'github:fixture-user-example-com', '--db', dbPath, '--json']);
  assert.equal(one.status, 0);
  const payload = JSON.parse(one.stdout) as { account: { purpose: string; secret_ref: string } };
  assert.equal(payload.account.purpose, 'code hosting, 290 repos');
  assert.equal(payload.account.secret_ref, 'env:GITHUB_TOKEN');

  const gaps = run('cli', ['gaps', '--db', dbPath]);
  assert.match(gaps.stdout, /could not place/);

  const notFound = run('cli', ['account', 'nobody-at-all', '--db', dbPath]);
  assert.equal(notFound.status, 1);
});

test('the CLI opens the registry read-only', () => {
  const dir = tempDir();
  const dbPath = join(dir, 'registry.sqlite');
  assert.equal(run('seed', ['--map', FIXTURE, '--db', dbPath]).status, 0);
  const db = new Database(dbPath, { readonly: true, fileMustExist: true });
  assert.throws(() => db.prepare("INSERT INTO account (id, provider, login, purpose, status) VALUES ('x','y','z','p','live')").run());
  db.close();
});

/* ------------------------------------------------------------------ */
/* Defect 5: verify must not pass vacuously                            */
/* ------------------------------------------------------------------ */

test('defect 5: verify fails on an empty map instead of passing 0 = 0', () => {
  const dir = tempDir();
  const dbPath = join(dir, 'registry.sqlite');
  assert.equal(run('seed', ['--map', FIXTURE, '--db', dbPath]).status, 0);

  const emptyMap = join(dir, 'empty.md');
  writeFileSync(emptyMap, '');
  const result = run('verify', ['--db', dbPath, '--map', emptyMap, '--no-report']);
  assert.equal(result.status, 1, 'an empty map cannot pass');
  assert.match(result.stdout, /extraction-nonzero\s+hard\s+FAIL/);
  assert.match(result.stdout, /Verdict: FAIL/);
});

test('defect 5: verify fails when the map is prose the parser cannot read', () => {
  const dir = tempDir();
  const dbPath = join(dir, 'registry.sqlite');
  assert.equal(run('seed', ['--map', FIXTURE, '--db', dbPath]).status, 0);

  const proseMap = join(dir, 'prose.md');
  writeFileSync(proseMap, '# Notes\n\nThis map was rewritten in a shape the parser does not know.\n');
  const result = run('verify', ['--db', dbPath, '--map', proseMap, '--no-report']);
  assert.equal(result.status, 1);
  assert.match(result.stdout, /nothing extracted for/);
});

test('seed refuses to erase a populated registry with an unreadable map', () => {
  const dir = tempDir();
  const dbPath = join(dir, 'registry.sqlite');
  assert.equal(run('seed', ['--map', FIXTURE, '--db', dbPath]).status, 0);

  const emptyMap = join(dir, 'empty.md');
  writeFileSync(emptyMap, '');
  const refused = run('seed', ['--map', emptyMap, '--db', dbPath]);
  assert.equal(refused.status, 1);
  assert.match(refused.stderr, /REFUSING TO SEED/);

  const stillThere = run('cli', ['accounts', '--db', dbPath, '--limit', '100']);
  assert.match(stillThere.stdout, /12 shown/, 'the registry is untouched');

  const forced = run('seed', ['--map', emptyMap, '--db', dbPath, '--allow-empty']);
  assert.equal(forced.status, 0);
});

test('the production-map shape fixture seeds and verifies end to end', () => {
  const dir = tempDir();
  const dbPath = join(dir, 'registry.sqlite');
  const shape = join(ROOT, 'fixtures', 'real-map-shape.md');

  const seed = run('seed', ['--map', shape, '--db', dbPath]);
  assert.equal(seed.status, 0, seed.stderr);

  const db = new Database(dbPath, { readonly: true });
  const counts = {
    accounts: (db.prepare('SELECT COUNT(*) AS n FROM account').get() as { n: number }).n,
    machines: (db.prepare('SELECT COUNT(*) AS n FROM machine').get() as { n: number }).n,
    ports: (db.prepare('SELECT COUNT(*) AS n FROM port').get() as { n: number }).n,
    urls: (db.prepare('SELECT COUNT(*) AS n FROM service WHERE url IS NOT NULL').get() as { n: number }).n,
    gmail: (db.prepare("SELECT COUNT(*) AS n FROM account WHERE provider = 'Gmail'").get() as { n: number }).n,
  };
  db.close();

  assert.ok(counts.accounts >= 10, `accounts: ${counts.accounts}`);
  assert.equal(counts.machines, 11);
  assert.equal(counts.gmail, 7, 'all seven Gmail rows resolve');
  assert.ok(counts.ports >= 10, `ports: ${counts.ports}`);
  assert.ok(counts.urls >= 12, `services with a URL: ${counts.urls}`);

  const verify = run('verify', ['--db', dbPath, '--map', shape, '--no-report']);
  assert.equal(verify.status, 0, verify.stdout + verify.stderr);
  assert.match(verify.stdout, /Verdict: PASS/);
  assert.doesNotMatch(verify.stdout, /FAIL/);
});
