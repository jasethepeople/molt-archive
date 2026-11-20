#!/usr/bin/env node
/**
 * seed — writes the map-derived registry snapshot.
 *
 * Reads the network map, refuses to proceed if it finds anything that looks like
 * a credential, then rebuilds the registry as a deterministic snapshot of the
 * map inside one transaction. Re-seeding the same map produces identical ids and
 * identical counts; health checks write only the separate live-health columns.
 */
import { createHash } from 'node:crypto';
import { readFileSync, mkdirSync, writeFileSync, existsSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { openForWrite, openReadOnly, PROJECT_ROOT, defaultDbPath, setMeta, tableCounts } from './db.js';
import { parseNetworkMap } from './parse/map.js';
import { blockingFindings } from './secrets.js';
import type { ParsedMap } from './types.js';

interface Options {
  map: string;
  db: string;
  dryRun: boolean;
  json: boolean;
  allowEmpty: boolean;
}

function usage(): never {
  console.error(`Usage: seed --map <network-map.md> [options]

  --map <path>   Network map to read (required)
  --db <path>    Registry file (default: data/registry.sqlite, or $AO_DB)
  --dry-run      Parse and validate, print the summary, write nothing
  --allow-empty  Permit a map that yields nothing (destroys the registry)
  --json         Machine-readable summary
  --help         This text`);
  process.exit(1);
}

function parseArgs(argv: string[]): Options {
  const options: Options = { map: '', db: defaultDbPath(), dryRun: false, json: false, allowEmpty: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    switch (arg) {
      case '--map':
        options.map = argv[++i] ?? '';
        break;
      case '--db':
        options.db = argv[++i] ?? options.db;
        break;
      case '--dry-run':
        options.dryRun = true;
        break;
      case '--allow-empty':
        options.allowEmpty = true;
        break;
      case '--json':
        options.json = true;
        break;
      case '--help':
      case '-h':
        usage();
        break;
      default:
        if (arg?.startsWith('--map=')) options.map = arg.slice(6);
        else if (arg?.startsWith('--db=')) options.db = arg.slice(5);
        else usage();
    }
  }
  if (!options.map) usage();
  return options;
}

/** Post-parse validation: the things SQLite will not catch for us. */
function validate(parsed: ParsedMap): string[] {
  const problems: string[] = [];
  const machineIds = new Set(parsed.machines.map((m) => m.id));
  const accountIds = new Set(parsed.accounts.map((a) => a.id));
  const serviceIds = new Set(parsed.services.map((s) => s.id));

  for (const port of parsed.ports) {
    if (!machineIds.has(port.machineId)) problems.push(`${port.sourceRef}: port ${port.id} references unknown machine ${port.machineId}`);
    if (port.serviceId && !serviceIds.has(port.serviceId)) problems.push(`${port.sourceRef}: port ${port.id} references unknown service ${port.serviceId}`);
  }
  for (const service of parsed.services) {
    if (service.accountId && !accountIds.has(service.accountId)) problems.push(`${service.sourceRef}: service ${service.id} references unknown account ${service.accountId}`);
    if (service.machineId && !machineIds.has(service.machineId)) problems.push(`${service.sourceRef}: service ${service.id} references unknown machine ${service.machineId}`);
  }
  for (const account of parsed.accounts) {
    if (!account.purpose || account.purpose.trim() === '') problems.push(`${account.sourceRef}: account ${account.id} has an empty purpose (should be "unknown")`);
    if (!account.login.trim()) problems.push(`${account.sourceRef}: account ${account.id} has an empty login`);
  }
  for (const link of parsed.links) {
    if (!accountIds.has(link.fromAccount) || !accountIds.has(link.toAccount)) problems.push(`${link.sourceRef}: link ${link.fromAccount} -> ${link.toAccount} references an unknown account`);
  }
  return problems;
}

function main(): void {
  const options = parseArgs(process.argv.slice(2));
  const mapPath = resolve(options.map);

  let text: string;
  try {
    text = readFileSync(mapPath, 'utf8');
  } catch (error) {
    console.error(`Cannot read map: ${mapPath}\n${(error as Error).message}`);
    process.exit(1);
  }

  // 1. Nothing that looks like a credential may even be parsed, let alone stored.
  const secretHits: { line: number; code: string; excerpt: string }[] = [];
  text.split('\n').forEach((line, index) => {
    for (const finding of blockingFindings(line)) {
      secretHits.push({ line: index + 1, code: finding.code, excerpt: finding.excerpt });
    }
  });
  if (secretHits.length > 0) {
    console.error('REFUSING TO SEED: the map contains credential-looking text.');
    console.error('Replace each value with a reference (for example `env:GITHUB_TOKEN`).');
    for (const hit of secretHits.slice(0, 25)) {
      console.error(`  ${options.map}:${hit.line}  ${hit.code}  ${hit.excerpt}`);
    }
    if (secretHits.length > 25) console.error(`  ... and ${secretHits.length - 25} more`);
    process.exit(1);
  }

  // 2. Parse.
  const parsed = parseNetworkMap(text, mapPath);
  const problems = validate(parsed);
  if (problems.length > 0) {
    console.error('REFUSING TO SEED: the parsed map is internally inconsistent.');
    for (const problem of problems.slice(0, 25)) console.error(`  ${problem}`);
    if (problems.length > 25) console.error(`  ... and ${problems.length - 25} more`);
    process.exit(1);
  }

  // 3. Never replace a populated registry with nothing: a map the parser cannot
  // read is a bug or the wrong file, not an empty account list.
  const empty = parsed.accounts.length === 0 && parsed.machines.length === 0 && parsed.services.length === 0;
  if (empty && !options.allowEmpty && existsSync(options.db)) {
    let stored = 0;
    try {
      const existing = openReadOnly(options.db);
      stored = (existing.prepare('SELECT COUNT(*) AS n FROM account').get() as { n: number }).n;
      existing.close();
    } catch {
      stored = 0;
    }
    if (stored > 0) {
      console.error('REFUSING TO SEED: the map produced no accounts, machines, or services.');
      console.error(`The registry at ${options.db} holds ${stored} account(s); this map would erase them.`);
      console.error('Use --dry-run to inspect, `ao gaps` for what was skipped, or --allow-empty to overwrite.');
      process.exit(1);
    }
  }
  if (empty) {
    console.error('WARNING: the map produced no accounts, machines, or services.');
  }

  const mapSha = createHash('sha256').update(text).digest('hex');
  const summary = {
    map: options.map,
    sha256: mapSha.slice(0, 12),
    machines: parsed.machines.length,
    accounts: parsed.accounts.length,
    services: parsed.services.length,
    ports: parsed.ports.length,
    links: parsed.links.length,
    gaps: parsed.unparsed.length,
    warnings: parsed.warnings.length,
    references: parsed.references.length,
    db: options.db,
    dryRun: options.dryRun,
  };

  if (options.dryRun) {
    emit(summary, parsed, options.json);
    return;
  }

  // 3. Rebuild the registry as a snapshot of the map.
  const db = openForWrite(options.db);
  try {
    const insertMachine = db.prepare(
      `INSERT INTO machine (id, name, tailnet_ip, os, role, status, source_ref, notes)
       VALUES (@id, @name, @tailnetIp, @os, @role, @status, @sourceRef, @notes)`,
    );
    const insertAccount = db.prepare(
      `INSERT INTO account (id, provider, login, purpose, status, secret_ref, source_ref, notes)
       VALUES (@id, @provider, @login, @purpose, @status, @secretRef, @sourceRef, @notes)`,
    );
    const insertService = db.prepare(
      `INSERT INTO service (id, name, kind, account_id, url, status, machine_id, source_ref, notes)
       VALUES (@id, @name, @kind, @accountId, @url, @status, @machineId, @sourceRef, @notes)`,
    );
    const insertPort = db.prepare(
      `INSERT INTO port (id, service_id, machine_id, port, protocol, health, source_ref)
       VALUES (@id, @serviceId, @machineId, @port, @protocol, @health, @sourceRef)`,
    );
    const insertLink = db.prepare(
      `INSERT INTO account_link (from_account, to_account, relation, source_ref)
       VALUES (@fromAccount, @toAccount, @relation, @sourceRef)`,
    );
    const insertNote = db.prepare('INSERT INTO import_note (level, source_ref, text, reason) VALUES (?, ?, ?, ?)');

    db.transaction(() => {
      db.exec('DELETE FROM account_link; DELETE FROM port; DELETE FROM service; DELETE FROM account; DELETE FROM machine; DELETE FROM import_note;');

      for (const machine of parsed.machines) {
        insertMachine.run({
          id: machine.id,
          name: machine.name,
          tailnetIp: machine.tailnetIp ?? null,
          os: machine.os ?? null,
          role: machine.role ?? null,
          status: machine.status,
          sourceRef: machine.sourceRef,
          notes: machine.notes ?? null,
        });
      }
      for (const account of parsed.accounts) {
        insertAccount.run({
          id: account.id,
          provider: account.provider,
          login: account.login,
          purpose: account.purpose,
          status: account.status,
          secretRef: account.secretRef ?? null,
          sourceRef: account.sourceRef,
          notes: account.notes ?? null,
        });
      }
      for (const service of parsed.services) {
        insertService.run({
          id: service.id,
          name: service.name,
          kind: service.kind,
          accountId: service.accountId ?? null,
          url: service.url ?? null,
          status: service.status,
          machineId: service.machineId ?? null,
          sourceRef: service.sourceRef,
          notes: service.notes ?? null,
        });
      }
      for (const port of parsed.ports) {
        insertPort.run({
          id: port.id,
          serviceId: port.serviceId ?? null,
          machineId: port.machineId,
          port: port.port,
          protocol: port.protocol,
          health: port.health,
          sourceRef: port.sourceRef,
        });
      }
      for (const link of parsed.links) {
        insertLink.run({
          fromAccount: link.fromAccount,
          toAccount: link.toAccount,
          relation: link.relation,
          sourceRef: link.sourceRef,
        });
      }
      for (const gap of parsed.unparsed) insertNote.run('gap', gap.sourceRef, gap.text, gap.reason);
      for (const warning of parsed.warnings) insertNote.run('warning', 'parse', warning, warning);
      for (const reference of parsed.references) insertNote.run('reference', reference.sourceRef, reference.text, reference.reason);

      setMeta(db, 'source_map_path', mapPath);
      setMeta(db, 'source_map_sha256', mapSha);
      setMeta(db, 'seeded_at', new Date().toISOString());
    })();

    const counts = tableCounts(db);
    // 4. Post-write guard: scan every stored string. Belt and braces.
    const stored = [
      ...db.prepare('SELECT id, provider, login, purpose, secret_ref AS s FROM account').all() as any[],
      ...db.prepare('SELECT id, name, url AS s FROM service').all() as any[],
    ];
    const leaks = stored.flatMap((row) =>
      Object.values(row)
        .filter((value): value is string => typeof value === 'string')
        .flatMap((value) => blockingFindings(value)),
    );
    if (leaks.length > 0) {
      throw new Error(`post-write secret scan failed (${leaks.length} hit(s)) — registry left as-is for inspection`);
    }

    // 5. Keep a machine-readable copy of the seed result next to the data.
    const reportDir = join(PROJECT_ROOT, 'data');
    mkdirSync(reportDir, { recursive: true });
    writeFileSync(
      join(reportDir, 'seed-report.json'),
      JSON.stringify({ ...summary, counts, gaps: parsed.unparsed, warnings: parsed.warnings }, null, 2),
    );

    emit({ ...summary, counts }, parsed, options.json);
  } finally {
    db.close();
  }
}

function emit(summary: Record<string, unknown>, parsed: ParsedMap, json: boolean): void {
  if (json) {
    console.log(JSON.stringify({ summary, gaps: parsed.unparsed, warnings: parsed.warnings }, null, 2));
    if (parsed.unparsed.length === 0) return;
  }
  if (!json) {
    const dbRel = relative(PROJECT_ROOT, String(summary['db'] ?? '')) || String(summary['db'] ?? '');
    console.log(`${summary['dryRun'] ? 'DRY RUN — nothing written' : 'Registry rebuilt'}: ${dbRel}`);
    console.log(`  map      ${summary['map']}  (sha256 ${summary['sha256']}…)`);
    console.log(`  machines ${summary['machines']}`);
    console.log(`  accounts ${summary['accounts']}`);
    console.log(`  services ${summary['services']}`);
    console.log(`  ports    ${summary['ports']}`);
    console.log(`  links    ${summary['links']}`);
    console.log(`  gaps     ${summary['gaps']}   (map material the parser could not place — see "ao gaps")`);
    console.log(`  warnings ${summary['warnings']}`);
    console.log(`  references ${summary['references']}  (status keys and notes kept as text — see "ao reference")`);
    if (summary['accounts'] === 0) {
      console.log('\nNo accounts were found. The map does not look like any shape the parser knows.');
      console.log('See docs/MAP-FORMAT.md, or run `ao gaps` after seeding to see what was skipped.');
    }
    if (parsed.unparsed.length > 0) {
      console.log('\nFirst gaps:');
      for (const gap of parsed.unparsed.slice(0, 10)) console.log(`  ${gap.sourceRef}  ${gap.reason}`);
    }
  } else if (parsed.unparsed.length > 0) {
    // json mode with gaps: the JSON above already carries them.
  }
}

main();
