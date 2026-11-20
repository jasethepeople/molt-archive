#!/usr/bin/env node
/**
 * verify — reconcile the registry against the map it came from.
 *
 * Phase 1's exit criterion is one sentence: every account in the network map
 * resolves to a record. This command is the evidence for that sentence, plus a
 * set of structural checks that do not need the map at all.
 *
 * Exit codes: 0 = every hard check passed, 1 = a hard check failed,
 * 2 = the map was not available, so reconciliation could not be performed.
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { basename, join, resolve } from 'node:path';
import { defaultDbPath, getMeta, openReadOnly, PROJECT_ROOT, SCHEMA_VERSION } from './db.js';
import { parseNetworkMap } from './parse/map.js';
import { blockingFindings } from './secrets.js';
import type { ParsedMap } from './types.js';
import { table } from './format.js';

type CheckStatus = 'pass' | 'fail' | 'warn' | 'skipped';

interface Check {
  id: string;
  title: string;
  severity: 'hard' | 'soft';
  status: CheckStatus;
  detail: string;
  items?: string[];
}

interface AccountRow {
  id: string;
  provider: string;
  login: string;
  purpose: string;
  status: string;
  secret_ref: string | null;
  source_ref: string | null;
}

interface Options {
  map?: string;
  db: string;
  json: boolean;
  out?: string;
  noReport: boolean;
}

function parseArgs(argv: string[]): Options {
  const options: Options = { db: defaultDbPath(), json: false, noReport: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--map') options.map = argv[++i];
    else if (arg?.startsWith('--map=')) options.map = arg.slice(6);
    else if (arg === '--db') options.db = argv[++i] ?? options.db;
    else if (arg?.startsWith('--db=')) options.db = arg.slice(5);
    else if (arg === '--out') options.out = argv[++i];
    else if (arg === '--json') options.json = true;
    else if (arg === '--no-report') options.noReport = true;
    else if (arg === '--help' || arg === '-h') {
      console.log(`Usage: verify [--map <network-map.md>] [--db <path>] [--out <report.md>] [--json] [--no-report]

With --map (or the path remembered at seed time) the registry is reconciled
against the map. Without it, only structural checks run and the exit code is 2.`);
      process.exit(0);
    }
  }
  return options;
}

/* ------------------------------------------------------------------ */

const TEXT_TABLES = ['machine', 'account', 'service', 'port', 'account_link', 'import_note', 'meta'];

function scanStoredText(db: ReturnType<typeof openReadOnly>): { count: number; hits: string[] } {
  const hits: string[] = [];
  let count = 0;
  for (const tableName of TEXT_TABLES) {
    const exists = db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(tableName);
    if (!exists) continue;
    const columns = db.prepare(`PRAGMA table_info(${tableName})`).all() as { name: string; type: string }[];
    const textColumns = columns.filter((column) => /TEXT/i.test(column.type) || column.type === '').map((column) => column.name);
    if (textColumns.length === 0) continue;
    const rows = db.prepare(`SELECT rowid AS __rowid, ${textColumns.join(', ')} FROM ${tableName}`).all() as Record<string, unknown>[];
    for (const row of rows) {
      count += 1;
      for (const column of textColumns) {
        const value = row[column];
        if (typeof value !== 'string') continue;
        const findings = blockingFindings(value);
        if (findings.length > 0) hits.push(`${tableName}.${column} rowid=${row['__rowid']}: ${findings[0]!.code}`);
      }
    }
  }
  return { count, hits };
}

function reconcile(parsed: ParsedMap, accounts: AccountRow[]): Check[] {
  const checks: Check[] = [];

  // 0 == 0 is not a pass. Without this, a parser that reads nothing reconciles
  // perfectly against a registry built from the same nothing.
  const zeroes: string[] = [];
  if (parsed.accounts.length === 0) zeroes.push('accounts');
  if (parsed.machines.length === 0) zeroes.push('machines');
  if (parsed.ports.length === 0) zeroes.push('ports');
  checks.push({
    id: 'extraction-nonzero',
    title: 'The map yielded accounts, machines and ports',
    severity: 'hard',
    status: zeroes.length === 0 ? 'pass' : 'fail',
    detail:
      zeroes.length === 0
        ? `${parsed.accounts.length} account(s), ${parsed.machines.length} machine(s), ${parsed.ports.length} port(s) extracted from the map`
        : `nothing extracted for: ${zeroes.join(', ')} — the map was not read`,
    items: zeroes.map((what) => `no ${what} extracted from ${parsed.sourcePath}`),
  });

  const byId = new Map(accounts.map((a) => [a.id, a]));
  const byKey = new Map(accounts.map((a) => [`${a.provider.toLowerCase()}|${a.login.toLowerCase()}`, a]));

  const missing: string[] = [];
  const matched: { mapId: string; recordId: string }[] = [];
  for (const account of parsed.accounts) {
    const record = byId.get(account.id) ?? byKey.get(`${account.provider.toLowerCase()}|${account.login.toLowerCase()}`);
    if (record) matched.push({ mapId: account.id, recordId: record.id });
    else missing.push(`${account.sourceRef}  ${account.provider} / ${account.login}  (expected id ${account.id})`);
  }

  checks.push({
    id: 'accounts-resolve',
    title: 'Every account in the map resolves to a registry record',
    severity: 'hard',
    status: missing.length === 0 && parsed.accounts.length === accounts.length ? 'pass' : 'fail',
    detail: `${matched.length}/${parsed.accounts.length} map accounts resolved; ${accounts.length} records in the registry`,
    items: missing,
  });

  const extras = accounts
    .filter((record) => !parsed.accounts.some((account) => account.id === record.id))
    .map((record) => `${record.id} (${record.provider} / ${record.login})`);
  checks.push({
    id: 'accounts-no-extras',
    title: 'The registry holds no accounts the map does not mention',
    severity: 'soft',
    status: extras.length === 0 ? 'pass' : 'warn',
    detail: extras.length === 0 ? 'none' : `${extras.length} record(s) not traceable to the map`,
    items: extras,
  });

  return checks;
}

function structuralChecks(db: ReturnType<typeof openReadOnly>, accounts: AccountRow[]): Check[] {
  const checks: Check[] = [];

  const version = getMeta(db, 'schema_version');
  checks.push({
    id: 'schema-version',
    title: 'Schema version matches this build',
    severity: 'hard',
    status: version === SCHEMA_VERSION ? 'pass' : 'fail',
    detail: `registry has ${version ?? 'none'}, this build expects ${SCHEMA_VERSION}`,
  });

  const fk = db.prepare('PRAGMA foreign_key_check').all() as unknown[];
  checks.push({
    id: 'foreign-keys',
    title: 'No dangling foreign keys',
    severity: 'hard',
    status: fk.length === 0 ? 'pass' : 'fail',
    detail: fk.length === 0 ? 'clean' : `${fk.length} violation(s)`,
    items: fk.map((row) => JSON.stringify(row)),
  });

  const counts = db
    .prepare('SELECT (SELECT COUNT(*) FROM account) AS a, (SELECT COUNT(*) FROM machine) AS m, (SELECT COUNT(*) FROM port) AS p')
    .get() as { a: number; m: number; p: number };
  const emptyParts: string[] = [];
  if (counts.a === 0) emptyParts.push('accounts');
  if (counts.m === 0) emptyParts.push('machines');
  if (counts.p === 0) emptyParts.push('ports');
  checks.push({
    id: 'registry-nonzero',
    title: 'The registry itself holds accounts, machines and ports',
    severity: 'hard',
    status: emptyParts.length === 0 ? 'pass' : 'fail',
    detail:
      emptyParts.length === 0
        ? `${counts.a} account(s), ${counts.m} machine(s), ${counts.p} port(s) stored`
        : `the registry is empty for: ${emptyParts.join(', ')} — seed it from the map first`,
  });

  const scanned = scanStoredText(db);
  checks.push({
    id: 'no-secrets',
    title: 'No credential values stored anywhere',
    severity: 'hard',
    status: scanned.hits.length === 0 ? 'pass' : 'fail',
    detail:
      scanned.hits.length === 0
        ? `${scanned.count} row(s) scanned across all text columns; no credential patterns found`
        : `${scanned.hits.length} hit(s)`,
    items: scanned.hits,
  });

  const badPurpose = db.prepare("SELECT id FROM account WHERE purpose IS NULL OR trim(purpose) = ''").all() as { id: string }[];
  checks.push({
    id: 'purpose-not-empty',
    title: 'Every account has a purpose ("unknown" is a valid answer)',
    severity: 'hard',
    status: badPurpose.length === 0 ? 'pass' : 'fail',
    detail: badPurpose.length === 0 ? `${accounts.length} account(s) checked` : `${badPurpose.length} empty`,
    items: badPurpose.map((row) => row.id),
  });

  const badStatus = db
    .prepare("SELECT id, status FROM account WHERE status NOT IN ('live','dead','unknown')")
    .all() as { id: string; status: string }[];
  checks.push({
    id: 'status-vocabulary',
    title: 'Statuses stay inside live / dead / unknown',
    severity: 'hard',
    status: badStatus.length === 0 ? 'pass' : 'fail',
    detail: badStatus.length === 0 ? 'clean' : badStatus.map((row) => `${row.id}=${row.status}`).join(', '),
  });

  const dupes = db
    .prepare('SELECT provider, login, COUNT(*) AS n FROM account GROUP BY provider, login HAVING n > 1')
    .all() as { provider: string; login: string; n: number }[];
  checks.push({
    id: 'account-uniqueness',
    title: 'No duplicate provider + login pairs',
    severity: 'hard',
    status: dupes.length === 0 ? 'pass' : 'fail',
    detail: dupes.length === 0 ? 'clean' : `${dupes.length} duplicate pair(s)`,
    items: dupes.map((row) => `${row.provider} / ${row.login} × ${row.n}`),
  });

  const noSecretRef = db.prepare('SELECT COUNT(*) AS n FROM account WHERE secret_ref IS NULL').get() as { n: number };
  checks.push({
    id: 'secret-refs',
    title: 'Accounts with no recorded secret location',
    severity: 'soft',
    status: noSecretRef.n === 0 ? 'pass' : 'warn',
    detail: `${noSecretRef.n} of ${accounts.length} — expected in Phase 1: the map does not say where credentials live`,
  });

  const orphanServices = db
    .prepare('SELECT id FROM service WHERE machine_id IS NULL AND kind <> \'repo\'')
    .all() as { id: string }[];
  checks.push({
    id: 'service-location',
    title: 'Services that are not placed on a machine',
    severity: 'soft',
    status: orphanServices.length === 0 ? 'pass' : 'warn',
    detail: `${orphanServices.length} service(s) with no machine`,
    items: orphanServices.map((row) => row.id),
  });

  const gaps = db.prepare('SELECT COUNT(*) AS n FROM import_note WHERE level = ?').get('gap') as { n: number };
  checks.push({
    id: 'import-gaps',
    title: 'Map material the parser could not place',
    severity: 'soft',
    status: gaps.n === 0 ? 'pass' : 'warn',
    detail: gaps.n === 0 ? 'none' : `${gaps.n} gap(s) — listed by "ao gaps"`,
  });

  return checks;
}

/* ------------------------------------------------------------------ */

function markdown(checkList: Check[], accounts: AccountRow[], meta: Record<string, string | undefined>, mapPath?: string): string {
  const hardFails = checkList.filter((c) => c.severity === 'hard' && c.status === 'fail');
  const verdict = hardFails.length === 0 ? 'PASS' : 'FAIL';
  const lines: string[] = [];
  lines.push('# Phase 1 verification report — Account Orchestrator');
  lines.push('');
  lines.push(`Generated: ${new Date().toISOString()}`);
  lines.push(`Registry: \`${meta['db'] ?? 'unknown'}\``);
  lines.push(`Map: ${mapPath ? `\`${basename(mapPath)}\` (sha256 ${meta['source_map_sha256']?.slice(0, 12) ?? 'n/a'}…)` : '**not supplied — reconciliation skipped**'}`);
  lines.push(`Seeded: ${meta['seeded_at'] ?? 'unknown'}`);
  lines.push('');
  lines.push(`## Verdict: ${verdict}`);
  lines.push('');
  lines.push(
    hardFails.length === 0
      ? `All ${checkList.filter((c) => c.severity === 'hard').length} hard checks passed.`
      : `${hardFails.length} hard check(s) failed: ${hardFails.map((c) => c.id).join(', ')}.`,
  );
  lines.push('');
  lines.push('## Checks');
  lines.push('');
  lines.push('| # | Check | Severity | Result | Detail |');
  lines.push('| --- | --- | --- | --- | --- |');
  checkList.forEach((c, index) => {
    lines.push(`| ${index + 1} | ${c.title} | ${c.severity} | ${c.status.toUpperCase()} | ${c.detail.replace(/\|/g, '\\|')} |`);
  });
  lines.push('');

  const failing = checkList.filter((c) => c.items && c.items.length > 0);
  if (failing.length > 0) {
    lines.push('## Items behind failing or warning checks');
    lines.push('');
    for (const check of failing) {
      lines.push(`### ${check.id} (${check.status})`);
      lines.push('');
      for (const item of check.items!.slice(0, 100)) lines.push(`- ${item}`);
      if (check.items!.length > 100) lines.push(`- … and ${check.items!.length - 100} more`);
      lines.push('');
    }
  }

  lines.push('## Registry contents');
  lines.push('');
  const byStatus = accounts.reduce<Record<string, number>>((acc, account) => {
    acc[account.status] = (acc[account.status] ?? 0) + 1;
    return acc;
  }, {});
  lines.push(`- Accounts: **${accounts.length}** (${Object.entries(byStatus).map(([k, v]) => `${v} ${k}`).join(', ') || 'none'})`);
  lines.push(`- Machines: ${meta['machines'] ?? '?'}`);
  lines.push(`- Services: ${meta['services'] ?? '?'}`);
  lines.push(`- Ports: ${meta['ports'] ?? '?'}`);
  lines.push('');
  if (accounts.length > 0) {
    lines.push('| Account id | Provider | Login | Purpose | Status | Secret ref | Source |');
    lines.push('| --- | --- | --- | --- | --- | --- | --- |');
    for (const account of accounts) {
      lines.push(
        `| \`${account.id}\` | ${account.provider} | ${account.login} | ${(account.purpose ?? '').replace(/\|/g, '\\|')} | ${account.status} | ${account.secret_ref ?? '—'} | ${account.source_ref ?? '—'} |`,
      );
    }
    lines.push('');
  }
  lines.push('## How to read this');
  lines.push('');
  lines.push('- A **hard** check failing means the registry is wrong; do not build Phase 2 on it.');
  lines.push('- **soft** warnings are expected in Phase 1: the map does not record where secrets live, and some map material is prose the parser will not guess at.');
  lines.push('- `source` is `map.md:line` — every record can be checked against the line it came from.');
  return lines.join('\n');
}

function main(): void {
  const options = parseArgs(process.argv.slice(2));
  const db = openReadOnly(options.db);
  try {
    const accounts = db
      .prepare('SELECT id, provider, login, purpose, status, secret_ref, source_ref FROM account ORDER BY provider, login')
      .all() as AccountRow[];

    const meta: Record<string, string | undefined> = {
      db: options.db,
      schema_version: getMeta(db, 'schema_version'),
      source_map_path: getMeta(db, 'source_map_path'),
      source_map_sha256: getMeta(db, 'source_map_sha256'),
      seeded_at: getMeta(db, 'seeded_at'),
    };
    const counts = db.prepare("SELECT (SELECT COUNT(*) FROM machine) AS m, (SELECT COUNT(*) FROM service) AS s, (SELECT COUNT(*) FROM port) AS p").get() as {
      m: number;
      s: number;
      p: number;
    };
    meta['machines'] = String(counts.m);
    meta['services'] = String(counts.s);
    meta['ports'] = String(counts.p);

    const checks = structuralChecks(db, accounts);

    const mapPath = options.map ?? meta['source_map_path'];
    let parsed: ParsedMap | undefined;
    let reconciled = false;
    if (mapPath && existsSync(mapPath)) {
      parsed = parseNetworkMap(readFileSync(mapPath, 'utf8'), resolve(mapPath));
      checks.unshift(...reconcile(parsed, accounts));
      checks.unshift(
        idSetCheck('machines-resolve', 'Every machine in the map has a record', parsed.machines.map((m) => m.id), db.prepare('SELECT id FROM machine').all().map((r: any) => r.id as string), 'hard'),
        idSetCheck('services-resolve', 'Every service in the map has a record', parsed.services.map((s) => s.id), db.prepare('SELECT id FROM service').all().map((r: any) => r.id as string), 'hard'),
        idSetCheck('ports-resolve', 'Every port in the map has a record', parsed.ports.map((p) => p.id), db.prepare('SELECT id FROM port').all().map((r: any) => r.id as string), 'hard'),
      );
      reconciled = true;
    } else {
      checks.unshift({
        id: 'map-reconciliation',
        title: 'Registry reconciled against the network map',
        severity: 'hard',
        status: 'skipped',
        detail: mapPath ? `map not found at ${mapPath}` : 'no map supplied and none remembered from seeding',
      });
    }

    const hardFails = checks.filter((c) => c.severity === 'hard' && c.status === 'fail');
    const hardPasses = checks.filter((c) => c.severity === 'hard' && c.status === 'pass');
    const skipped = checks.filter((c) => c.status === 'skipped');
    const warnings = checks.filter((c) => c.status === 'warn');

    if (options.json) {
      console.log(JSON.stringify({ reconciled, verdict: hardFails.length === 0 && reconciled ? 'PASS' : hardFails.length > 0 ? 'FAIL' : 'UNVERIFIED', checks, accounts }, null, 2));
    } else {
      console.log('Phase 1 verification — Account Orchestrator');
      console.log(`registry ${options.db}`);
      console.log(`map      ${mapPath ?? '(none)'}`);
      console.log('');
      const rows = checks.map((c, index) => [String(index + 1), c.id, c.severity, c.status.toUpperCase(), c.detail]);
      console.log(
        table(
          [
            { header: '#', max: 2 },
            { header: 'check', max: 24 },
            { header: 'kind', max: 4 },
            { header: 'result', max: 8 },
            { header: 'detail', max: 78 },
          ],
          rows,
        ),
      );
      console.log('');
      console.log(`${hardPasses.length} hard check(s) passed, ${hardFails.length} failed, ${warnings.length} warning(s), ${skipped.length} skipped.`);
      if (!reconciled) {
        console.log('');
        console.log('NOT VERIFIED: the network map was not available, so "every account in the map');
        console.log('resolves to a record" could not be checked. Re-run with --map <path>.');
      }
      const verdict = hardFails.length > 0 ? 'FAIL' : reconciled ? 'PASS' : 'UNVERIFIED';
      console.log(`\nVerdict: ${verdict}`);
    }

    if (!options.noReport) {
      const reportDir = join(PROJECT_ROOT, 'reports');
      mkdirSync(reportDir, { recursive: true });
      const out = options.out ? resolve(options.out) : join(reportDir, `verification-${new Date().toISOString().replace(/[:.]/g, '-')}.md`);
      writeFileSync(out, `${markdown(checks, accounts, meta, reconciled ? mapPath : undefined)}\n`);
      if (!options.json) console.log(`Report: ${out}`);
    }

    process.exitCode = hardFails.length > 0 ? 1 : reconciled ? 0 : 2;
  } finally {
    db.close();
  }
}

main();
function idSetCheck(
  id: string,
  title: string,
  mapIds: string[],
  dbIds: string[],
  severity: 'hard' | 'soft',
): Check {
  const stored = new Set(dbIds);
  const missing = mapIds.filter((value) => !stored.has(value));
  const extras = dbIds.filter((value) => !mapIds.includes(value));
  const clean = missing.length === 0 && extras.length === 0;
  const parts = [`${mapIds.length - missing.length}/${mapIds.length} from the map present in the registry`];
  if (extras.length > 0) parts.push(`${extras.length} not traceable to the map`);
  return {
    id,
    title,
    severity: clean ? severity : severity === 'hard' ? 'hard' : 'soft',
    status: clean ? 'pass' : severity === 'hard' ? 'fail' : 'warn',
    detail: parts.join('; '),
    items: [...missing.map((value) => `missing: ${value}`), ...extras.map((value) => `extra: ${value}`)],
  };
}

