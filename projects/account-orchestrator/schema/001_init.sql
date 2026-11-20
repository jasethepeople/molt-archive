-- Account Orchestrator — Phase 1 schema
-- One SQLite file. Local only. No cloud copy.
--
-- Invariants enforced here:
--   * Vocabulary is closed: status/health/kind only take the values listed below.
--   * Ports always belong to a machine and to a service.
--   * Accounts are unique per (provider, login).
--   * Nothing in this schema stores a secret value. `secret_ref` holds a
--     *location* string such as `env:GITHUB_TOKEN`; the importer and `verify`
--     both reject secret-looking values (see src/secrets.ts).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS machine (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  tailnet_ip  TEXT,
  os          TEXT,
  role        TEXT,
  status      TEXT NOT NULL DEFAULT 'unknown'
                CHECK (status IN ('live', 'dead', 'unknown')),
  last_seen   TEXT,
  source_ref  TEXT,
  notes       TEXT
);

CREATE TABLE IF NOT EXISTS account (
  id           TEXT PRIMARY KEY,
  provider     TEXT NOT NULL,
  login        TEXT NOT NULL,
  purpose      TEXT NOT NULL DEFAULT 'unknown',
  status       TEXT NOT NULL DEFAULT 'unknown'
                 CHECK (status IN ('live', 'dead', 'unknown')),
  -- Reference only, never a value. NULL = where the credential lives is not
  -- recorded yet; that account cannot be used by anything downstream.
  secret_ref   TEXT,
  last_checked TEXT,
  source_ref   TEXT,
  notes        TEXT,
  UNIQUE (provider, login),
  -- 'unknown' is a real answer; an empty purpose is not.
  CHECK (length(trim(purpose)) > 0)
);

CREATE TABLE IF NOT EXISTS service (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  kind       TEXT NOT NULL
               CHECK (kind IN ('repo', 'site', 'database', 'vm', 'app', 'api', 'other')),
  account_id TEXT REFERENCES account(id) ON DELETE SET NULL,
  url        TEXT,
  status     TEXT NOT NULL DEFAULT 'unknown'
               CHECK (status IN ('live', 'dead', 'unknown')),
  machine_id TEXT REFERENCES machine(id) ON DELETE SET NULL,
  source_ref TEXT,
  notes      TEXT
);

CREATE TABLE IF NOT EXISTS port (
  id         TEXT PRIMARY KEY,
  service_id TEXT REFERENCES service(id) ON DELETE CASCADE,
  machine_id TEXT NOT NULL REFERENCES machine(id) ON DELETE CASCADE,
  port       INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
  protocol   TEXT NOT NULL DEFAULT 'tcp'
               CHECK (protocol IN ('tcp', 'udp')),
  health     TEXT NOT NULL DEFAULT 'unknown'
               CHECK (health IN ('up', 'down', 'unknown')),
  last_seen  TEXT,
  source_ref TEXT,
  UNIQUE (machine_id, port, protocol)
);

CREATE TABLE IF NOT EXISTS account_link (
  from_account TEXT NOT NULL REFERENCES account(id) ON DELETE CASCADE,
  to_account   TEXT NOT NULL REFERENCES account(id) ON DELETE CASCADE,
  relation     TEXT NOT NULL DEFAULT 'linked',
  source_ref   TEXT,
  PRIMARY KEY (from_account, to_account, relation),
  CHECK (from_account <> to_account)
);

CREATE INDEX IF NOT EXISTS idx_account_provider ON account(provider);
CREATE INDEX IF NOT EXISTS idx_account_status   ON account(status);
CREATE INDEX IF NOT EXISTS idx_service_account  ON service(account_id);
CREATE INDEX IF NOT EXISTS idx_service_machine  ON service(machine_id);
CREATE INDEX IF NOT EXISTS idx_service_kind     ON service(kind);
CREATE INDEX IF NOT EXISTS idx_port_machine     ON port(machine_id);
CREATE INDEX IF NOT EXISTS idx_port_service     ON port(service_id);
-- Provenance: what the map contained that the parser could not place, and what
-- it merged or guessed at. Carried in the registry so `ao gaps` works offline.
CREATE TABLE IF NOT EXISTS import_note (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  level      TEXT NOT NULL CHECK (level IN ('gap', 'warning', 'reference')),
  source_ref TEXT NOT NULL,
  text       TEXT,
  reason     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_account_provider ON account(provider);
