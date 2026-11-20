import { existsSync } from 'node:fs';
import { createConnection, isIP } from 'node:net';
import { defaultDbPath, openForWrite, setMeta } from './db.js';

export type ServiceStatus = 'live' | 'dead' | 'unknown';
export type PortHealth = 'up' | 'down' | 'unknown';

interface ServiceRow {
  id: string;
  url: string | null;
}

interface PortRow {
  id: string;
  service_id: string | null;
  machine_id: string;
  port: number;
  protocol: 'tcp' | 'udp';
  host: string | null;
}

interface ProbeResult<T extends string> {
  health: T;
  lastSeen: string | null;
}

export interface CheckCycleOptions {
  dbPath?: string;
  timeoutMs?: number;
  concurrency?: number;
  now?: () => Date;
}

export interface CheckCycleSummary {
  checkedAt: string;
  services: { total: number; live: number; dead: number; unknown: number };
  ports: { total: number; up: number; down: number; unknown: number };
}

const DEFAULT_TIMEOUT_MS = 3000;
const DEFAULT_CONCURRENCY = 16;

function checkedTimeout(timeoutMs: number | undefined): number {
  const value = timeoutMs ?? DEFAULT_TIMEOUT_MS;
  if (!Number.isInteger(value) || value < 1 || value > 60_000) {
    throw new Error('timeout-ms must be an integer from 1 to 60000');
  }
  return value;
}

function checkedConcurrency(concurrency: number | undefined): number {
  const value = concurrency ?? DEFAULT_CONCURRENCY;
  if (!Number.isInteger(value) || value < 1 || value > 64) {
    throw new Error('concurrency must be an integer from 1 to 64');
  }
  return value;
}

async function mapConcurrent<T, R>(
  items: T[],
  concurrency: number,
  run: (item: T) => Promise<R>,
): Promise<R[]> {
  const results = new Array<R>(items.length);
  let next = 0;
  await Promise.all(
    Array.from({ length: Math.min(concurrency, items.length) }, async () => {
      while (true) {
        const index = next;
        next += 1;
        if (index >= items.length) return;
        results[index] = await run(items[index]!);
      }
    }),
  );
  return results;
}

async function probeHttp(url: string, timeoutMs: number, checkedAt: string): Promise<ProbeResult<ServiceStatus>> {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return { health: 'unknown', lastSeen: null };
  }
  if ((parsed.protocol !== 'http:' && parsed.protocol !== 'https:') || parsed.username || parsed.password) {
    return { health: 'unknown', lastSeen: null };
  }

  const request = async (method: 'HEAD' | 'GET'): Promise<number> => {
    const response = await fetch(parsed, {
      method,
      redirect: 'follow',
      signal: AbortSignal.timeout(timeoutMs),
      headers: method === 'GET' ? { range: 'bytes=0-0' } : undefined,
    });
    const status = response.status;
    if (response.body) await response.body.cancel().catch(() => undefined);
    return status;
  };

  try {
    let status = await request('HEAD');
    if (status === 405 || status === 501) status = await request('GET');
    return { health: status >= 200 && status < 300 ? 'live' : 'dead', lastSeen: checkedAt };
  } catch {
    return { health: 'dead', lastSeen: checkedAt };
  }
}

async function probeTcp(
  host: string | null,
  port: number,
  protocol: PortRow['protocol'],
  timeoutMs: number,
  checkedAt: string,
): Promise<ProbeResult<PortHealth>> {
  if (protocol !== 'tcp' || !host?.trim() || isIP(host) === 0) return { health: 'unknown', lastSeen: null };
  return await new Promise((resolve) => {
    let settled = false;
    let socket: ReturnType<typeof createConnection> | undefined;
    const finish = (health: PortHealth, lastSeen: string | null) => {
      if (settled) return;
      settled = true;
      socket?.destroy();
      resolve({ health, lastSeen });
    };
    try {
      socket = createConnection({ host, port });
    } catch {
      finish('unknown', null);
      return;
    }
    socket.setTimeout(timeoutMs, () => finish('down', checkedAt));
    socket.once('connect', () => finish('up', checkedAt));
    socket.once('error', () => finish('down', checkedAt));
  });
}

function serviceStatusFromPorts(results: ProbeResult<PortHealth>[]): ProbeResult<ServiceStatus> {
  if (results.length === 0) return { health: 'unknown', lastSeen: null };
  const lastSeen = results.map((result) => result.lastSeen).filter((value): value is string => value !== null).sort().at(-1) ?? null;
  if (results.some((result) => result.health === 'up')) return { health: 'live', lastSeen };
  if (results.every((result) => result.health === 'down')) return { health: 'dead', lastSeen };
  return { health: 'unknown', lastSeen };
}

export async function runCheckCycle(options: CheckCycleOptions = {}): Promise<CheckCycleSummary> {
  const timeoutMs = checkedTimeout(options.timeoutMs);
  const concurrency = checkedConcurrency(options.concurrency);
  const checkedAt = (options.now?.() ?? new Date()).toISOString();
  const dbPath = options.dbPath ?? defaultDbPath();
  if (!existsSync(dbPath)) throw new Error(`No registry at ${dbPath}. Seed it before running health checks.`);
  const db = openForWrite(dbPath);

  try {
    const services = db.prepare('SELECT id, url FROM service ORDER BY id').all() as ServiceRow[];
    const ports = db.prepare(
      `SELECT p.id, p.service_id, p.machine_id, p.port, p.protocol, m.tailnet_ip AS host
       FROM port p JOIN machine m ON m.id = p.machine_id
       ORDER BY p.id`,
    ).all() as PortRow[];

    const portResults = await mapConcurrent(ports, concurrency, (row) => probeTcp(row.host, row.port, row.protocol, timeoutMs, checkedAt));
    const portsByService = new Map<string, ProbeResult<PortHealth>[]>();
    for (let index = 0; index < ports.length; index += 1) {
      const serviceId = ports[index]!.service_id;
      if (!serviceId) continue;
      const results = portsByService.get(serviceId) ?? [];
      results.push(portResults[index]!);
      portsByService.set(serviceId, results);
    }

    const serviceResults = await mapConcurrent(services, concurrency, async (row) => {
      if (row.url?.trim()) return await probeHttp(row.url, timeoutMs, checkedAt);
      return serviceStatusFromPorts(portsByService.get(row.id) ?? []);
    });

    const updateService = db.prepare('UPDATE service SET live_status = ?, health = ?, last_seen = ? WHERE id = ?');
    const updatePort = db.prepare('UPDATE port SET health = ?, last_seen = ? WHERE id = ?');
    const commit = db.transaction(() => {
      for (let index = 0; index < services.length; index += 1) {
        const result = serviceResults[index]!;
        const health = result.health === 'live' ? 'up' : result.health === 'dead' ? 'down' : 'unknown';
        updateService.run(result.health, health, result.lastSeen, services[index]!.id);
      }
      for (let index = 0; index < ports.length; index += 1) {
        const result = portResults[index]!;
        updatePort.run(result.health, result.lastSeen, ports[index]!.id);
      }
      setMeta(db, 'last_health_check_at', checkedAt);
    });
    commit();

    return {
      checkedAt,
      services: {
        total: serviceResults.length,
        live: serviceResults.filter((result) => result.health === 'live').length,
        dead: serviceResults.filter((result) => result.health === 'dead').length,
        unknown: serviceResults.filter((result) => result.health === 'unknown').length,
      },
      ports: {
        total: portResults.length,
        up: portResults.filter((result) => result.health === 'up').length,
        down: portResults.filter((result) => result.health === 'down').length,
        unknown: portResults.filter((result) => result.health === 'unknown').length,
      },
    };
  } finally {
    db.close();
  }
}

