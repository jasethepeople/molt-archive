/**
 * Database access.
 *
 * One registry file. Seed and health-check cycles are the only write paths;
 * query commands open it readonly.
 */
import Database from 'better-sqlite3';
import { readFileSync, existsSync, mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export type Db = Database.Database;

/** Project root: the nearest ancestor holding this package.json. */
function findProjectRoot(): string {
  let directory = dirname(fileURLToPath(import.meta.url));
  for (let depth = 0; depth < 6; depth += 1) {
    const candidate = join(directory, 'package.json');
    if (existsSync(candidate)) {
      const manifest = JSON.parse(readFileSync(candidate, 'utf8')) as { name?: string };
      if (manifest.name === 'account-orchestrator') return directory;
    }
    const parent = dirname(directory);
    if (parent === directory) break;
    directory = parent;
  }
  return resolve(dirname(fileURLToPath(import.meta.url)), '..');
}

export const PROJECT_ROOT = findProjectRoot();

export const SCHEMA_VERSION = '2';

export function defaultDbPath(): string {
  return process.env['AO_DB'] ?? join(PROJECT_ROOT, 'data', 'registry.sqlite');
}

export function schemaPath(): string {
  return join(PROJECT_ROOT, 'schema', '001_init.sql');
}

export function openReadOnly(path: string = defaultDbPath()): Db {
  if (!existsSync(path)) {
    throw new Error(
      `No registry at ${path}. Seed it first: npm run seed -- --map <network-map.md> --machine <this-box>`,
    );
  }
  const db = new Database(path, { readonly: true, fileMustExist: true });
  db.pragma('foreign_keys = ON');
  return db;
}

/** Creates or reopens the registry, applying additive migrations before returning it. */
export function openForWrite(path: string = defaultDbPath()): Db {
  mkdirSync(dirname(path), { recursive: true });
  const db = new Database(path);
  db.pragma('journal_mode = DELETE');
  db.pragma('synchronous = FULL');
  db.pragma('foreign_keys = ON');
  db.pragma('busy_timeout = 5000');
  try {
    db.exec(readFileSync(schemaPath(), 'utf8'));
    let version = Number(getMeta(db, 'schema_version') ?? '1');
    if (!Number.isSafeInteger(version) || version < 1) throw new Error(`Invalid registry schema version: ${version}`);
    if (version > Number(SCHEMA_VERSION)) throw new Error(`Registry schema ${version} is newer than this build (${SCHEMA_VERSION})`);
    if (getMeta(db, 'schema_version') === undefined) setMeta(db, 'schema_version', '1');
    if (version < 2) {
      const migrate = db.transaction(() => {
        db.exec(readFileSync(join(PROJECT_ROOT, 'schema', '002_health_checks.sql'), 'utf8'));
        setMeta(db, 'schema_version', '2');
      });
      migrate();
      version = 2;
    }
    if (version !== Number(SCHEMA_VERSION)) throw new Error(`Registry schema ${version} is not supported`);
    return db;
  } catch (error) {
    db.close();
    throw error;
  }
}

export function getMeta(db: Db, key: string): string | undefined {
  const row = db.prepare('SELECT value FROM meta WHERE key = ?').get(key) as { value: string } | undefined;
  return row?.value;
}

export function setMeta(db: Db, key: string, value: string): void {
  db.prepare(
    'INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
  ).run(key, value);
}

export function tableCounts(db: Db): Record<string, number> {
  const tables = ['machine', 'account', 'service', 'port', 'account_link'] as const;
  const counts: Record<string, number> = {};
  for (const table of tables) {
    const row = db.prepare(`SELECT COUNT(*) AS n FROM ${table}`).get() as { n: number };
    counts[table] = row.n;
  }
  return counts;
}
