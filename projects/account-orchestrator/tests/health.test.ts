import assert from 'node:assert/strict';
import { createServer, type Server } from 'node:http';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import Database from 'better-sqlite3';
import { getMeta, openForWrite, openReadOnly, PROJECT_ROOT, schemaPath, setMeta } from '../src/db.js';
import { runCheckCycle } from '../src/health.js';

function temporaryDb(): { directory: string; path: string } {
  const directory = mkdtempSync(join(tmpdir(), 'ao-health-'));
  return { directory, path: join(directory, 'registry.sqlite') };
}

function listen(server: Server): Promise<{ host: string; port: number; url: string }> {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      server.removeListener('error', reject);
      const address = server.address();
      if (!address || typeof address === 'string') {
        reject(new Error('The test server did not bind a TCP address.'));
        return;
      }
      resolve({ host: '127.0.0.1', port: address.port, url: `http://127.0.0.1:${address.port}/health` });
    });
  });
}

function close(server: Server): Promise<void> {
  return new Promise((resolve, reject) => {
    if (!server.listening) {
      resolve();
      return;
    }
    server.close((error) => error ? reject(error) : resolve());
  });
}

function insertRegistry(path: string, url: string | null, staticStatus: 'live' | 'dead' | 'unknown', protocol: 'tcp' | 'udp' = 'tcp', port?: number): void {
  const db = openForWrite(path);
  try {
    db.prepare('INSERT INTO machine (id, name, tailnet_ip, status) VALUES (?, ?, ?, ?)')
      .run('machine:test', 'Local test machine', '127.0.0.1', 'live');
    db.prepare('INSERT INTO service (id, name, kind, url, status) VALUES (?, ?, ?, ?, ?)')
      .run('svc:test', 'Test service', 'app', url, staticStatus);
    if (port !== undefined) {
      db.prepare('INSERT INTO port (id, service_id, machine_id, port, protocol) VALUES (?, ?, ?, ?, ?)')
        .run(`machine:test:${port}/${protocol}`, 'svc:test', 'machine:test', port, protocol);
    }
  } finally {
    db.close();
  }
}

test('Phase 2 migration upgrades a Phase 1 database without changing map status or rows', () => {
  const { directory, path } = temporaryDb();
  const legacy = new Database(path);
  try {
    legacy.exec(readFileSync(schemaPath(), 'utf8'));
    setMeta(legacy, 'schema_version', '1');
    legacy.prepare('INSERT INTO machine (id, name, tailnet_ip, status) VALUES (?, ?, ?, ?)')
      .run('machine:legacy', 'Legacy machine', '127.0.0.1', 'live');
    legacy.prepare('INSERT INTO service (id, name, kind, url, status) VALUES (?, ?, ?, ?, ?)')
      .run('svc:legacy', 'Legacy service', 'app', 'http://127.0.0.1/', 'dead');
  } finally {
    legacy.close();
  }

  const upgraded = openForWrite(path);
  try {
    const service = upgraded.prepare('SELECT status, live_status, health, last_seen FROM service WHERE id = ?')
      .get('svc:legacy') as { status: string; live_status: string; health: string; last_seen: string | null };
    assert.equal(getMeta(upgraded, 'schema_version'), '2');
    assert.deepEqual(service, { status: 'dead', live_status: 'unknown', health: 'unknown', last_seen: null });
    assert.equal((upgraded.prepare('SELECT COUNT(*) AS n FROM service').get() as { n: number }).n, 1);
  } finally {
    upgraded.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test('a local service goes live, then dead after the server is stopped, updating the same rows', async () => {
  const server = createServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'text/plain' });
    response.end('ready');
  });
  const endpoint = await listen(server);
  const { directory, path } = temporaryDb();
  insertRegistry(path, endpoint.url, 'dead', 'tcp', endpoint.port);

  try {
    const first = await runCheckCycle({ dbPath: path, timeoutMs: 500, now: () => new Date('2026-11-09T12:00:00.000Z') });
    assert.equal(first.services.live, 1);
    assert.equal(first.ports.up, 1);
    const afterFirstDb = openReadOnly(path);
    let afterFirst: { status: string; live_status: string; health: string; last_seen: string | null; port_health: string; port_last_seen: string | null };
    try {
      afterFirst = afterFirstDb.prepare(
        `SELECT s.status, s.live_status, s.health, s.last_seen,
                p.health AS port_health, p.last_seen AS port_last_seen
         FROM service s JOIN port p ON p.service_id = s.id WHERE s.id = 'svc:test'`,
      ).get() as typeof afterFirst;
    } finally {
      afterFirstDb.close();
    }
    assert.deepEqual(afterFirst!, {
      status: 'dead',
      live_status: 'live',
      health: 'up',
      last_seen: first.checkedAt,
      port_health: 'up',
      port_last_seen: first.checkedAt,
    });

    await close(server);
    const second = await runCheckCycle({ dbPath: path, timeoutMs: 500, now: () => new Date('2026-11-09T12:05:00.000Z') });
    assert.equal(second.services.dead, 1);
    assert.equal(second.ports.down, 1);
    const afterSecondDb = openReadOnly(path);
    try {
      const afterSecond = afterSecondDb.prepare(
        `SELECT s.status, s.live_status, s.health, s.last_seen,
                p.health AS port_health, p.last_seen AS port_last_seen,
                (SELECT COUNT(*) FROM service) AS service_count,
                (SELECT COUNT(*) FROM port) AS port_count
         FROM service s JOIN port p ON p.service_id = s.id WHERE s.id = 'svc:test'`,
      ).get() as Record<string, unknown>;
      assert.equal(afterSecond['status'], 'dead');
      assert.equal(afterSecond['live_status'], 'dead');
      assert.equal(afterSecond['health'], 'down');
      assert.equal(afterSecond['last_seen'], second.checkedAt);
      assert.equal(afterSecond['port_health'], 'down');
      assert.equal(afterSecond['port_last_seen'], second.checkedAt);
      assert.equal(afterSecond['service_count'], 1);
      assert.equal(afterSecond['port_count'], 1);
    } finally {
      afterSecondDb.close();
    }

    const cli = spawnSync(process.execPath, [join(PROJECT_ROOT, 'dist', 'src', 'cli.js'), 'live-status', '--db', path], {
      encoding: 'utf8',
      timeout: 5000,
    });
    assert.equal(cli.status, 0, cli.stderr);
    assert.match(cli.stdout, /Test service/);
    assert.match(cli.stdout, /dead/);
    assert.match(cli.stdout, /Local test machine/);
    assert.match(cli.stdout, /down/);
  } finally {
    await close(server);
    rmSync(directory, { recursive: true, force: true });
  }
});

test('services without a URL and UDP ports remain unknown rather than guessed', async () => {
  const { directory, path } = temporaryDb();
  const db = openForWrite(path);
  try {
    db.prepare('INSERT INTO machine (id, name, tailnet_ip) VALUES (?, ?, ?)')
      .run('machine:test', 'Local test machine', '127.0.0.1');
    db.prepare('INSERT INTO service (id, name, kind, status) VALUES (?, ?, ?, ?)')
      .run('svc:udp', 'UDP service', 'app', 'live');
    db.prepare('INSERT INTO service (id, name, kind, status) VALUES (?, ?, ?, ?)')
      .run('svc:empty', 'No endpoint', 'other', 'dead');
    db.prepare('INSERT INTO port (id, service_id, machine_id, port, protocol) VALUES (?, ?, ?, ?, ?)')
      .run('machine:test:9999/udp', 'svc:udp', 'machine:test', 9999, 'udp');
  } finally {
    db.close();
  }

  try {
    const summary = await runCheckCycle({ dbPath: path, timeoutMs: 500 });
    assert.deepEqual(summary.services, { total: 2, live: 0, dead: 0, unknown: 2 });
    assert.deepEqual(summary.ports, { total: 1, up: 0, down: 0, unknown: 1 });
    const checked = openReadOnly(path);
    try {
      const services = checked.prepare('SELECT live_status, health, last_seen FROM service ORDER BY id').all() as {
        live_status: string; health: string; last_seen: string | null;
      }[];
      assert.deepEqual(services, [
        { live_status: 'unknown', health: 'unknown', last_seen: null },
        { live_status: 'unknown', health: 'unknown', last_seen: null },
      ]);
      assert.equal((checked.prepare('SELECT health FROM port').get() as { health: string }).health, 'unknown');
      assert.ok(getMeta(checked, 'last_health_check_at'));
    } finally {
      checked.close();
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('HTTP 405 on HEAD falls back to a ranged GET and accepts a 2xx response', async () => {
  const methods: string[] = [];
  const ranges: string[] = [];
  const server = createServer((request, response) => {
    methods.push(request.method ?? '');
    ranges.push(request.headers['range'] ?? '');
    if (request.method === 'HEAD') {
      response.writeHead(405).end();
      return;
    }
    response.writeHead(204).end();
  });
  const endpoint = await listen(server);
  const { directory, path } = temporaryDb();
  insertRegistry(path, endpoint.url, 'unknown');

  try {
    const summary = await runCheckCycle({ dbPath: path, timeoutMs: 500 });
    assert.deepEqual(methods, ['HEAD', 'GET']);
    assert.deepEqual(ranges, ['', 'bytes=0-0']);
    assert.equal(summary.services.live, 1);
  } finally {
    await close(server);
    rmSync(directory, { recursive: true, force: true });
  }
});


test('HTTP URLs containing user info remain unknown and are never requested', async () => {
  let requests = 0;
  const server = createServer((_request, response) => {
    requests += 1;
    response.writeHead(200).end();
  });
  const endpoint = await listen(server);
  const { directory, path } = temporaryDb();
  const credentialUrl = endpoint.url.replace('http://', 'http://operator@');
  insertRegistry(path, credentialUrl, 'live');

  try {
    const summary = await runCheckCycle({ dbPath: path, timeoutMs: 500 });
    assert.equal(summary.services.unknown, 1);
    assert.equal(requests, 0);
    const db = openReadOnly(path);
    try {
      const result = db.prepare('SELECT live_status, health, last_seen FROM service').get() as {
        live_status: string; health: string; last_seen: string | null;
      };
      assert.deepEqual(result, { live_status: 'unknown', health: 'unknown', last_seen: null });
    } finally {
      db.close();
    }
  } finally {
    await close(server);
    rmSync(directory, { recursive: true, force: true });
  }
});

test('HTTP timeouts mark a service dead without changing its map status', async () => {
  const server = createServer((_request, response) => {
    const timer = setTimeout(() => {
      if (!response.destroyed) response.writeHead(200).end();
    }, 250);
    response.on('close', () => clearTimeout(timer));
  });
  const endpoint = await listen(server);
  const { directory, path } = temporaryDb();
  insertRegistry(path, endpoint.url, 'live');

  try {
    const summary = await runCheckCycle({ dbPath: path, timeoutMs: 25 });
    assert.equal(summary.services.dead, 1);
    const db = openReadOnly(path);
    try {
      const result = db.prepare('SELECT status, live_status, health, last_seen FROM service').get() as {
        status: string; live_status: string; health: string; last_seen: string | null;
      };
      assert.deepEqual(result, { status: 'live', live_status: 'dead', health: 'down', last_seen: summary.checkedAt });
    } finally {
      db.close();
    }
  } finally {
    await close(server);
    rmSync(directory, { recursive: true, force: true });
  }
});

