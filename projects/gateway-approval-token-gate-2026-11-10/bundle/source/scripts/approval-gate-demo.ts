import { createHash, createPublicKey, verify } from 'node:crypto';
import { copyFile, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { APPROVAL_TTL_MS, LocalTestApprovalIssuer } from '../tests/support/local-test-approval-issuer.js';
import { createServer, ROOT_DIR } from '../src/server.js';

const projectDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const outputDir = path.join(projectDir, 'demo-output');
const callerId = 'local-demo-caller';
const port = Number(process.env.PORT ?? 3456);

type Entry = 'REST' | 'A2A';
type ResponseData = { ok: boolean; target: string; attempts: number; status: number; taskState?: string; result?: unknown; error?: unknown };

type Runtime = {
  server: http.Server;
  baseUrl: string;
  issuer: LocalTestApprovalIssuer;
};

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonical(record[key])}`).join(',')}}`;
  }
  const serialized = JSON.stringify(value);
  return serialized === undefined ? 'null' : serialized;
}

async function startServer(issuerDir: string): Promise<Runtime> {
  const issuer = new LocalTestApprovalIssuer(issuerDir);
  const server = createServer(ROOT_DIR, {
    approvalAuthority: issuer,
    resolveCaller: (request) => {
      const id = request.headers['x-test-caller-id'];
      return typeof id === 'string' ? id : undefined;
    },
  });
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', resolve);
  });
  return { server, baseUrl: `http://127.0.0.1:${port}`, issuer };
}

async function stopServer(runtime: Runtime): Promise<void> {
  if (!runtime.server.listening) return;
  await new Promise<void>((resolve, reject) => {
    runtime.server.close((error) => error ? reject(error) : resolve());
  });
}

async function invoke(runtime: Runtime, entry: Entry, target: string, event: unknown, token?: string): Promise<ResponseData> {
  if (entry === 'REST') {
    const headers: Record<string, string> = {
      'content-type': 'application/json',
      'x-test-caller-id': callerId,
      connection: 'close',
    };
    if (token) headers['x-approval-token'] = token;
    const response = await fetch(`${runtime.baseUrl}/api/v1/execute`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ target, event }),
    });
    return { ...await response.json() as Omit<ResponseData, 'status'>, status: response.status };
  }
  const response = await fetch(`${runtime.baseUrl}/a2a`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-test-caller-id': callerId, connection: 'close' },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'message/send',
      params: {
        message: {
          kind: 'message',
          messageId: 'local-demo',
          role: 'user',
          parts: [{ kind: 'data', data: { target, event, approvalToken: token } }],
        },
      },
    }),
  });
  const rpc = await response.json() as any;
  return {
    ...rpc.result.artifacts[0].parts[0].data,
    status: response.status,
    taskState: rpc.result.status.state,
  } as ResponseData;
}

async function ledgerRecords(issuer: LocalTestApprovalIssuer): Promise<any[]> {
  try {
    return (await readFile(issuer.ledgerFilePath, 'utf8'))
      .trim()
      .split('\n')
      .filter(Boolean)
      .map((line) => JSON.parse(line));
  } catch {
    return [];
  }
}

function count(records: any[], type: string): number {
  return records.filter((record) => record.type === type).length;
}

function isRefused(entry: Entry, result: ResponseData): boolean {
  return !result.ok &&
    result.attempts === 0 &&
    result.status === (entry === 'REST' ? 403 : 200) &&
    (entry === 'REST' || result.taskState === 'failed');
}

function assertSignedLedger(records: any[]): void {
  let previousHash = '0'.repeat(64);
  for (const record of records) {
    const core = {
      id: record.id,
      type: record.type,
      timestamp: record.timestamp,
      epoch: record.epoch,
      payload: record.payload,
      previousHash: record.previousHash,
    };
    const eventHash = createHash('sha256').update(canonical(core)).digest('hex');
    if (record.previousHash !== previousHash || record.eventHash !== eventHash) {
      throw new Error('approval ledger hash chain verification failed');
    }
    if (!verify(null, Buffer.from(eventHash), createPublicKey(record.publicKey), Buffer.from(record.signature, 'base64url'))) {
      throw new Error('approval ledger signature verification failed');
    }
    previousHash = eventHash;
  }
}

async function run(): Promise<void> {
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error(`invalid PORT: ${port}`);
  await rm(outputDir, { recursive: true, force: true });
  await mkdir(outputDir, { recursive: true });
  const beforeDir = path.join(outputDir, 'before-restart');
  const afterDir = path.join(outputDir, 'after-restart');
  let runtime = await startServer(beforeDir);
  const checks: any[] = [];

  try {
    const health = await fetch(`${runtime.baseUrl}/health`, { headers: { connection: 'close' } }).then((response) => response.json() as Promise<{ ok: boolean }>);
    if (!health.ok) throw new Error('loopback /health smoke check failed');

    const smokeEvent = { hello: 'approval-gate-demo' };
    const smokeToken = await runtime.issuer.issueForTest({ callerId, target: 'native/echo', event: smokeEvent });
    const smoke = await invoke(runtime, 'REST', 'native/echo', smokeEvent, smokeToken);
    if (!smoke.ok || (smoke.result as any)?.echo?.hello !== smokeEvent.hello) {
      throw new Error('loopback native/echo smoke check failed');
    }

    for (const entry of ['REST', 'A2A'] as const) {
      const missingEvent = { case: 'missing-token', entry };
      const missing = await invoke(runtime, entry, 'native/echo', missingEvent);
      let records = await ledgerRecords(runtime.issuer);
      const authCount = count(records, 'execution.authorized');
      if (!isRefused(entry, missing) || count(records, 'execution.authorized') !== authCount) {
        throw new Error(`${entry} missing-token gate check failed`);
      }
      checks.push({ entrypoint: entry, bypass: 'missing token', refused: true, attempts: missing.attempts, componentExecuted: false });

      const expiredEvent = { case: 'expired-token', entry };
      const expiredToken = await runtime.issuer.issueForTest({
        callerId,
        target: 'native/echo',
        event: expiredEvent,
        issuedAtMs: Date.now() - APPROVAL_TTL_MS - 1,
      });
      records = await ledgerRecords(runtime.issuer);
      const beforeExpired = count(records, 'execution.authorized');
      const expired = await invoke(runtime, entry, 'native/echo', expiredEvent, expiredToken);
      records = await ledgerRecords(runtime.issuer);
      if (!isRefused(entry, expired) || count(records, 'execution.authorized') !== beforeExpired) {
        throw new Error(`${entry} expired-token gate check failed`);
      }
      checks.push({ entrypoint: entry, bypass: 'expired token', refused: true, attempts: expired.attempts, componentExecuted: false });

      const replayEvent = { case: 'replayed-token', entry };
      const replayToken = await runtime.issuer.issueForTest({ callerId, target: 'native/echo', event: replayEvent });
      const first = await invoke(runtime, entry, 'native/echo', replayEvent, replayToken);
      if (!first.ok) throw new Error(`${entry} first single-use call did not execute`);
      records = await ledgerRecords(runtime.issuer);
      const beforeReplay = count(records, 'execution.authorized');
      const replay = await invoke(runtime, entry, 'native/echo', replayEvent, replayToken);
      records = await ledgerRecords(runtime.issuer);
      if (!isRefused(entry, replay) || count(records, 'execution.authorized') !== beforeReplay) {
        throw new Error(`${entry} replay gate check failed`);
      }
      checks.push({ entrypoint: entry, bypass: 'replayed token', refused: true, attempts: replay.attempts, componentExecuted: false, firstCallExecuted: true });

      const targetEvent = { case: 'cross-target', entry };
      const targetToken = await runtime.issuer.issueForTest({ callerId, target: 'native/echo', event: targetEvent });
      records = await ledgerRecords(runtime.issuer);
      const beforeTarget = count(records, 'execution.authorized');
      const crossTarget = await invoke(runtime, entry, 'native/transform', targetEvent, targetToken);
      records = await ledgerRecords(runtime.issuer);
      if (!isRefused(entry, crossTarget) || count(records, 'execution.authorized') !== beforeTarget) {
        throw new Error(`${entry} cross-target replay gate check failed`);
      }
      checks.push({ entrypoint: entry, bypass: 'cross-target replay', refused: true, attempts: crossTarget.attempts, componentExecuted: false });
    }

    const a2aEvent = { hello: 'signed-a2a-event' };
    const a2aToken = await runtime.issuer.issueForTest({ callerId, target: 'native/echo', event: a2aEvent });
    const a2aSuccess = await invoke(runtime, 'A2A', 'native/echo', a2aEvent, a2aToken);
    if (!a2aSuccess.ok) throw new Error('A2A valid-token call did not execute');

    const restartEvent = { hello: 'accepted-before-restart' };
    const acceptedBeforeRestart = await invoke(
      runtime,
      'REST',
      'native/echo',
      restartEvent,
      await runtime.issuer.issueForTest({ callerId, target: 'native/echo', event: restartEvent }),
    );
    if (!acceptedBeforeRestart.ok) throw new Error('pre-restart valid-token call did not execute');
    const outstandingEvent = { hello: 'outstanding-at-restart' };
    const outstandingToken = await runtime.issuer.issueForTest({ callerId, target: 'native/echo', event: outstandingEvent });

    const beforeRecords = await ledgerRecords(runtime.issuer);
    assertSignedLedger(beforeRecords);
    await copyFile(runtime.issuer.ledgerFilePath, path.join(outputDir, 'ledger-before-restart.jsonl'));
    await rm(beforeDir, { recursive: true, force: true });
    await rm(beforeDir, { recursive: true, force: true });
    await stopServer(runtime);

    runtime = await startServer(afterDir);
    const afterRestart = await invoke(runtime, 'REST', 'native/echo', outstandingEvent, outstandingToken);
    const afterRecords = await ledgerRecords(runtime.issuer);
    if (!isRefused('REST', afterRestart) || count(afterRecords, 'execution.authorized') !== 0) {
      throw new Error('restart did not void the outstanding approval');
    }
    checks.push({ entrypoint: 'REST', bypass: 'outstanding token after restart', refused: true, attempts: afterRestart.attempts, componentExecuted: false });

    await writeFile(path.join(outputDir, 'ledger-after-restart.jsonl'), afterRecords.map((record) => JSON.stringify(record)).join('\n') + (afterRecords.length ? '\n' : ''), 'utf8');
    const report = {
      port,
      bind: '127.0.0.1 only',
      healthOk: health.ok,
      nativeEchoOk: smoke.ok,
      validA2aTokenOk: a2aSuccess.ok,
      acceptedBeforeRestart: acceptedBeforeRestart.ok,
      outstandingApprovalRefusedAfterRestart: !afterRestart.ok,
      restartMethod: 'fresh in-process server and authority instance',
      signedLedgerVerifiedBeforeRestart: true,
      restartMethod: 'fresh server and authority instance in the same Node process',
      checks,
      noPublicServiceOrDeployment: true,
    };
    await writeFile(path.join(outputDir, 'demo-report.json'), `${JSON.stringify(report, null, 2)}\n`, 'utf8');
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  } finally {
    await stopServer(runtime);
  }
}

run().catch((error) => {
  console.error(error instanceof Error ? error.stack ?? error.message : error);
  process.exitCode = 1;
});
