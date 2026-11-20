#!/usr/bin/env node
/**
 * ao — read-only registry queries plus explicit health-check commands.
 *
 * Query commands open SQLite readonly. `seed`, `check`, and the supervised `run`
 * loop are the only write paths.
 */
import { defaultDbPath, getMeta, openReadOnly, SCHEMA_VERSION, type Db } from './db.js';
import { orDash, orUnknown, table } from './format.js';
import { runCheckCycle, type CheckCycleSummary } from './health.js';

interface Flags {
  json: boolean;
  limit: number;
  [key: string]: string | boolean | number;
}

const HELP = `ao — query the account registry (read-only)

  ao stats                          What the registry holds, and where it came from
  ao accounts [filters]             List accounts
      --status live|dead|unknown    --provider <name>    --search <text>
  ao account <id|login|provider>    One account: services, ports, links
  ao services [filters]             List services
      --kind <kind>   --machine <name>   --account <id>
  ao service <id>                   One service and its ports
  ao machines                       List machines
  ao machine <name>                 One machine: services and ports
  ao ports [--machine <name>]       List ports
  ao check [--timeout-ms <n>]       Run one health-check cycle now
  ao run [--interval-ms <n>]        Supervised periodic health checks
  ao live-status                    Show live health beside map status
  ao gaps                           Map material the parser could not place
  ao reference                      Status keys and notes kept as text, not data
  ao warnings                       Merges and inferences worth a look
  ao lookup <text>                  Search across everything

  Global: --json   --db <path>   --limit <n>   --timeout-ms <n>   --interval-ms <n>   --concurrency <n>   --help

Registry: ${defaultDbPath()}`;

function parse(argv: string[]): { command: string; args: string[]; flags: Flags } {
  const flags: Flags = { json: false, limit: 50 };
  const args: string[] = [];
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i]!;
    if (arg === '--json') flags['json'] = true;
    else if (arg === '--limit') flags['limit'] = Number(argv[++i] ?? '50');
    else if (arg.startsWith('--limit=')) flags['limit'] = Number(arg.slice(8));
    else if (arg === '--db') flags['db'] = argv[++i] ?? '';
    else if (arg.startsWith('--db=')) flags['db'] = arg.slice(5);
    else if (arg === '--status' || arg === '--provider' || arg === '--search' || arg === '--kind' || arg === '--machine' || arg === '--account') {
      flags[arg.slice(2)] = argv[++i] ?? '';
    } else if (arg === '--timeout-ms' || arg === '--interval-ms' || arg === '--concurrency') {
      flags[arg.slice(2)] = Number(argv[++i] ?? '');
    } else if (arg.startsWith('--') && arg.includes('=')) {
      const [key, value] = arg.slice(2).split('=');
      flags[key!] = value ?? '';
    } else if (arg.startsWith('--')) {
      console.error(`Unknown flag: ${arg}`);
      process.exit(64);
    } else {
      args.push(arg);
    }
  }
  return { command: args[0] ?? 'help', args: args.slice(1), flags };
}

function emit(flags: Flags, payload: unknown, render: () => void): void {
  if (flags['json']) {
    console.log(JSON.stringify(payload, null, 2));
    return;
  }
  render();
}

/* ------------------------------------------------------------------ */

function requireCurrentSchema(db: Db): void {
  const version = getMeta(db, 'schema_version');
  if (version !== SCHEMA_VERSION) {
    throw new Error(`Registry schema ${version ?? 'unknown'} needs migration; run ao check once before querying live status.`);
  }
}

const commands: Record<string, (db: Db, args: string[], flags: Flags) => void> = {
  stats(db, _args, flags) {
    const counts = db
      .prepare(
        `SELECT (SELECT COUNT(*) FROM account) AS accounts,
                (SELECT COUNT(*) FROM service) AS services,
                (SELECT COUNT(*) FROM machine) AS machines,
                (SELECT COUNT(*) FROM port) AS ports,
                (SELECT COUNT(*) FROM account_link) AS links,
                (SELECT COUNT(*) FROM import_note WHERE level='gap') AS gaps,
                (SELECT COUNT(*) FROM import_note WHERE level='warning') AS warnings,
                (SELECT COUNT(*) FROM import_note WHERE level='reference') AS reference`,
      )
      .get() as Record<string, number>;
    const byStatus = db.prepare('SELECT status, COUNT(*) AS n FROM account GROUP BY status ORDER BY status').all() as {
      status: string;
      n: number;
    }[];
    const byProvider = db.prepare('SELECT provider, COUNT(*) AS n FROM account GROUP BY provider ORDER BY n DESC, provider').all() as {
      provider: string;
      n: number;
    }[];
    const byMachine = db
      .prepare('SELECT m.name, COUNT(p.id) AS n FROM machine m LEFT JOIN port p ON p.machine_id = m.id GROUP BY m.id ORDER BY m.name')
      .all() as { name: string; n: number }[];

    emit(flags, { counts, byStatus, byProvider, byMachine, meta: allMeta(db) }, () => {
      console.log('Registry');
      console.log(`  file        ${flags['db'] ?? defaultDbPath()}`);
      console.log(`  seeded      ${orDash(getMeta(db, 'seeded_at'))}`);
      console.log(`  source map  ${orDash(getMeta(db, 'source_map_path'))}`);
      console.log(`  map sha256  ${orDash(getMeta(db, 'source_map_sha256')?.slice(0, 16))}…`);
      console.log('');
      console.log(table(
        [{ header: 'thing', max: 12 }, { header: 'count', max: 8 }],
        [
          ['accounts', String(counts['accounts'])],
          ['machines', String(counts['machines'])],
          ['services', String(counts['services'])],
          ['ports', String(counts['ports'])],
          ['links', String(counts['links'])],
          ['gaps', String(counts['gaps'])],
          ['warnings', String(counts['warnings'])],
          ['reference', String(counts['reference'])],
        ],
      ));
      if (byStatus.length > 0) {
        console.log('');
        console.log(`Account status: ${byStatus.map((row) => `${row.n} ${row.status}`).join(', ')}`);
      }
      if (byProvider.length > 0) {
        console.log('');
        console.log('Accounts per provider');
        console.log(table(
          [{ header: 'provider', max: 24 }, { header: 'accounts', max: 8 }],
          byProvider.map((row) => [row.provider, String(row.n)]),
        ));
      }
      if (byMachine.length > 0) {
        console.log('');
        console.log('Ports per machine');
        console.log(table(
          [{ header: 'machine', max: 24 }, { header: 'ports', max: 8 }],
          byMachine.map((row) => [row.name, String(row.n)]),
        ));
      }
    });
  },

  accounts(db, _args, flags) {
    const where: string[] = [];
    const params: unknown[] = [];
    if (typeof flags['status'] === 'string') {
      where.push('status = ?');
      params.push(flags['status']);
    }
    if (typeof flags['provider'] === 'string' && flags['provider']) {
      where.push('provider LIKE ?');
      params.push(`%${flags['provider']}%`);
    }
    if (typeof flags['search'] === 'string' && flags['search']) {
      where.push('(id LIKE ? OR provider LIKE ? OR login LIKE ? OR purpose LIKE ?)');
      const needle = `%${flags['search']}%`;
      params.push(needle, needle, needle, needle);
    }
    const rows = db
      .prepare(
        `SELECT id, provider, login, purpose, status,
                (SELECT COUNT(*) FROM service s WHERE s.account_id = a.id) AS services
         FROM account a ${where.length ? `WHERE ${where.join(' AND ')}` : ''}
         ORDER BY provider, login LIMIT ?`,
      )
      .all(...params, Number(flags['limit'])) as Record<string, unknown>[];

    emit(flags, rows, () => {
      if (rows.length === 0) {
        console.log('No accounts matched.');
        return;
      }
      console.log(
        table(
          [
            { header: 'id', max: 30 },
            { header: 'login', max: 28 },
            { header: 'purpose', max: 40 },
            { header: 'status', max: 7 },
            { header: 'svc', max: 3 },
          ],
          rows.map((row) => [
            String(row['id']),
            String(row['login']),
            orUnknown(row['purpose'] as string),
            String(row['status']),
            String(row['services']),
          ]),
        ),
      );
      console.log(`\n${rows.length} shown (limit ${flags['limit']})`);
    });
  },

  account(db, args, flags) {
    const ref = args[0];
    if (!ref) {
      console.error('Usage: ao account <id|login|provider>');
      process.exit(64);
    }
    const matches = resolveAccount(db, ref);
    if (matches.length === 0) {
      console.error(`No account matching "${ref}". Try: ao lookup ${ref}`);
      process.exit(1);
    }
    if (matches.length > 1) {
      console.log(`${matches.length} accounts match "${ref}":`);
      for (const row of matches) console.log(`  ${row.id}  (${row.provider} / ${row.login})`);
      return;
    }
    const account = matches[0]!;
    const services = db.prepare('SELECT id, name, kind, url, status, machine_id FROM service WHERE account_id = ? ORDER BY name').all(account.id) as Record<
      string,
      unknown
    >[];
    const ports = db
      .prepare(
        `SELECT p.id, p.port, p.protocol, p.health, m.name AS machine
         FROM port p JOIN machine m ON m.id = p.machine_id
         WHERE p.service_id IN (SELECT id FROM service WHERE account_id = ?)
         ORDER BY m.name, p.port`,
      )
      .all(account.id) as Record<string, unknown>[];
    const links = db
      .prepare(
        `SELECT 'out' AS direction, l.relation, l.to_account AS peer
           FROM account_link l WHERE l.from_account = ?
         UNION
         SELECT 'in', l.relation, l.from_account
           FROM account_link l WHERE l.to_account = ?
         ORDER BY peer, direction`,
      )
      .all(account.id, account.id) as Record<string, unknown>[];

    emit(flags, { account, services, ports, links }, () => {
      console.log(`${account.id}`);
      console.log(`  provider   ${account.provider}`);
      console.log(`  login      ${account.login}`);
      console.log(`  purpose    ${orUnknown(account.purpose)}`);
      console.log(`  status     ${account.status}`);
      console.log(`  secret ref ${account.secret_ref ? account.secret_ref : '— (not recorded; Phase 1 does not know where it lives)'}`);
      console.log(`  source     ${orDash(account.source_ref)}`);
      if (account.notes) console.log(`  notes      ${account.notes}`);
      if (services.length > 0) {
        console.log('\n  Services');
        console.log(
          table(
            [
              { header: 'id', max: 26 },
              { header: 'name', max: 24 },
              { header: 'kind', max: 8 },
              { header: 'machine', max: 14 },
              { header: 'status', max: 7 },
            ],
            services.map((s) => [String(s['id']), String(s['name']), String(s['kind']), orDash(s['machine_id'] as string), String(s['status'])]),
          )
            .split('\n')
            .map((line) => `  ${line}`)
            .join('\n'),
        );
      }
      if (ports.length > 0) {
        console.log('\n  Ports');
        console.log(
          table(
            [
              { header: 'machine', max: 14 },
              { header: 'port', max: 6 },
              { header: 'proto', max: 4 },
              { header: 'health', max: 6 },
            ],
            ports.map((p) => [String(p['machine']), String(p['port']), String(p['protocol']), String(p['health'])]),
          )
            .split('\n')
            .map((line) => `  ${line}`)
            .join('\n'),
        );
      }
      if (links.length > 0) {
        console.log('\n  Linked to');
        for (const link of links) console.log(`  ${link['direction'] === 'out' ? '→' : '←'} ${String(link['peer'])}  (${link['relation']})`);
      }
    });
  },

  services(db, _args, flags) {
    const where: string[] = [];
    const params: unknown[] = [];
    if (typeof flags['kind'] === 'string' && flags['kind']) {
      where.push('s.kind = ?');
      params.push(flags['kind']);
    }
    if (typeof flags['machine'] === 'string' && flags['machine']) {
      where.push('m.name LIKE ?');
      params.push(`%${flags['machine']}%`);
    }
    if (typeof flags['account'] === 'string' && flags['account']) {
      where.push('s.account_id = ?');
      params.push(flags['account']);
    }
    const rows = db
      .prepare(
        `SELECT s.id, s.name, s.kind, s.status, s.url, m.name AS machine, s.account_id
         FROM service s LEFT JOIN machine m ON m.id = s.machine_id
         ${where.length ? `WHERE ${where.join(' AND ')}` : ''}
         ORDER BY s.kind, s.name LIMIT ?`,
      )
      .all(...params, Number(flags['limit'])) as Record<string, unknown>[];

    emit(flags, rows, () => {
      if (rows.length === 0) {
        console.log('No services matched.');
        return;
      }
      console.log(
        table(
          [
            { header: 'id', max: 32 },
            { header: 'kind', max: 8 },
            { header: 'machine', max: 14 },
            { header: 'status', max: 7 },
            { header: 'account', max: 24 },
          ],
          rows.map((row) => [
            String(row['id']),
            String(row['kind']),
            orDash(row['machine'] as string),
            String(row['status']),
            orDash(row['account_id'] as string),
          ]),
        ),
      );
      console.log(`\n${rows.length} shown (limit ${flags['limit']})`);
    });
  },

  service(db, args, flags) {
    const ref = args[0];
    if (!ref) {
      console.error('Usage: ao service <id>');
      process.exit(64);
    }
    const service = db
      .prepare('SELECT s.*, m.name AS machine FROM service s LEFT JOIN machine m ON m.id = s.machine_id WHERE s.id LIKE ? OR s.name LIKE ?')
      .get(ref, `%${ref}%`) as Record<string, unknown> | undefined;
    if (!service) {
      console.error(`No service matching "${ref}".`);
      process.exit(1);
    }
    const ports = db.prepare('SELECT * FROM port WHERE service_id = ? ORDER BY port').all(service['id'] as string) as Record<string, unknown>[];
    emit(flags, { service, ports }, () => {
      console.log(String(service['id']));
      console.log(`  name     ${service['name']}`);
      console.log(`  kind     ${service['kind']}`);
      console.log(`  machine  ${orDash(service['machine'] as string)}`);
      console.log(`  url      ${orDash(service['url'] as string)}`);
      console.log(`  status   ${service['status']}`);
      console.log(`  account  ${orDash(service['account_id'] as string)}`);
      console.log(`  source   ${orDash(service['source_ref'] as string)}`);
      if (ports.length > 0) {
        console.log('');
        console.log(table(
          [{ header: 'port', max: 6 }, { header: 'proto', max: 5 }, { header: 'health', max: 7 }, { header: 'last seen', max: 24 }],
          ports.map((p) => [String(p['port']), String(p['protocol']), String(p['health']), orDash(p['last_seen'] as string)]),
        ));
      }
    });
  },

  machines(db, _args, flags) {
    const rows = db
      .prepare(
        `SELECT m.*, (SELECT COUNT(*) FROM port p WHERE p.machine_id = m.id) AS ports,
                (SELECT COUNT(*) FROM service s WHERE s.machine_id = m.id) AS services
         FROM machine m ORDER BY m.name`,
      )
      .all() as Record<string, unknown>[];
    emit(flags, rows, () => {
      if (rows.length === 0) {
        console.log('No machines in the registry.');
        return;
      }
      console.log(
        table(
          [
            { header: 'id', max: 16 },
            { header: 'name', max: 18 },
            { header: 'tailnet ip', max: 16 },
            { header: 'os', max: 12 },
            { header: 'role', max: 26 },
            { header: 'status', max: 7 },
            { header: 'ports', max: 5 },
          ],
          rows.map((row) => [
            String(row['id']),
            String(row['name']),
            orDash(row['tailnet_ip'] as string),
            orDash(row['os'] as string),
            orDash(row['role'] as string),
            String(row['status']),
            String(row['ports']),
          ]),
        ),
      );
    });
  },

  machine(db, args, flags) {
    const ref = args[0];
    if (!ref) {
      console.error('Usage: ao machine <name|id>');
      process.exit(64);
    }
    const machine = db.prepare('SELECT * FROM machine WHERE id = ? OR lower(name) = lower(?) OR name LIKE ?').get(ref, ref, `%${ref}%`) as
      | Record<string, unknown>
      | undefined;
    if (!machine) {
      console.error(`No machine matching "${ref}".`);
      process.exit(1);
    }
    const ports = db
      .prepare(
        `SELECT p.port, p.protocol, p.health, p.last_seen, s.name AS service, s.id AS service_id
         FROM port p LEFT JOIN service s ON s.id = p.service_id
         WHERE p.machine_id = ? ORDER BY p.port`,
      )
      .all(machine['id'] as string) as Record<string, unknown>[];
    emit(flags, { machine, ports }, () => {
      console.log(`${machine['name']}  (${machine['id']})`);
      console.log(`  tailnet ip ${orDash(machine['tailnet_ip'] as string)}`);
      console.log(`  os         ${orDash(machine['os'] as string)}`);
      console.log(`  role       ${orDash(machine['role'] as string)}`);
      console.log(`  status     ${machine['status']}`);
      console.log(`  source     ${orDash(machine['source_ref'] as string)}`);
      if (ports.length > 0) {
        console.log('');
        console.log(
          table(
            [
              { header: 'port', max: 6 },
              { header: 'proto', max: 5 },
              { header: 'health', max: 7 },
              { header: 'service', max: 30 },
              { header: 'last seen', max: 24 },
            ],
            ports.map((p) => [
              String(p['port']),
              String(p['protocol']),
              String(p['health']),
              orDash(p['service'] as string),
              orDash(p['last_seen'] as string),
            ]),
          ),
        );
      }
    });
  },

  ports(db, _args, flags) {
    const params: unknown[] = [];
    let where = '';
    if (typeof flags['machine'] === 'string' && flags['machine']) {
      where = 'WHERE m.name LIKE ?';
      params.push(`%${flags['machine']}%`);
    }
    const rows = db
      .prepare(
        `SELECT p.port, p.protocol, p.health, p.last_seen, m.name AS machine, s.name AS service
         FROM port p JOIN machine m ON m.id = p.machine_id LEFT JOIN service s ON s.id = p.service_id
         ${where} ORDER BY m.name, p.port LIMIT ?`,
      )
      .all(...params, Number(flags['limit'])) as Record<string, unknown>[];
    emit(flags, rows, () => {
      if (rows.length === 0) {
        console.log('No ports in the registry.');
        return;
      }
      console.log(
        table(
          [
            { header: 'machine', max: 16 },
            { header: 'port', max: 6 },
            { header: 'proto', max: 5 },
            { header: 'service', max: 34 },
            { header: 'health', max: 7 },
          ],
          rows.map((row) => [
            String(row['machine']),
            String(row['port']),
            String(row['protocol']),
            orDash(row['service'] as string),
            String(row['health']),
          ]),
        ),
      );
    });
  },

  'live-status'(db, _args, flags) {
    requireCurrentSchema(db);
    const checkedAt = getMeta(db, 'last_health_check_at') ?? null;
    const services = db.prepare(
      `SELECT id, name, status AS map_status, live_status, health, last_seen
       FROM service ORDER BY kind, name`,
    ).all() as Record<string, unknown>[];
    const ports = db.prepare(
      `SELECT p.id, m.name AS machine, s.name AS service, p.port, p.protocol, p.health,
              CASE p.health WHEN 'up' THEN 'live' WHEN 'down' THEN 'dead' ELSE 'unknown' END AS live_status,
              p.last_seen
       FROM port p JOIN machine m ON m.id = p.machine_id
       LEFT JOIN service s ON s.id = p.service_id
       ORDER BY m.name, p.port`,
    ).all() as Record<string, unknown>[];

    emit(flags, { checkedAt, services, ports }, () => {
      console.log(`Last completed check: ${orDash(checkedAt ?? undefined)}`);
      console.log(`\nServices (${services.length})`);
      console.log(table(
        [
          { header: 'name', max: 28 },
          { header: 'map', max: 8 },
          { header: 'live', max: 8 },
          { header: 'health', max: 8 },
          { header: 'last seen', max: 24 },
        ],
        services.map((row) => [
          String(row['name']), String(row['map_status']), String(row['live_status']),
          String(row['health']), orDash(row['last_seen'] as string),
        ]),
      ));
      console.log(`\nPorts (${ports.length})`);
      if (ports.length === 0) {
        console.log('No ports in the registry.');
        return;
      }
      console.log(table(
        [
          { header: 'machine', max: 24 },
          { header: 'service', max: 28 },
          { header: 'port', max: 6 },
          { header: 'proto', max: 5 },
          { header: 'live', max: 8 },
          { header: 'health', max: 8 },
          { header: 'last seen', max: 24 },
        ],
        ports.map((row) => [
          String(row['machine']), orDash(row['service'] as string), String(row['port']),
          String(row['protocol']), String(row['live_status']), String(row['health']),
          orDash(row['last_seen'] as string),
        ]),
      ));
    });
  },

  links(db, _args, flags) {
    const rows = db
      .prepare(
        `SELECT l.from_account, l.to_account, l.relation, l.source_ref
         FROM account_link l ORDER BY l.from_account, l.to_account LIMIT ?`,
      )
      .all(Number(flags['limit'])) as Record<string, unknown>[];
    emit(flags, rows, () => {
      if (rows.length === 0) {
        console.log('No account links recorded.');
        return;
      }
      console.log(
        table(
          [
            { header: 'from', max: 30 },
            { header: 'to', max: 30 },
            { header: 'relation', max: 10 },
            { header: 'source', max: 18 },
          ],
          rows.map((row) => [String(row['from_account']), String(row['to_account']), String(row['relation']), orDash(row['source_ref'] as string)]),
        ),
      );
    });
  },

  gaps(db, _args, flags) {
    const rows = db.prepare("SELECT source_ref, text, reason FROM import_note WHERE level='gap' ORDER BY id LIMIT ?").all(Number(flags['limit'])) as Record<
      string,
      unknown
    >[];
    emit(flags, rows, () => {
      if (rows.length === 0) {
        console.log('No gaps: everything in the map was placed.');
        return;
      }
      console.log('Map material the parser could not place:\n');
      for (const row of rows) {
        console.log(`${row['source_ref']}`);
        console.log(`   ${String(row['text'] ?? '').slice(0, 140)}`);
        console.log(`   → ${row['reason']}`);
      }
      console.log('\nThese lines are not in the registry. Fix the map or extend the parser — do not guess.');
    });
  },

  reference(db, _args, flags) {
    const rows = db
      .prepare("SELECT source_ref, text FROM import_note WHERE level='reference' ORDER BY id LIMIT ?")
      .all(Number(flags['limit'])) as Record<string, unknown>[];
    emit(flags, rows, () => {
      if (rows.length === 0) {
        console.log('No reference notes: the map had no status key or legend.');
        return;
      }
      console.log('Kept as text, not records:\n');
      for (const row of rows) console.log(`${row['source_ref']}  ${row['text']}`);
    });
  },

  warnings(db, _args, flags) {
    const rows = db.prepare("SELECT source_ref, text FROM import_note WHERE level='warning' ORDER BY id LIMIT ?").all(Number(flags['limit'])) as Record<
      string,
      unknown
    >[];
    emit(flags, rows, () => {
      if (rows.length === 0) {
        console.log('No warnings: nothing was merged, inferred, or guessed.');
        return;
      }
      for (const row of rows) console.log(`${row['source_ref']}  ${row['text']}`);
    });
  },

  lookup(db, args, flags) {
    const term = args.join(' ');
    if (!term) {
      console.error('Usage: ao lookup <text>');
      process.exit(64);
    }
    const needle = `%${term}%`;
    const accounts = db
      .prepare('SELECT id, provider, login, purpose, status FROM account WHERE id LIKE ? OR provider LIKE ? OR login LIKE ? OR purpose LIKE ? LIMIT ?')
      .all(needle, needle, needle, needle, Number(flags['limit'])) as Record<string, unknown>[];
    const services = db
      .prepare('SELECT id, name, kind, url, machine_id FROM service WHERE id LIKE ? OR name LIKE ? OR url LIKE ? LIMIT ?')
      .all(needle, needle, needle, Number(flags['limit'])) as Record<string, unknown>[];
    const machines = db.prepare('SELECT id, name, tailnet_ip, role FROM machine WHERE id LIKE ? OR name LIKE ? OR tailnet_ip LIKE ? LIMIT ?').all(
      needle,
      needle,
      needle,
      Number(flags['limit']),
    ) as Record<string, unknown>[];

    emit(flags, { accounts, services, machines }, () => {
      console.log(`Accounts (${accounts.length})`);
      for (const row of accounts) console.log(`  ${row['id']}  ${row['provider']} / ${row['login']}  [${row['status']}]  ${orUnknown(row['purpose'] as string)}`);
      console.log(`\nServices (${services.length})`);
      for (const row of services) console.log(`  ${row['id']}  ${row['kind']}  ${orDash(row['url'] as string)}`);
      console.log(`\nMachines (${machines.length})`);
      for (const row of machines) console.log(`  ${row['name']}  ${orDash(row['tailnet_ip'] as string)}  ${orDash(row['role'] as string)}`);
    });
  },
};

interface AccountDetail {
  id: string;
  provider: string;
  login: string;
  purpose: string;
  status: string;
  secret_ref: string | null;
  source_ref: string | null;
  notes: string | null;
}

function resolveAccount(db: Db, ref: string): AccountDetail[] {
  const exact = db.prepare('SELECT * FROM account WHERE id = ? OR id = ? OR lower(provider || \':\' || login) = lower(?)').all(ref, ref.toLowerCase(), ref) as AccountDetail[];
  if (exact.length > 0) return exact;
  const loose = db
    .prepare('SELECT * FROM account WHERE lower(login) = lower(?) OR lower(provider) = lower(?) LIMIT 25')
    .all(ref, ref) as AccountDetail[];
  return loose;
}

function allMeta(db: Db): Record<string, string> {
  const rows = db.prepare('SELECT key, value FROM meta').all() as { key: string; value: string }[];
  return Object.fromEntries(rows.map((row) => [row.key, row.value]));
}

/* ------------------------------------------------------------------ */

function numberOption(
  flags: Flags,
  key: string,
  envName: string,
  fallback: number,
  min: number,
  max: number,
): number {
  const raw = flags[key] ?? process.env[envName] ?? fallback;
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    throw new Error(`${key} must be an integer from ${min} to ${max}`);
  }
  return value;
}

function printCheck(summary: CheckCycleSummary, flags: Flags): void {
  if (flags['json']) {
    console.log(JSON.stringify(summary));
    return;
  }
  console.log(`Check completed at ${summary.checkedAt}`);
  console.log(`Services: ${summary.services.live} live, ${summary.services.dead} dead, ${summary.services.unknown} unknown (${summary.services.total} total)`);
  console.log(`Ports: ${summary.ports.up} up, ${summary.ports.down} down, ${summary.ports.unknown} unknown (${summary.ports.total} total)`);
}

function waitFor(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const finish = () => {
      clearTimeout(timer);
      signal.removeEventListener('abort', finish);
      resolve();
    };
    const timer = setTimeout(finish, ms);
    signal.addEventListener('abort', finish, { once: true });
  });
}

async function runChecks(command: 'check' | 'run', flags: Flags, args: string[]): Promise<void> {
  if (args.length > 0) throw new Error(`Usage: ao ${command} [options]`);
  const dbPath = typeof flags['db'] === 'string' && flags['db'] ? flags['db'] : defaultDbPath();
  const timeoutMs = numberOption(flags, 'timeout-ms', 'AO_CHECK_TIMEOUT_MS', 3000, 1, 60_000);
  const concurrency = numberOption(flags, 'concurrency', 'AO_CHECK_CONCURRENCY', 16, 1, 64);
  const options = { dbPath, timeoutMs, concurrency };
  if (command === 'check') {
    printCheck(await runCheckCycle(options), flags);
    return;
  }

  const intervalMs = numberOption(flags, 'interval-ms', 'AO_CHECK_INTERVAL_MS', 300_000, 1000, 86_400_000);
  const controller = new AbortController();
  const stop = () => controller.abort();
  process.once('SIGINT', stop);
  process.once('SIGTERM', stop);
  try {
    while (!controller.signal.aborted) {
      printCheck(await runCheckCycle(options), flags);
      await waitFor(intervalMs, controller.signal);
    }
  } finally {
    process.off('SIGINT', stop);
    process.off('SIGTERM', stop);
  }
}

async function main(): Promise<void> {
  const { command, args, flags } = parse(process.argv.slice(2));
  if (command === 'help' || command === '--help' || command === '-h') {
    console.log(HELP);
    return;
  }
  if (command === 'check' || command === 'run') {
    try {
      await runChecks(command, flags, args);
    } catch (error) {
      console.error((error as Error).message);
      process.exitCode = 1;
    }
    return;
  }
  const handler = commands[command];
  if (!handler) {
    console.error(`Unknown command: ${command}\n`);
    console.log(HELP);
    process.exitCode = 64;
    return;
  }
  let db: Db;
  try {
    db = openReadOnly(typeof flags['db'] === 'string' && flags['db'] ? flags['db'] : defaultDbPath());
  } catch (error) {
    console.error((error as Error).message);
    process.exitCode = 1;
    return;
  }
  try {
    handler(db, args, flags);
  } catch (error) {
    console.error((error as Error).message);
    process.exitCode = 1;
  } finally {
    db.close();
  }
}

void main().catch((error) => {
  console.error((error as Error).message);
  process.exitCode = 1;
});
