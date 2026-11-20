import { createHash, generateKeyPairSync, randomUUID, sign, verify, type KeyObject } from 'node:crypto';
import { appendFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import type { ApprovalAuthority, ApprovalCheck, ApprovalGrant, ApprovalOutcome } from '../../src/approval.js';

export const APPROVAL_TTL_MS = 10 * 60 * 1000;
const TEST_ISSUER = 'LOCAL_TEST_ONLY';
const ZERO_HASH = '0'.repeat(64);

type JsonRecord = Record<string, unknown>;

interface ApprovalClaims {
  version: 1;
  issuer: typeof TEST_ISSUER;
  tokenId: string;
  callerId: string;
  target: string;
  payloadHash: string;
  approvalEventId: string;
  approvalEventHash: string;
  issuedAtMs: number;
  expiresAtMs: number;
  epoch: string;
}

interface SignedLedgerEvent {
  id: string;
  type: string;
  timestamp: string;
  epoch: string;
  payload: JsonRecord;
  previousHash: string;
  eventHash: string;
  signature: string;
  publicKey: string;
}

function canonicalValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (value && typeof value === 'object') {
    const record = value as JsonRecord;
    return Object.fromEntries(Object.keys(record).sort().map((key) => [key, canonicalValue(record[key])]));
  }
  return value;
}

function canonicalJson(value: unknown): string {
  const serialized = JSON.stringify(canonicalValue(value));
  return serialized === undefined ? 'null' : serialized;
}

function sha256(value: string): string {
  return createHash('sha256').update(value).digest('hex');
}

function validId(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 512 && value.trim() === value;
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export class LocalTestApprovalIssuer implements ApprovalAuthority {
  readonly epoch = randomUUID();
  readonly ledgerFilePath: string;
  private readonly privateKey: KeyObject;
  private readonly publicKey: KeyObject;
  private readonly publicKeyPem: string;
  private readonly eventsById = new Map<string, SignedLedgerEvent>();
  private readonly eventsByHash = new Map<string, SignedLedgerEvent>();
  private readonly consumed = new Set<string>();
  private readonly authorized = new Map<string, ApprovalGrant>();
  private readonly finalized = new Set<string>();
  private previousHash = ZERO_HASH;
  private appendQueue: Promise<void> = Promise.resolve();

  constructor(rootDir: string, private readonly now: () => number = Date.now) {
    const pair = generateKeyPairSync('ed25519');
    this.privateKey = pair.privateKey;
    this.publicKey = pair.publicKey;
    this.publicKeyPem = pair.publicKey.export({ type: 'spki', format: 'pem' }).toString();
    this.ledgerFilePath = path.join(rootDir, 'logs', `approval-ledger-${this.epoch}.jsonl`);
  }

  async issueForTest(input: {
    callerId: string;
    target: string;
    event?: unknown;
    issuedAtMs?: number;
  }): Promise<string> {
    const callerId = input.callerId;
    const target = input.target;
    const issuedAtMs = input.issuedAtMs ?? this.now();
    if (!validId(callerId) || !validId(target) || !Number.isSafeInteger(issuedAtMs)) {
      throw new Error('LOCAL_TEST_ONLY issuer requires valid caller, target, and issuance time');
    }
    const tokenId = randomUUID();
    const payloadHash = sha256(canonicalJson(input.event ?? null));
    const expiresAtMs = issuedAtMs + APPROVAL_TTL_MS;
    const approvalEvent = await this.appendEvent('approval.granted', {
      issuer: TEST_ISSUER,
      tokenId,
      callerId,
      target,
      payloadHash,
      issuedAtMs,
      expiresAtMs,
      epoch: this.epoch,
    });
    return this.signClaims({
      version: 1,
      issuer: TEST_ISSUER,
      tokenId,
      callerId,
      target,
      payloadHash,
      approvalEventId: approvalEvent.id,
      approvalEventHash: approvalEvent.eventHash,
      issuedAtMs,
      expiresAtMs,
      epoch: this.epoch,
    });
  }

  async issueDetachedTokenForTest(input: {
    callerId: string;
    target: string;
    event?: unknown;
  }): Promise<string> {
    if (!validId(input.callerId) || !validId(input.target)) {
      throw new Error('LOCAL_TEST_ONLY issuer requires valid caller and target');
    }
    const issuedAtMs = this.now();
    return this.signClaims({
      version: 1,
      issuer: TEST_ISSUER,
      tokenId: randomUUID(),
      callerId: input.callerId,
      target: input.target,
      payloadHash: sha256(canonicalJson(input.event ?? null)),
      approvalEventId: randomUUID(),
      approvalEventHash: 'a'.repeat(64),
      issuedAtMs,
      expiresAtMs: issuedAtMs + APPROVAL_TTL_MS,
      epoch: this.epoch,
    });
  }

  inspectForTest(check: ApprovalCheck): boolean {
    return this.verifyToken(check) !== undefined;
  }

  async consume(check: ApprovalCheck): Promise<ApprovalGrant | undefined> {
    const claims = this.verifyToken(check);
    if (!claims || this.consumed.has(claims.tokenId)) return undefined;
    this.consumed.add(claims.tokenId);
    const grant: ApprovalGrant = {
      executionId: randomUUID(),
      tokenId: claims.tokenId,
      callerId: claims.callerId,
      target: claims.target,
      payloadHash: claims.payloadHash,
      approvalEventId: claims.approvalEventId,
      approvalEventHash: claims.approvalEventHash,
    };
    try {
      await this.appendEvent('execution.authorized', grant as unknown as JsonRecord);
    } catch {
      return undefined;
    }
    this.authorized.set(grant.executionId, grant);
    return grant;
  }

  async recordExecution(grant: ApprovalGrant, outcome: ApprovalOutcome): Promise<void> {
    const authorized = this.authorized.get(grant.executionId);
    if (!authorized || canonicalJson(authorized) !== canonicalJson(grant)) {
      throw new Error('LOCAL_TEST_ONLY execution grant is not authorized');
    }
    if (this.finalized.has(grant.executionId)) throw new Error('execution already finalized');
    this.finalized.add(grant.executionId);
    await this.appendEvent('execution.completed', {
      executionId: grant.executionId,
      tokenId: grant.tokenId,
      callerId: grant.callerId,
      target: grant.target,
      payloadHash: grant.payloadHash,
      approvalEventId: grant.approvalEventId,
      approvalEventHash: grant.approvalEventHash,
      ok: outcome.ok,
      durationMs: outcome.durationMs,
      attempts: outcome.attempts,
      resultHash: outcome.ok ? sha256(canonicalJson(outcome.result ?? null)) : undefined,
      errorHash: outcome.error ? sha256(outcome.error) : undefined,
    });
  }

  private signClaims(claims: ApprovalClaims): string {
    const encoded = Buffer.from(canonicalJson(claims)).toString('base64url');
    const signature = sign(null, Buffer.from(encoded), this.privateKey).toString('base64url');
    return `${encoded}.${signature}`;
  }

  private verifyToken(check: ApprovalCheck): ApprovalClaims | undefined {
    try {
      if (!validId(check.callerId) || !validId(check.target)) return undefined;
      if (typeof check.token !== 'string' || check.token.length > 16_384) return undefined;
      const parts = check.token.split('.');
      if (parts.length !== 2 || !/^[A-Za-z0-9_-]+$/.test(parts[0]) || !/^[A-Za-z0-9_-]+$/.test(parts[1])) {
        return undefined;
      }
      const [encoded, encodedSignature] = parts;
      if (!verify(null, Buffer.from(encoded), this.publicKey, Buffer.from(encodedSignature, 'base64url'))) {
        return undefined;
      }
      const rawClaims: unknown = JSON.parse(Buffer.from(encoded, 'base64url').toString('utf8'));
      if (!isRecord(rawClaims)) return undefined;
      const claims = rawClaims as unknown as ApprovalClaims;
      if (
        claims.version !== 1 ||
        claims.issuer !== TEST_ISSUER ||
        !validId(claims.tokenId) ||
        !validId(claims.callerId) ||
        !validId(claims.target) ||
        !validId(claims.approvalEventId) ||
        typeof claims.approvalEventHash !== 'string' ||
        !/^[a-f0-9]{64}$/.test(claims.approvalEventHash) ||
        typeof claims.payloadHash !== 'string' ||
        !/^[a-f0-9]{64}$/.test(claims.payloadHash) ||
        !Number.isSafeInteger(claims.issuedAtMs) ||
        !Number.isSafeInteger(claims.expiresAtMs) ||
        claims.epoch !== this.epoch
      ) {
        return undefined;
      }
      const now = this.now();
      if (
        claims.issuedAtMs > now ||
        claims.expiresAtMs <= now ||
        claims.expiresAtMs - claims.issuedAtMs !== APPROVAL_TTL_MS ||
        claims.callerId !== check.callerId ||
        claims.target !== check.target ||
        claims.payloadHash !== sha256(canonicalJson(check.event ?? null)) ||
        this.consumed.has(claims.tokenId)
      ) {
        return undefined;
      }
      const approvalEvent = this.eventsById.get(claims.approvalEventId);
      if (!approvalEvent || !this.verifyLedgerChain(approvalEvent)) return undefined;
      const approval = approvalEvent.payload;
      if (
        approvalEvent.type !== 'approval.granted' ||
        approvalEvent.eventHash !== claims.approvalEventHash ||
        approval.issuer !== TEST_ISSUER ||
        approval.tokenId !== claims.tokenId ||
        approval.callerId !== claims.callerId ||
        approval.target !== claims.target ||
        approval.payloadHash !== claims.payloadHash ||
        approval.issuedAtMs !== claims.issuedAtMs ||
        approval.expiresAtMs !== claims.expiresAtMs ||
        approval.epoch !== this.epoch
      ) {
        return undefined;
      }
      return claims;
    } catch {
      return undefined;
    }
  }

  private appendEvent(type: string, payload: JsonRecord): Promise<SignedLedgerEvent> {
    const write = this.appendQueue.then(async () => {
      const core = {
        id: randomUUID(),
        type,
        timestamp: new Date(this.now()).toISOString(),
        epoch: this.epoch,
        payload,
        previousHash: this.previousHash,
      };
      const eventHash = sha256(canonicalJson(core));
      const record: SignedLedgerEvent = {
        ...core,
        eventHash,
        signature: sign(null, Buffer.from(eventHash), this.privateKey).toString('base64url'),
        publicKey: this.publicKeyPem,
      };
      await mkdir(path.dirname(this.ledgerFilePath), { recursive: true });
      await appendFile(this.ledgerFilePath, `${JSON.stringify(record)}\n`, { encoding: 'utf8', mode: 0o600 });
      this.previousHash = eventHash;
      this.eventsById.set(record.id, record);
      this.eventsByHash.set(record.eventHash, record);
      return record;
    });
    this.appendQueue = write.then(() => undefined, () => undefined);
    return write;
  }

  private verifyLedgerEvent(event: SignedLedgerEvent): boolean {
    const core = {
      id: event.id,
      type: event.type,
      timestamp: event.timestamp,
      epoch: event.epoch,
      payload: event.payload,
      previousHash: event.previousHash,
    };
    const computedHash = sha256(canonicalJson(core));
    return event.epoch === this.epoch &&
      event.publicKey === this.publicKeyPem &&
      computedHash === event.eventHash &&
      verify(null, Buffer.from(event.eventHash), this.publicKey, Buffer.from(event.signature, 'base64url'));
  }

  private verifyLedgerChain(event: SignedLedgerEvent): boolean {
    let current: SignedLedgerEvent | undefined = event;
    const seen = new Set<string>();
    while (current) {
      if (!this.verifyLedgerEvent(current) || seen.has(current.eventHash)) return false;
      seen.add(current.eventHash);
      if (current.previousHash === ZERO_HASH) return true;
      current = this.eventsByHash.get(current.previousHash);
    }
    return false;
  }
}
