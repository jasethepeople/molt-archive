import http from 'node:http';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Router } from '../src/router.js';
import { createServer } from '../src/server.js';
import { LocalTestApprovalIssuer } from './support/local-test-approval-issuer.js';
import type { ExecuteRequest, ExecuteResponse } from '../src/types.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const ROOT_DIR = path.resolve(__dirname, '..');
export const TEST_CALLER_ID = 'test-caller';

export interface TestRuntime {
  router: Router;
  approvalIssuer: LocalTestApprovalIssuer;
  callerId: string;
  close: () => Promise<void>;
}

export interface RunningServer {
  server: http.Server;
  port: number;
  baseUrl: string;
  approvalIssuer: LocalTestApprovalIssuer;
  close: () => Promise<void>;
}

export async function createTestRuntime(rootDir: string = ROOT_DIR): Promise<TestRuntime> {
  const ledgerRoot = await mkdtemp(path.join(os.tmpdir(), 'gateway-approval-runtime-'));
  const approvalIssuer = new LocalTestApprovalIssuer(ledgerRoot);
  return {
    router: new Router(rootDir, { approvalAuthority: approvalIssuer }),
    approvalIssuer,
    callerId: TEST_CALLER_ID,
    close: () => rm(ledgerRoot, { recursive: true, force: true }),
  };
}

export async function executeApproved(
  runtime: TestRuntime,
  request: ExecuteRequest,
): Promise<ExecuteResponse> {
  const approvalToken = await runtime.approvalIssuer.issueForTest({
    callerId: runtime.callerId,
    target: request.target,
    event: request.event,
  });
  return runtime.router.execute({ ...request, approvalToken }, runtime.callerId);
}

export async function startServer(
  rootDir: string = ROOT_DIR,
  options: { approvalIssuer?: LocalTestApprovalIssuer; callerId?: string } = {},
): Promise<RunningServer> {
  const ownedLedgerRoot = options.approvalIssuer
    ? undefined
    : await mkdtemp(path.join(os.tmpdir(), 'gateway-approval-server-'));
  const approvalIssuer = options.approvalIssuer ?? new LocalTestApprovalIssuer(ownedLedgerRoot!);
  const server = createServer(rootDir, {
    approvalAuthority: approvalIssuer,
    resolveCaller: (req) => {
      const caller = req.headers['x-test-caller-id'];
      return typeof caller === 'string' ? caller : options.callerId;
    },
  });
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      server.off('error', reject);
      const addr = server.address();
      if (!addr || typeof addr === 'string') return reject(new Error('server did not bind TCP'));
      resolve({
        server,
        port: addr.port,
        baseUrl: `http://127.0.0.1:${addr.port}`,
        approvalIssuer,
        close: () => new Promise<void>((done) => {
          server.close(async () => {
            if (ownedLedgerRoot) await rm(ownedLedgerRoot, { recursive: true, force: true });
            done();
          });
        }),
      });
    });
  });
}

export async function issueTestApproval(
  server: RunningServer,
  target: string,
  event: unknown,
  callerId = TEST_CALLER_ID,
  issuedAtMs?: number,
): Promise<string> {
  return server.approvalIssuer.issueForTest({ callerId, target, event, issuedAtMs });
}

export async function postJson(
  url: string,
  body: unknown,
  options: { approvalToken?: string; callerId?: string } = {},
): Promise<{ status: number; json: any }> {
  const headers: Record<string, string> = { 'content-type': 'application/json' };
  if (options.approvalToken) headers['x-approval-token'] = options.approvalToken;
  if (options.callerId) headers['x-test-caller-id'] = options.callerId;
  const res = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  });
  return { status: res.status, json: await res.json() };
}

export interface RecordedRequest {
  url: string;
  method: string;
  headers: Record<string, string>;
  body?: unknown;
  rawBody?: string;
}

export function makeFakeFetch(
  handler: (req: RecordedRequest) => { status?: number; body: unknown },
): { fakeFetch: typeof fetch; requests: RecordedRequest[] } {
  const requests: RecordedRequest[] = [];
  const fakeFetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    const method = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase();
    const hdrs = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
    const headers: Record<string, string> = {};
    hdrs.forEach((v, k) => (headers[k.toLowerCase()] = v));
    const rawBody = typeof init?.body === 'string' ? init.body : undefined;
    let body: unknown;
    if (rawBody) {
      try {
        body = JSON.parse(rawBody);
      } catch {
        body = rawBody;
      }
    }
    const rec: RecordedRequest = { url, method, headers, body, rawBody };
    requests.push(rec);
    const canned = handler(rec);
    return new Response(JSON.stringify(canned.body), {
      status: canned.status ?? 200,
      headers: { 'content-type': 'application/json' },
    });
  }) as unknown as typeof fetch;
  return { fakeFetch, requests };
}
