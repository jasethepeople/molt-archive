import { createHash, createPublicKey, verify } from 'node:crypto';
import { appendFile, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import { FailClosedApprovalAuthority } from '../src/approval.js';
import { Router } from '../src/router.js';
import { createServer } from '../src/server.js';
import { APPROVAL_TTL_MS, LocalTestApprovalIssuer } from './support/local-test-approval-issuer.js';
import { issueTestApproval, postJson, startServer, TEST_CALLER_ID, type RunningServer } from './helpers.js';

const TARGET_A = 'native/approval-probe';
const TARGET_B = 'native/approval-probe-b';
const ENTRY_POINTS = ['REST', 'A2A'] as const;
const ZERO_HASH = '0'.repeat(64);

type Invocation = {
  status: number;
  ok: boolean;
  error?: { code?: string; message?: string };
  attempts?: number;
  taskState?: string;
};

let projectRoot: string;
let runFile: string;
let server: RunningServer;
let requestId = 100;

async function runCount(): Promise<number> {
  const contents = await readFile(runFile, 'utf8');
  return contents.split('\n').filter(Boolean).length;
}

async function resetRuns(): Promise<void> {
  await writeFile(runFile, '');
}

async function invoke(
  entryPoint: typeof ENTRY_POINTS[number],
  input: { target: string; event?: unknown; token?: string; callerId?: string },
): Promise<Invocation> {
  const callerId = input.callerId ?? TEST_CALLER_ID;
  if (entryPoint === 'REST') {
    const { status, json } = await postJson(
      `${server.baseUrl}/api/v1/execute`,
      { target: input.target, event: input.event, auth: { access_token: 'app-credential-that-must-not-be-used-on-denial' } },
      { approvalToken: input.token, callerId },
    );
    return { status, ok: json.ok, error: json.error, attempts: json.attempts };
  }
  const data: Record<string, unknown> = { target: input.target, event: input.event };
  if (input.token !== undefined) data.approvalToken = input.token;
  const res = await fetch(`${server.baseUrl}/a2a`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-test-caller-id': callerId },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: requestId++,
      method: 'message/send',
      params: {
        message: {
          kind: 'message',
          messageId: `approval-${requestId}`,
          role: 'user',
          parts: [{ kind: 'data', data }],
        },
      },
    }),
  });
  const json = await res.json() as any;
  const task = json.result;
  const result = task?.artifacts?.[0]?.parts?.[0]?.data ?? {};
  return {
    status: res.status,
    ok: result.ok,
    error: result.error,
    attempts: result.attempts,
    taskState: task?.status?.state,
  };
}

function expectDenied(entryPoint: typeof ENTRY_POINTS[number], result: Invocation): void {
  expect(result.ok).toBe(false);
  expect(result.error?.code).toBe('approval_denied');
  expect(result.attempts).toBe(0);
  if (entryPoint === 'REST') expect(result.status).toBe(403);
  else {
    expect(result.status).toBe(200);
    expect(result.taskState).toBe('failed');
  }
}

function sha256(value: string): string {
  return createHash('sha256').update(value).digest('hex');
}

function canonicalValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return Object.fromEntries(Object.keys(record).sort().map((key) => [key, canonicalValue(record[key])]));
  }
  return value;
}

function canonicalJson(value: unknown): string {
  const serialized = JSON.stringify(canonicalValue(value));
  return serialized === undefined ? 'null' : serialized;
}

async function verifyLedgerFile(filePath: string): Promise<any[]> {
  const raw = await readFile(filePath, 'utf8');
  const events = raw.trim().split('\n').filter(Boolean).map((line) => JSON.parse(line));
  let previousHash = ZERO_HASH;
  for (const event of events) {
    expect(event.previousHash).toBe(previousHash);
    const core = {
      id: event.id,
      type: event.type,
      timestamp: event.timestamp,
      epoch: event.epoch,
      payload: event.payload,
      previousHash: event.previousHash,
    };
    const eventHash = sha256(canonicalJson(core));
    expect(event.eventHash).toBe(eventHash);
    expect(verify(
      null,
      Buffer.from(event.eventHash),
      createPublicKey(event.publicKey),
      Buffer.from(event.signature, 'base64url'),
    )).toBe(true);
    previousHash = event.eventHash;
  }
  return events;
}

beforeAll(async () => {
  projectRoot = await mkdtemp(path.join(tmpdir(), 'gateway-approval-project-'));
  runFile = path.join(projectRoot, 'component-runs.jsonl');
  await writeFile(runFile, '');
  for (const moduleName of ['approval-probe', 'approval-probe-b']) {
    const componentDir = path.join(projectRoot, 'registry', 'native', moduleName);
    await mkdir(componentDir, { recursive: true });
    await writeFile(path.join(componentDir, 'index.ts'), `import { appendFile } from 'node:fs/promises';
export default {
  async run(_ctx: unknown, event: unknown) {
    await appendFile(process.env.APPROVAL_PROBE_RUNS!, JSON.stringify(event) + '\\n');
    return { ran: true, event };
  },
};
`);
  }
  process.env.APPROVAL_PROBE_RUNS = runFile;
  server = await startServer(projectRoot);
});

afterAll(async () => {
  await server.close();
  delete process.env.APPROVAL_PROBE_RUNS;
  await rm(projectRoot, { recursive: true, force: true });
});

describe.each(ENTRY_POINTS)('%s approval gate bypass resistance', (entryPoint) => {
  it('rejects a call with no token before the component runs', async () => {
    await resetRuns();
    const result = await invoke(entryPoint, { target: TARGET_A, event: { case: 'missing' } });
    expectDenied(entryPoint, result);
    expect(await runCount()).toBe(0);
  });

  it('rejects a token expired beyond the 10-minute TTL before the component runs', async () => {
    await resetRuns();
    const event = { case: 'expired' };
    const token = await issueTestApproval(
      server,
      TARGET_A,
      event,
      TEST_CALLER_ID,
      Date.now() - APPROVAL_TTL_MS - 1,
    );
    const result = await invoke(entryPoint, { target: TARGET_A, event, token });
    expectDenied(entryPoint, result);
    expect(await runCount()).toBe(0);
  });

  it('allows the first use of a token and refuses its replay without a second component run', async () => {
    await resetRuns();
    const event = { case: 'replay' };
    const token = await issueTestApproval(server, TARGET_A, event);
    const first = await invoke(entryPoint, { target: TARGET_A, event, token });
    expect(first.ok).toBe(true);
    expect(await runCount()).toBe(1);
    const replay = await invoke(entryPoint, { target: TARGET_A, event, token });
    expectDenied(entryPoint, replay);
    expect(await runCount()).toBe(1);
  });

  it('rejects cross-target replay before either probe component runs', async () => {
    await resetRuns();
    const eventA = { case: 'cross-target-A' };
    const eventB = { case: 'cross-target-B' };
    const token = await issueTestApproval(server, TARGET_A, eventA);
    const result = await invoke(entryPoint, { target: TARGET_B, event: eventB, token });
    expectDenied(entryPoint, result);
    expect(await runCount()).toBe(0);
  });

  it('rejects a changed event payload before the component runs', async () => {
    await resetRuns();
    const eventA = { case: 'event-A' };
    const eventB = { case: 'event-B' };
    const token = await issueTestApproval(server, TARGET_A, eventA);
    const result = await invoke(entryPoint, { target: TARGET_A, event: eventB, token });
    expectDenied(entryPoint, result);
    expect(await runCount()).toBe(0);
  });

  it('rejects a token not chained to an existing signed ledger event', async () => {
    await resetRuns();
    const event = { case: 'detached' };
    const token = await server.approvalIssuer.issueDetachedTokenForTest({
      callerId: TEST_CALLER_ID,
      target: TARGET_A,
      event,
    });
    const result = await invoke(entryPoint, { target: TARGET_A, event, token });
    expectDenied(entryPoint, result);
    expect(await runCount()).toBe(0);
  });

  it('rejects a token issued to a different caller before the component runs', async () => {
    await resetRuns();
    const event = { case: 'caller-binding' };
    const token = await issueTestApproval(server, TARGET_A, event, 'caller-A');
    const result = await invoke(entryPoint, {
      target: TARGET_A,
      event,
      token,
      callerId: 'caller-B',
    });
    expectDenied(entryPoint, result);
    expect(await runCount()).toBe(0);
  });

  it('atomically permits only one of two concurrent uses', async () => {
    await resetRuns();
    const event = { case: 'concurrent-replay' };
    const token = await issueTestApproval(server, TARGET_A, event);
    const results = await Promise.all([
      invoke(entryPoint, { target: TARGET_A, event, token }),
      invoke(entryPoint, { target: TARGET_A, event, token }),
    ]);
    expect(results.filter((result) => result.ok)).toHaveLength(1);
    expectDenied(entryPoint, results.find((result) => !result.ok)!);
    expect(await runCount()).toBe(1);
  });
});

describe('approval gate evidence and restart behavior', () => {
  it('executes with a valid token and writes a hash-chained, signed authorization and completion ledger', async () => {
    await resetRuns();
    const event = { case: 'signed-ledger' };
    const token = await issueTestApproval(server, TARGET_A, event);
    const result = await invoke('REST', { target: TARGET_A, event, token });
    expect(result.ok).toBe(true);
    expect(await runCount()).toBe(1);
    const events = await verifyLedgerFile(server.approvalIssuer.ledgerFilePath);
    const payloadHash = sha256(canonicalJson(event));
    const authorization = events.find((item) => item.type === 'execution.authorized' && item.payload.target === TARGET_A && item.payload.payloadHash === payloadHash)!;
    const completion = events.find((item) => item.type === 'execution.completed' && item.payload.executionId === authorization.payload.executionId);
    const linkedApproval = events.find((item) => item.id === authorization.payload.approvalEventId);
    expect(events.some((item) => item.type === 'approval.granted')).toBe(true);
    expect(authorization).toBeDefined();
    expect(completion).toBeDefined();
    expect(linkedApproval?.type).toBe('approval.granted');
    expect(authorization.payload.approvalEventHash).toBe(linkedApproval?.eventHash);
    expect(completion?.payload.approvalEventId).toBe(linkedApproval?.id);
  });

  it('voids an outstanding token after a fresh gateway authority starts', async () => {
    await resetRuns();
    const ledgerRoot = await mkdtemp(path.join(tmpdir(), 'gateway-approval-restart-'));
    const project = projectRoot;
    const callerId = 'restart-caller';
    const firstIssuer = new LocalTestApprovalIssuer(ledgerRoot);
    const firstServer = await startServer(project, { approvalIssuer: firstIssuer, callerId });
    let secondServer: RunningServer | undefined;
    let firstServerClosed = false;
    try {
      const acceptedEvent = { case: 'before-restart-accepted' };
      const acceptedToken = await firstIssuer.issueForTest({ callerId, target: TARGET_A, event: acceptedEvent });
      const accepted = await postJson(`${firstServer.baseUrl}/api/v1/execute`, {
        target: TARGET_A,
        event: acceptedEvent,
      }, { approvalToken: acceptedToken, callerId });
      expect(accepted.status).toBe(200);
      expect(accepted.json.ok).toBe(true);

      const outstandingEvent = { case: 'outstanding-across-restart' };
      const outstandingToken = await firstIssuer.issueForTest({ callerId, target: TARGET_A, event: outstandingEvent });
      expect(firstIssuer.inspectForTest({
        token: outstandingToken,
        callerId,
        target: TARGET_A,
        event: outstandingEvent,
      })).toBe(true);
      await firstServer.close();
      firstServerClosed = true;

      const secondIssuer = new LocalTestApprovalIssuer(ledgerRoot);
      expect(secondIssuer.inspectForTest({
        token: outstandingToken,
        callerId,
        target: TARGET_A,
        event: outstandingEvent,
      })).toBe(false);
      secondServer = await startServer(project, { approvalIssuer: secondIssuer, callerId });
      await resetRuns();
      const afterRestart = await postJson(`${secondServer.baseUrl}/api/v1/execute`, {
        target: TARGET_A,
        event: outstandingEvent,
      }, { approvalToken: outstandingToken, callerId });
      expect(afterRestart.status).toBe(403);
      expect(afterRestart.json.ok).toBe(false);
      expect(afterRestart.json.error.code).toBe('approval_denied');
      expect(afterRestart.json.attempts).toBe(0);
      expect(await runCount()).toBe(0);
    } finally {
      if (secondServer) await secondServer.close();
      else if (!firstServerClosed) await firstServer.close();
      await rm(ledgerRoot, { recursive: true, force: true });
    }
  });

  it('rejects even a test-issued token on the unconfigured production server', async () => {
    await resetRuns();
    const ledgerRoot = await mkdtemp(path.join(tmpdir(), 'gateway-approval-untrusted-issuer-'));
    const issuer = new LocalTestApprovalIssuer(ledgerRoot);
    const event = { case: 'unconfigured-default' };
    const token = await issuer.issueForTest({ callerId: TEST_CALLER_ID, target: TARGET_A, event });
    const defaultServer = createServer(projectRoot);
    await new Promise<void>((resolve, reject) => {
      defaultServer.once('error', reject);
      defaultServer.listen(0, '127.0.0.1', resolve);
    });
    const address = defaultServer.address();
    if (!address || typeof address === 'string') throw new Error('default server failed to bind');
    try {
      const result = await postJson(
        `http://127.0.0.1:${address.port}/api/v1/execute`,
        { target: TARGET_A, event },
        { approvalToken: token, callerId: TEST_CALLER_ID },
      );
      expectDenied('REST', { status: result.status, ...result.json });
      expect(await runCount()).toBe(0);
    } finally {
      await new Promise<void>((resolve) => defaultServer.close(() => resolve()));
      await rm(ledgerRoot, { recursive: true, force: true });
    }
  });

  it('fails closed when the approval authority is unavailable', async () => {
    const loaderLoad = vi.fn();
    const root = await mkdtemp(path.join(tmpdir(), 'gateway-approval-partition-'));
    const authority = {
      consume: vi.fn(async () => { throw new Error('approval authority unavailable'); }),
      recordExecution: vi.fn(async () => undefined),
    };
    const router = new Router(root, { approvalAuthority: authority as any, loader: { load: loaderLoad } as any });
    const result = await router.execute({ target: TARGET_A, event: { case: 'partition' } }, TEST_CALLER_ID);
    expect(result.ok).toBe(false);
    expect(result.error?.code).toBe('approval_denied');
    expect(result.attempts).toBe(0);
    expect(loaderLoad).not.toHaveBeenCalled();
    expect(authority.recordExecution).not.toHaveBeenCalled();
    await rm(root, { recursive: true, force: true });
  });

  it('does not inspect request app credentials or load a component on rejection', async () => {
    const loaderLoad = vi.fn();
    const root = await mkdtemp(path.join(tmpdir(), 'gateway-approval-no-credentials-'));
    const router = new Router(root, {
      approvalAuthority: new FailClosedApprovalAuthority(),
      loader: { load: loaderLoad } as any,
    });
    let credentialReads = 0;
    const request: any = { target: TARGET_A, event: { case: 'credential-order' } };
    Object.defineProperty(request, 'auth', {
      get() {
        credentialReads += 1;
        throw new Error('app credential getter touched before approval');
      },
    });
    const result = await router.execute(request, TEST_CALLER_ID);
    expectDenied('REST', { status: 403, ...result });
    expect(credentialReads).toBe(0);
    expect(loaderLoad).not.toHaveBeenCalled();
    await rm(root, { recursive: true, force: true });
  });
});
