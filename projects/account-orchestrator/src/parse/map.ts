/**
 * Network map -> ParsedMap.
 *
 * The map is prose written for a human, so the parser is tolerant on shape and
 * strict on meaning:
 *
 *   * Shape is forgiving: heading sections, GFM tables, bullet lists, and fenced
 *     JSON blocks all work. Unrecognised material is collected into
 *     `unparsed` with a line number — never dropped, never guessed at.
 *   * Meaning is strict: a purpose the map does not state becomes `unknown`;
 *     a status the map does not state becomes `unknown`. Nothing is inferred
 *     from a name or a URL.
 *
 * See docs/MAP-FORMAT.md for the accepted shapes.
 */
import { IdAllocator, accountId as makeAccountId, slug } from '../ids.js';
import type {
  AccountInput,
  Health,
  LinkInput,
  MachineInput,
  ParsedMap,
  PortInput,
  ServiceInput,
  ServiceKind,
  Status,
  UnparsedRegion,
} from '../types.js';
import { emptyParsedMap } from '../types.js';
import { cleanInline, scanMarkdown, type Block, type BulletBlock, type TableRow } from './markdown.js';
import { blockingFindings, parseSecretRef } from '../secrets.js';
import { parsePastedNetworkMap } from './pasted-outline.js';

/* ------------------------------------------------------------------ */
/* Vocabulary                                                          */
/* ------------------------------------------------------------------ */

const LIVE_WORDS = new Set([
  'live', 'up', 'active', 'running', 'online', 'ok', 'healthy', 'current', 'production', 'prod', 'in-use', 'in use',
]);
const DEAD_WORDS = new Set([
  'dead', 'down', 'offline', 'retired', 'archived', 'deprecated', 'decommissioned', 'unused', 'disabled',
  'cancelled', 'canceled', 'expired', 'deleted', 'closed', 'stopped', 'gone', 'abandoned', 'paused',
]);
const UNKNOWN_WORDS = new Set(['unknown', 'unsure', 'unclear', 'unsorted', 'unverified', 'maybe', 'tbd', '?', '-', '—']);

export function parseStatus(raw: string | undefined): { status: Status; note?: string } {
  const value = cleanInline(raw ?? '').toLowerCase().replace(/[.!]$/, '').trim();
  if (value === '') return { status: 'unknown' };
  if (LIVE_WORDS.has(value)) return { status: 'live' };
  if (DEAD_WORDS.has(value)) return { status: 'dead', note: value === 'dead' ? undefined : value };
  if (UNKNOWN_WORDS.has(value)) return { status: 'unknown' };
  return { status: 'unknown', note: value };
}

const HEALTH_UP = new Set(['up', 'live', 'healthy', 'ok', 'open', 'listening', 'pass', 'passing']);
const HEALTH_DOWN = new Set(['down', 'closed', 'refused', 'unreachable', 'fail', 'failing', 'timeout']);

export function parseHealth(raw: string | undefined): Health {
  const value = cleanInline(raw ?? '').toLowerCase().trim();
  if (HEALTH_UP.has(value)) return 'up';
  if (HEALTH_DOWN.has(value)) return 'down';
  return 'unknown';
}

const KIND_WORDS: Record<string, ServiceKind> = {
  repo: 'repo', repository: 'repo', git: 'repo', project: 'repo', github: 'repo', code: 'repo',
  site: 'site', website: 'site', web: 'site', page: 'site', domain: 'site',
  database: 'database', db: 'database', postgres: 'database', sqlite: 'database', postgresql: 'database',
  mysql: 'database', redis: 'database', 'vector-db': 'database',
  vm: 'vm', server: 'vm', instance: 'vm', box: 'vm', vps: 'vm',
  app: 'app', service: 'app', application: 'app', daemon: 'app', stack: 'app',
  api: 'api', endpoint: 'api', gateway: 'api', mcp: 'api',
  other: 'other',
};

export function parseKind(raw: string | undefined, fallback: ServiceKind = 'other'): ServiceKind {
  const value = cleanInline(raw ?? '').toLowerCase().trim();
  if (!value) return fallback;
  return KIND_WORDS[value] ?? fallback;
}

/* ------------------------------------------------------------------ */
/* Column synonyms                                                     */
/* ------------------------------------------------------------------ */

const PROVIDER_COLS = ['provider', 'platform', 'vendor', 'company', 'service', 'site', 'where'];
const LOGIN_COLS = ['login', 'email', 'e-mail', 'handle', 'username', 'user', 'account', 'identity', 'account login'];
const PURPOSE_COLS = ['purpose', 'use', 'used for', 'usage', 'what for', 'function', 'role', 'description', 'notes', 'note'];
const STATUS_COLS = ['status', 'state', 'health'];
const LINK_COLS = ['linked', 'linked to', 'links', 'linked-to', 'related', 'relates to', 'connected to', 'ties', 'associated'];
const SECRET_COLS = ['secret', 'secret ref', 'secret reference', 'credential ref', 'secrets ref', 'where secret lives', 'secret location', 'key location'];
const MACHINE_COLS = ['machine', 'host', 'device', 'box', 'node', 'server', 'hardware', 'runs on', 'location'];
const URL_COLS = ['url', 'link', 'address', 'endpoint', 'uri', 'site url', 'base url'];
const PORT_COLS = ['port', 'ports', 'listens on', 'listen'];
const PROTOCOL_COLS = ['protocol', 'proto'];
const NAME_COLS = ['name', 'repo', 'repository', 'project', 'component', 'title', 'service', 'app'];
const KIND_COLS = ['kind', 'type', 'category', 'class'];
const IP_COLS = ['ip', 'tailnet ip', 'tailscale ip', 'tailnet', 'address'];
const OS_COLS = ['os', 'operating system', 'system', 'arch'];
const ROLE_COLS = ['role', 'purpose', 'function', 'notes'];

const SECTION_KEYWORDS: Record<string, string[]> = {
  machine: ['machine', 'machines', 'host', 'hosts', 'device', 'devices', 'boxes', 'nodes', 'hardware', 'topology', 'rigs', 'fleet'],
  account: ['account', 'accounts', 'credential', 'credentials', 'login', 'logins', 'identity', 'identities', 'subscription', 'subscriptions', 'ownership', 'who-owns-what'],
  service: ['service', 'services', 'endpoint', 'endpoints', 'stack', 'runtime', 'application', 'applications', 'apps', 'deployment', 'deployments', 'infrastructure', 'site', 'sites', 'domain', 'domains'],
  repo: ['repo', 'repos', 'repository', 'repositories', 'code', 'projects', 'git'],
  port: ['port', 'ports', 'listener', 'listeners', 'exposure', 'exposed'],
  link: ['link', 'links', 'relationship', 'relationships', 'dependency', 'dependencies', 'connections', 'graph'],
};

const TAILNET_IP = /\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b/;
const HOST_PORT = /\b((?:(?:\d{1,3}\.){3}\d{1,3})|(?:localhost|127\.0\.0\.1)|(?:[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+(?:\.ts\.net)?)):(\d{2,5})\b/gi;
const URL_RE = /\bhttps?:\/\/[^\s<>)]+/gi;

/** `Machine: Acer (100.64.1.5)` -> `Acer`. */
function machineLabel(text: string): string {
  const clean = cleanInline(text.replace(TAILNET_IP, ' ').replace(/\(\s*\)/g, ' '))
    .replace(/^[^\p{L}\p{N}]+/u, '')
    .replace(/^(?:machine|host|device|box|node|server)\s*[:\-]\s*/i, '')
    .replace(/\s{2,}/g, ' ')
    .trim();
  const phone = /^Phone\s+[—–]\s*(S22\s+Ultra)$/i.exec(clean);
  if (phone) return `Phone ${phone[1]}`;
  const title = clean.split(/\s+[—–]\s+/u, 1)[0]?.trim() ?? clean;
  const acer = /^Acer\s*\(\s*(Aspire\s+\d+)\b/i.exec(title);
  if (acer) return `Acer ${acer[1]}`;
  return title.replace(/[\s\-:|,]+$/g, '').trim();
}

function isMachineRecordRow(label: string): boolean {
  return !/\b(?:supabase|vercel|netlify|google play)\b/i.test(label);
}

function machineAliases(name: string): string[] {
  const aliases = new Set([name, splitLabel(name).head]);
  const oracle = /^(Oracle VM)\b/i.exec(name);
  const acer = /^(Acer)\b/i.exec(name);
  const phone = /^(Phone\s+S22\s+Ultra)$/i.exec(name);
  if (oracle) aliases.add(oracle[1]!);
  if (acer) aliases.add(acer[1]!);
  if (phone) aliases.add('S22 Ultra');
  return [...aliases].filter(Boolean);
}

function validPort(value: string | number): number | undefined {
  const match = /^\s*(\d{1,5})\s*(?:\/\s*(?:tcp|udp))?\s*$/i.exec(String(value));
  if (!match) return undefined;
  const port = Number(match[1]);
  if (port < 1 || port > 65535 || (port >= 1900 && port <= 2099)) return undefined;
  return port;
}

function portNumbers(text: string): number[] {
  const ports = new Set<number>();
  const add = (raw: string): void => {
    const port = validPort(raw);
    if (port !== undefined) ports.add(port);
  };
  HOST_PORT.lastIndex = 0;
  let hostPort: RegExpExecArray | null;
  while ((hostPort = HOST_PORT.exec(text)) !== null) add(hostPort[2]!);
  const explicit = /\bports?\b\s*(?:[:=]\s*)?(\d{2,5}(?:\s*(?:,|\+|\/|&|and)\s*\d{2,5})*)/gi;
  let list: RegExpExecArray | null;
  while ((list = explicit.exec(text)) !== null) {
    for (const value of list[1]!.match(/\d{2,5}/g) ?? []) add(value);
  }
  NAME_PORT.lastIndex = 0;
  let named: RegExpExecArray | null;
  while ((named = NAME_PORT.exec(text)) !== null) add(named[2]!);
  return [...ports];
}

/**
 * `Gmail — personal` -> head "Gmail", qualifier "personal".
 * `Hub (8471)` -> head "Hub", qualifier "8471".
 * Only spaced dashes, pipes and colons split: `hugging-face` stays one word.
 */
export function splitLabel(label: string): { head: string; qualifier?: string } {
  const clean = label.trim();
  const dashed = /^(.+?)\s+(?:—|–|\||:)\s*(.+)$/.exec(clean) ?? /^(.+?)\s+-\s+(.+)$/.exec(clean);
  if (dashed) return { head: dashed[1]!.trim(), qualifier: dashed[2]!.trim() };
  const parenthesised = /^(.+?)\s*\(([^)]+)\)\s*$/.exec(clean);
  if (parenthesised) return { head: parenthesised[1]!.trim(), qualifier: parenthesised[2]!.trim() };
  return { head: clean };
}

export function emailOf(...texts: (string | undefined)[]): string | undefined {
  for (const text of texts) {
    const found = text ? EMAIL.exec(text) : null;
    if (found) return found[0];
  }
  return undefined;
}

export function addressOf(...texts: (string | undefined)[]): string | undefined {
  for (const text of texts) {
    const found = text ? IPV4.exec(text) : null;
    if (found) return found[0];
  }
  return undefined;
}

/** `**live** — this thing is up` under a status-key heading is explanation, not data. */
export function isStatusKeyBullet(text: string): boolean {
  const prefix = text
    .replace(/^\s*[-*+]\s*/, '')
    .replace(/[*_`]/g, '')
    .split(/\s+[—–]\s+|\s+\|\s+|:\s*/u, 1)[0]
    ?.trim()
    .toLowerCase()
    .replace(/[.!]$/, '') ?? '';
  const statusKeys = new Set([
    ...LIVE_WORDS,
    ...DEAD_WORDS,
    ...UNKNOWN_WORDS,
    'built', 'exists', 'in storage', 'submitted', 'in testing', 'parked', 'planned', 'unchecked',
  ]);
  return statusKeys.has(prefix);
}

function findColumn(header: string[], synonyms: string[]): number {
  const normalised = header.map((h) => cleanInline(h).toLowerCase().replace(/[.]$/, '').trim());
  for (const synonym of synonyms) {
    const exact = normalised.indexOf(synonym);
    if (exact >= 0) return exact;
  }
  for (const synonym of synonyms) {
    const loose = normalised.findIndex((h) => h.includes(synonym));
    if (loose >= 0) return loose;
  }
  return -1;
}

function cell(row: string[], index: number): string | undefined {
  if (index < 0) return undefined;
  const value = row[index];
  if (value === undefined) return undefined;
  const trimmed = cleanInline(value);
  return trimmed === '' || trimmed === '-' || trimmed === '—' ? undefined : trimmed;
}

function sectionKind(path: string[]): string | undefined {
  const joined = path.join(' / ').toLowerCase();
  for (const [kind, words] of Object.entries(SECTION_KEYWORDS)) {
    if (words.some((word) => new RegExp(`(^|[^a-z])${word.replace(/[-/\\^$*+?.()|[\]{}]/g, '\\$&')}([^a-z]|$)`).test(joined))) {
      return kind;
    }
  }
  return undefined;
}

/* ------------------------------------------------------------------ */
/* Builder with merge semantics                                        */
/* ------------------------------------------------------------------ */

class MapBuilder {
  readonly machines = new Map<string, MachineInput>();
  readonly accounts = new Map<string, AccountInput>();
  readonly services = new Map<string, ServiceInput>();
  readonly ports = new Map<string, PortInput>();
  readonly links = new Map<string, LinkInput>();
  readonly unparsed: UnparsedRegion[] = [];
  readonly references: UnparsedRegion[] = [];
  readonly warnings: string[] = [];

  private readonly machineByIp = new Map<string, string>();
  private readonly machineByName = new Map<string, string>();
  private readonly accountByKey = new Map<string, string>();
  private readonly serviceByKey = new Map<string, string>();
  private readonly ids = new IdAllocator();

  constructor(private readonly sourcePath: string) {}

  ref(line: number): string {
    return `${this.sourcePath.split('/').pop() ?? this.sourcePath}:${line}`;
  }

  drop(line: number, text: string, reason: string): void {
    this.unparsed.push({ sourceRef: this.ref(line), text: text.slice(0, 240), reason });
  }

  warn(message: string): void {
    this.warnings.push(message);
  }

  /** Map text kept for the human: status keys, legends, notes. Never data. */
  reference(line: number, text: string): void {
    this.references.push({ sourceRef: this.ref(line), text: text.slice(0, 240), reason: 'reference' });
  }

  addMachine(input: { name: string; tailnetIp?: string; os?: string; role?: string; status?: Status }, line: number): string | undefined {
    const name = cleanInline(input.name);
    if (!name) {
      this.drop(line, input.name ?? '', 'machine row without a name');
      return undefined;
    }
    const existingId = this.machineByName.get(name.toLowerCase());
    if (existingId) {
      const existing = this.machines.get(existingId)!;
      existing.tailnetIp = existing.tailnetIp ?? input.tailnetIp;
      existing.os = existing.os ?? input.os;
      existing.role = existing.role ?? input.role;
      if (existing.status === 'unknown' && input.status) existing.status = input.status;
      if (input.tailnetIp) this.machineByIp.set(input.tailnetIp, existingId);
      return existingId;
    }
    const id = this.ids.next(slug(name));
    const machine: MachineInput = {
      id,
      name,
      tailnetIp: input.tailnetIp,
      os: input.os,
      role: input.role,
      status: input.status ?? 'unknown',
      sourceRef: this.ref(line),
    };
    this.machines.set(id, machine);
    for (const alias of machineAliases(name)) {
      if (!this.machineByName.has(alias.toLowerCase())) this.machineByName.set(alias.toLowerCase(), id);
    }
    if (input.tailnetIp) this.machineByIp.set(input.tailnetIp, id);
    return id;
  }

  resolveMachine(nameOrIp: string | undefined): string | undefined {
    if (!nameOrIp) return undefined;
    const value = cleanInline(nameOrIp);
    return this.machineByName.get(value.toLowerCase()) ?? this.machineByIp.get(value) ?? this.machineByName.get(slug(value).replace(/-/g, ' '));
  }

  /** Record that a machine answers at this address, so a later `host:port` resolves. */
  bindAddress(address: string, machineName: string, line: number): void {
    if (!address || !machineName) return;
    const known = this.resolveMachine(machineName);
    const id = known ?? this.addMachine({ name: machineName, tailnetIp: address, status: 'unknown' }, line);
    if (!id) return;
    const machine = this.machines.get(id);
    if (machine && !machine.tailnetIp) machine.tailnetIp = address;
    if (!this.machineByIp.has(address)) this.machineByIp.set(address, id);
  }

  /** A host seen only as an IP becomes a machine record marked unidentified. */
  machineForIp(ip: string, line: number): string {
    const known = this.machineByIp.get(ip);
    if (known) return known;
    return this.addMachine({ name: ip, tailnetIp: ip, role: 'unidentified — seen only as an address in the map', status: 'unknown' }, line)!;
  }

  addAccount(
    input: { provider?: string; login?: string; purpose?: string; statusRaw?: string; secretRef?: string; linked?: string[] },
    line: number,
  ): string | undefined {
    const provider = cleanInline(input.provider ?? '');
    const login = cleanInline(input.login ?? '');
    if (!provider && login.includes('@')) {
      return this.addAccount({ ...input, provider: login.split('@')[1] ?? '' }, line);
    }
    if (!provider) {
      this.drop(line, `${provider} ${login}`, 'account without a provider');
      return undefined;
    }
    if (!login) {
      this.drop(line, provider, 'account without a login');
      return undefined;
    }

    const key = `${provider.toLowerCase()}|${login.toLowerCase()}`;
    const { status, note } = parseStatus(input.statusRaw);
    const purpose = cleanInline(input.purpose ?? '') || 'unknown';

    let secretRef: string | undefined;
    if (input.secretRef) {
      const parsed = parseSecretRef(input.secretRef);
      if (!parsed.ok) {
        this.drop(line, `${provider} ${login}`, `secret reference rejected: ${parsed.error}`);
        return undefined;
      }
      secretRef = `${parsed.ref.scheme}:${parsed.ref.path}`;
    }

    const existingId = this.accountByKey.get(key);
    if (existingId) {
      const existing = this.accounts.get(existingId)!;
      if (existing.purpose === 'unknown' && purpose !== 'unknown') existing.purpose = purpose;
      if (existing.status === 'unknown' && status !== 'unknown') existing.status = status;
      existing.secretRef = existing.secretRef ?? secretRef;
      this.warn(`${this.ref(line)}: duplicate entry for ${provider} ${login} (first seen at ${existing.sourceRef}) — merged`);
      return existingId;
    }

    const id = this.ids.next(makeAccountId(provider, login));
    this.accountByKey.set(key, id);
    this.accounts.set(id, {
      id,
      provider,
      login,
      purpose,
      status,
      secretRef,
      sourceRef: this.ref(line),
      notes: note ? `map said: ${note}` : undefined,
    });
    for (const target of input.linked ?? []) this.queueLink(id, target);
    return id;
  }

  /**
   * Long-form rows name the thing in prose: `Gmail — personal`. The address, if
   * the map gives one, is the login; otherwise the label itself is recorded and
   * flagged, because inventing an address would be worse.
   */
  addAccountFromLabel(
    input: { label?: string; description?: string; statusRaw?: string },
    line: number,
  ): string | undefined {
    const label = cleanInline(input.label ?? '');
    const description = cleanInline(input.description ?? '');
    if (!label) {
      this.drop(line, description, 'account row without a name');
      return undefined;
    }
    const { head, qualifier } = splitLabel(label);
    const email = emailOf(label, description);
    const handle = HANDLE.exec(description)?.[1] ?? HANDLE.exec(label)?.[1];
    const provider = head || email?.split('@')[1] || '';
    if (!provider) {
      this.drop(line, label, 'account row without a provider');
      return undefined;
    }
    // `| GitHub | fixture-owner, personal repositories | live |` — the handle is
    // the first bare token when no address is given.
    const firstToken = description.split(/[,;]/)[0]?.trim() ?? '';
    const bareHandle = /^[A-Za-z0-9][A-Za-z0-9._-]{1,39}$/.test(firstToken) && !/\s/.test(firstToken) ? firstToken : undefined;
    const login = email ?? (handle ? `@${handle}` : qualifier ?? bareHandle ?? head);
    if (!email && !handle && !bareHandle && !qualifier) {
      this.warn(`${this.ref(line)}: "${label}" has no address in the map — "${login}" is the label, not a login`);
    }
    const withoutAddress = (email ? description.split(email).join(' ') : bareHandle && !qualifier ? description.replace(bareHandle, ' ') : description)
      .replace(/^[\s—–\-:|,]+/, '')
      .trim();
    const split = purposeAndStatus(withoutAddress || undefined);
    return this.addAccount(
      { provider, login, purpose: split.purpose, statusRaw: input.statusRaw ?? split.status },
      line,
    );
  }

  /** `| Machine | What it is | Status |` — the address, when present, goes on the record. */
  addMachineFromLabel(input: { label?: string; description?: string; statusRaw?: string }, line: number): string | undefined {
    const label = cleanInline(input.label ?? '');
    const description = cleanInline(input.description ?? '');
    const name = machineLabel(label) || machineLabel(description);
    if (!name) {
      this.drop(line, label, 'machine row without a name');
      return undefined;
    }
    const address = TAILNET_IP.exec(`${label} ${description}`)?.[0] ?? addressOf(label, description);
    const role = (address ? description.split(address).join(' ') : description)
      .replace(/^[\s—–\-:|,]+/, '')
      .replace(/[\s—–\-:|,]+$/, '')
      .trim();
    return this.addMachine(
      { name, tailnetIp: address, role: role || undefined, status: parseStatus(input.statusRaw).status },
      line,
    );
  }

  private readonly pendingLinks: { from: string; target: string; sourceRef: string }[] = [];

  queueLink(from: string, targetRaw: string): void {
    for (const part of targetRaw.split(/[,;/]| and /i)) {
      const target = cleanInline(part);
      if (!target) continue;
      this.pendingLinks.push({ from, target, sourceRef: this.accounts.get(from)?.sourceRef ?? from });
    }
  }

  resolveLinks(): void {
    const candidates = [...this.accounts.values()];
    for (const pending of this.pendingLinks) {
      const needle = pending.target.toLowerCase();
      const matches = candidates.filter(
        (a) =>
          a.id !== pending.from &&
          (a.login.toLowerCase() === needle ||
            a.provider.toLowerCase() === needle ||
            a.id.toLowerCase() === needle ||
            `${a.provider.toLowerCase()}:${a.login.toLowerCase()}` === needle ||
            a.login.toLowerCase().startsWith(needle + '@')),
      );
      if (matches.length === 0) {
        this.unparsed.push({
          sourceRef: pending.sourceRef,
          text: pending.target,
          reason: `link target "${pending.target}" does not match any account in the map`,
        });
        continue;
      }
      if (matches.length > 1) {
        this.warn(`${pending.sourceRef}: link target "${pending.target}" is ambiguous (${matches.map((m) => m.id).join(', ')}) — using ${matches[0]!.id}`);
      }
      const target = matches[0]!;
      const key = `${pending.from}->${target.id}`;
      if (!this.links.has(key)) {
        this.links.set(key, { fromAccount: pending.from, toAccount: target.id, relation: 'linked', sourceRef: pending.sourceRef });
      }
    }
  }

  addService(
    input: {
      name?: string;
      kind?: string;
      machine?: string;
      url?: string;
      /** Markdown link targets from the same row or bullet. */
      links?: string[];
      account?: string;
      statusRaw?: string;
      /** A port stated next to the name: `Hub (8471)`. */
      portHint?: string | number;
      /** An address stated in the row's description; places the service on that machine. */
      address?: string;
    },
    line: number,
  ): string | undefined {
    let name = cleanInline(input.name ?? '');
    let portHint = input.portHint;
    // The name itself may carry the port: `Hub (8471)`.
    const named = splitLabel(name);
    if (named.qualifier && /^\d{2,5}(?:\s*\/\s*(?:tcp|udp))?$/i.test(named.qualifier)) {
      portHint = portHint ?? named.qualifier;
      name = named.head;
    }
    const url = (input.url ? cleanInline(input.url) : undefined) ?? input.links?.find((link) => /^https?:\/\//i.test(link));
    if (!name && url) name = serviceNameFromUrl(url);
    if (!name) {
      this.drop(line, JSON.stringify(input), 'service row without a name or URL');
      return undefined;
    }
    const kind = input.kind ? parseKind(input.kind, 'other') : kindFromUrl(url);
    let machineId = this.resolveMachine(input.machine);
    // A service whose URL — or description — names an address is placed on that machine.
    if (!machineId && url) {
      const host = safeHost(url);
      if (host && IPV4.test(host) && host !== '127.0.0.1') machineId = this.machineForIp(host, line);
    }
    if (!machineId && input.address && IPV4.test(input.address)) {
      machineId = this.machineForIp(input.address, line);
    }
    const key = `${machineId ?? '-'}|${slug(name)}`;
    const existingId = this.serviceByKey.get(key);
    if (existingId) {
      const existing = this.services.get(existingId)!;
      if (existing.url === undefined && url) existing.url = url;
      if (existing.kind === 'other' && kind !== 'other') existing.kind = kind;
      if (existing.accountId === undefined && input.account) existing.accountId = this.resolveAccountRef(input.account);
      if (existing.status === 'unknown') existing.status = parseStatus(input.statusRaw).status;
      return existingId;
    }

    const preferred = machineId ? `svc:${slug(name)}@${machineId}` : `svc:${slug(name)}`;
    const id = this.ids.next(preferred);
    const accountId = input.account ? this.resolveAccountRef(input.account) : undefined;
    this.services.set(id, {
      id,
      name,
      kind,
      accountId,
      url,
      status: parseStatus(input.statusRaw).status,
      machineId,
      sourceRef: this.ref(line),
    });
    this.serviceByKey.set(key, id);
    if (url) this.portFromUrl(url, machineId, id, line);
    if (portHint !== undefined && machineId) {
      this.addPort({ machine: this.machines.get(machineId)?.name, port: portHint, serviceId: id }, line);
    }
    return id;
  }

  resolveAccountRef(value: string): string | undefined {
    const needle = cleanInline(value).toLowerCase();
    for (const account of this.accounts.values()) {
      if (account.id.toLowerCase() === needle) return account.id;
      if (account.login.toLowerCase() === needle) return account.id;
      if (`${account.provider.toLowerCase()}:${account.login.toLowerCase()}` === needle) return account.id;
      if (needle.includes('@') && account.login.toLowerCase() === needle) return account.id;
    }
    return undefined;
  }

  addPort(input: { machine?: string; port?: string | number; protocol?: string; service?: string; healthRaw?: string; serviceId?: string }, line: number): string | undefined {
    if (!input.machine) {
      this.drop(line, `port ${String(input.port ?? '')}`, 'port row has no machine, and the section does not name one');
      return undefined;
    }
    const machineId = this.resolveMachine(input.machine);
    if (!machineId) {
      this.drop(line, String(input.port ?? ''), `port row names unknown machine "${input.machine}"`);
      return undefined;
    }
    const portNumber = Number(String(input.port ?? '').replace(/[^\d]/g, ''));
    if (!Number.isInteger(portNumber) || portNumber < 1 || portNumber > 65535) {
      this.drop(line, String(input.port ?? ''), 'port is not a number between 1 and 65535');
      return undefined;
    }
    const protocol = cleanInline(input.protocol ?? '').toLowerCase().startsWith('udp') ? 'udp' : 'tcp';
    const id = `${machineId}:${portNumber}/${protocol}`;
    const serviceId = input.serviceId ?? (input.service ? this.resolveService(input.service, machineId) : undefined);
    const health = parseHealth(input.healthRaw);

    const existing = this.ports.get(id);
    if (existing) {
      existing.serviceId = existing.serviceId ?? serviceId;
      if (existing.health === 'unknown') existing.health = health;
      return id;
    }
    this.ports.set(id, { id, serviceId, machineId, port: portNumber, protocol, health, sourceRef: this.ref(line) });
    return id;
  }

  /** The first known machine named in a sentence: `sits on the T340`. */
  machineMentioned(text: string): string | undefined {
    const lowerText = text.toLowerCase();
    const aliasMatches = [...this.machines.values()]
      .flatMap((machine) => machineAliases(machine.name).map((alias) => ({ machine, alias })))
      .sort((a, b) => b.alias.length - a.alias.length);
    for (const { machine, alias } of aliasMatches) {
      if (alias.toLowerCase() !== machine.name.toLowerCase() && lowerText.includes(alias.toLowerCase())) return machine.name;
    }
    const names = [...this.machines.values()].sort((a, b) => b.name.length - a.name.length);
    for (const machine of names) {
      const pattern = new RegExp(`(^|[^\\p{L}\\p{N}])${machine.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}([^\\p{L}\\p{N}]|$)`, 'iu');
      if (pattern.test(text)) return machine.name;
    }
    return undefined;
  }

  /** The machine a named service already sits on, if any. */
  machineOfService(name: string): string | undefined {
    const key = slug(name);
    for (const service of this.services.values()) {
      if (slug(service.name) === key && service.machineId) return service.machineId;
    }
    return undefined;
  }

  resolveService(name: string, machineId?: string): string | undefined {
    const key = `${machineId ?? '-'}|${slug(name)}`;
    return this.serviceByKey.get(key) ?? this.serviceByKey.get(`-|${slug(name)}`);
  }

  private portFromUrl(url: string, machineId: string | undefined, serviceId: string, line: number): void {
    const match = /^https?:\/\/([^/:]+):(\d{2,5})/.exec(url);
    if (!match) return;
    const host = match[1]!;
    const port = Number(match[2]);
    let resolved = machineId;
    if (!resolved && TAILNET_IP.test(host)) resolved = this.resolveMachine(host);
    if (!resolved && (host === 'localhost' || host === '127.0.0.1')) return;
    if (!resolved) return;
    this.addPort({ machine: this.machines.get(resolved)?.name, port, serviceId, healthRaw: 'unknown' }, line);
  }

  finish(): ParsedMap {
    this.resolveLinks();
    // The map contradicting itself is worth knowing about, not worth silently
    // resolving: the same service name placed on two machines is a real finding.
    const byName = new Map<string, MachineInput[]>();
    for (const service of this.services.values()) {
      if (!service.machineId) continue;
      const list = byName.get(service.name.toLowerCase()) ?? [];
      const machine = this.machines.get(service.machineId);
      if (machine) list.push(machine);
      byName.set(service.name.toLowerCase(), list);
    }
    for (const [name, machines] of byName) {
      if (machines.length > 1) {
        this.warn(`service "${name}" is placed on ${machines.map((m) => m.name).join(' and ')} — the map names one service twice`);
      }
    }
    const parsed = emptyParsedMap(this.sourcePath);
    parsed.machines = [...this.machines.values()];
    parsed.accounts = [...this.accounts.values()];
    parsed.services = [...this.services.values()];
    parsed.ports = [...this.ports.values()];
    parsed.links = [...this.links.values()];
    parsed.unparsed = this.unparsed;
    parsed.references = this.references;
    parsed.warnings = this.warnings;
    return parsed;
  }
}

/** Every URL a table row carries, from Markdown links or bare text. */
function rowUrls(row: TableRow, description?: string): string[] {
  const urls = [...row.links.flat()];
  if (description) urls.push(...(description.match(URL_RE) ?? []));
  return [...new Set(urls)];
}

/** Which entity a `Name | What it is | Status` table describes. */
function wideEntityFor(header: string[], section: string | undefined): 'account' | 'machine' | 'service' | 'repo' {
  const first = (header[0] ?? '').toLowerCase();
  if (section === 'machine' || /machine|host|device|box|node/.test(first)) return 'machine';
  if (section === 'repo' || /repo|repository|project/.test(first)) return 'repo';
  if (section === 'service' || /site|service|app|endpoint|tool|dashboard/.test(first)) return 'service';
  if (section === 'account' || /account|thing|identity|login|credential/.test(first)) return 'account';
  return 'service';
}

/**
 * A URL is often the only evidence of what a thing is. A GitHub/GitLab URL is a
 * repository; anything else is a site. Nothing is claimed beyond that.
 */
export function kindFromUrl(url: string | undefined): ServiceKind {
  if (!url) return 'other';
  const host = safeHost(url) ?? '';
  if (/^(?:www\.)?(?:github\.com|gitlab\.com|bitbucket\.org|codeberg\.org)$/i.test(host)) return 'repo';
  if (/^(?:www\.)?(?:npmjs\.com|pypi\.org)$/i.test(host)) return 'repo';
  return 'site';
}

function safeHost(url: string): string | undefined {
  try {
    return new URL(url).hostname;
  } catch {
    return undefined;
  }
}

function serviceNameFromUrl(url: string): string {
  try {
    const parsed = new URL(url);
    const path = parsed.pathname.replace(/\/$/, '');
    return path ? `${parsed.host}${path}` : parsed.host;
  } catch {
    return url;
  }
}

/* ------------------------------------------------------------------ */
/* Structured (JSON) blocks                                            */
/* ------------------------------------------------------------------ */

interface StructuredPayload {
  machines?: Array<Record<string, unknown>>;
  accounts?: Array<Record<string, unknown>>;
  services?: Array<Record<string, unknown>>;
  ports?: Array<Record<string, unknown>>;
  links?: Array<Record<string, unknown>>;
}

function asString(value: unknown): string | undefined {
  if (typeof value === 'string') return value;
  if (typeof value === 'number') return String(value);
  return undefined;
}

function asStringArray(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((v) => asString(v) ?? '').filter(Boolean);
  const single = asString(value);
  return single ? [single] : [];
}

/* ------------------------------------------------------------------ */
/* Main parse                                                          */
/* ------------------------------------------------------------------ */

export function parseNetworkMap(text: string, sourcePath: string): ParsedMap {
  const pasted = parsePastedNetworkMap(text, sourcePath);
  if (pasted) return pasted;
  const { blocks } = scanMarkdown(text);
  const builder = new MapBuilder(sourcePath);
  const headings = blocks.filter((b): b is Extract<Block, { kind: 'heading' }> => b.kind === 'heading');

  const headingStack = (line: number): { level: number; text: string; line: number }[] => {
    const stack: { level: number; text: string; line: number }[] = [];
    for (const heading of headings) {
      if (heading.line > line) break;
      while (stack.length > 0 && stack[stack.length - 1]!.level >= heading.level) stack.pop();
      stack.push({ level: heading.level, text: heading.text, line: heading.line });
    }
    return stack;
  };
  const pathAt = (line: number): string[] => headingStack(line).map((entry) => entry.text).filter(Boolean);

  // ---- Pass 1: learn the real machine rows before earlier service mentions need them.
  const machineNameHints = new Set<string>();
  const addMachineHint = (name: string): void => {
    const label = machineLabel(name);
    if (!label) return;
    for (const alias of machineAliases(label)) machineNameHints.add(alias.toLowerCase());
  };
  for (const block of blocks) {
    if (block.kind === 'table') {
      const kind = sectionKind(pathAt(block.line));
      const descriptionCol = findColumn(block.header, DESCRIPTION_COLS);
      const wideNameCol = findColumn(block.header, WIDE_NAME_COLS);
      const statusCol = findColumn(block.header, STATUS_COLS);
      const isAccountTable = findColumn(block.header, PROVIDER_COLS) >= 0 && findColumn(block.header, LOGIN_COLS) >= 0;
      const isMixedMachineTable = kind === 'machine' && !isAccountTable && descriptionCol >= 0 && wideNameCol >= 0;
      if (isMixedMachineTable) {
        for (const row of block.rows) {
          const label = cell(row.cells, wideNameCol) ?? '';
          const description = cell(row.cells, descriptionCol);
          if (!isMachineRecordRow(label)) continue;
          const machineId = builder.addMachineFromLabel({ label, description, statusRaw: cell(row.cells, statusCol) }, row.line);
          const machine = machineId ? builder.machines.get(machineId) : undefined;
          if (machine) addMachineHint(machine.name);
        }
      } else {
        const hasName = findColumn(block.header, MACHINE_COLS) >= 0 || findColumn(block.header, ['name']) === 0;
        const hasAddress = findColumn(block.header, IP_COLS) >= 0;
        const hasOs = findColumn(block.header, OS_COLS) >= 0;
        if (hasName && (hasAddress || hasOs) && !isAccountTable) {
          const nameIndex = findColumn(block.header, ['machine', 'host', 'device', 'node', 'name']);
          for (const row of block.rows) {
            const name = cell(row.cells, nameIndex);
            if (name) addMachineHint(name);
          }
        }
      }
    }
    if (block.kind === 'heading' && IPV4.test(block.text)) addMachineHint(block.text);
  }

  /** The machine this line sits inside: `## Acer`, `### Machine: Acer`, `## Acer — 100.64.1.10`. */
  const machineSectionFor = (line: number): { name: string; line: number } | undefined => {
    const stack = headingStack(line);
    for (let index = stack.length - 1; index >= 0; index -= 1) {
      const entry = stack[index]!;
      const bare = machineLabel(entry.text);
      if (!bare) continue;
      if (machineNameHints.has(bare.toLowerCase())) return { name: builder.machineMentioned(entry.text) ?? bare, line: entry.line };
      if (/^(?:machine|host|device|box|node|server)\s*[:\-]/i.test(entry.text)) return { name: bare, line: entry.line };
    }
    return undefined;
  };

  // ---- Pass 2: extract.
  for (const block of blocks) {
    const kind = sectionKind(pathAt(block.line));
    const currentMachine = machineSectionFor(block.line)?.name;

    if (block.kind === 'heading') {
      const address = block.text.match(IPV4)?.[0];
      if (address) {
        const label = machineLabel(block.text);
        if (label) builder.addMachine({ name: label, tailnetIp: address, status: 'unknown' }, block.line);
      }
      continue;
    }

    if (block.kind === 'code') {
      if (block.lang === 'json' || block.lang === 'ao' || block.lang === 'ao-json') {
        try {
          const payload = JSON.parse(block.body) as StructuredPayload;
          ingestStructured(builder, payload, block.line);
        } catch (error) {
          builder.drop(block.line, block.body, `JSON block could not be parsed: ${(error as Error).message}`);
        }
      } else if (/^\s*(?:[A-Za-z0-9_.-]+:|-\s)/m.test(block.body) && block.body.trim() !== '') {
        builder.drop(
          block.line,
          block.body,
          `fenced block in unsupported language "${block.lang || 'none'}" — convert to JSON (see docs/MAP-FORMAT.md)`,
        );
      }
      continue;
    }

    if (block.kind === 'table') {
      const header = block.header;
      const providerCol = findColumn(header, PROVIDER_COLS);
      const loginCol = findColumn(header, LOGIN_COLS);
      const portCol = findColumn(header, PORT_COLS);
      const ipCol = findColumn(header, IP_COLS);
      const urlCol = findColumn(header, URL_COLS);
      const machineCol = findColumn(header, ['machine', 'host', 'device', 'box', 'node']);
      const nameCol = findColumn(header, NAME_COLS);
      const kindCol = findColumn(header, KIND_COLS);
      const osCol = findColumn(header, OS_COLS);
      const statusCol = findColumn(header, STATUS_COLS);
      const roleCol = findColumn(header, ROLE_COLS);
      const descriptionCol = findColumn(header, DESCRIPTION_COLS);
      const wideNameCol = findColumn(header, WIDE_NAME_COLS);

      const isAccountTable = providerCol >= 0 && loginCol >= 0;
      const isMachineTable =
        !isAccountTable &&
        (machineCol >= 0 || nameCol === 0 || (nameCol >= 0 && ipCol >= 0)) &&
        (ipCol >= 0 || osCol >= 0 || (roleCol >= 0 && descriptionCol < 0)) &&
        loginCol < 0 &&
        portCol < 0;
      // The map's main shape: `Thing | What it is | Status` and its Machine / Site siblings.
      const isWideTable = !isAccountTable && !isMachineTable && portCol < 0 && descriptionCol >= 0 && wideNameCol >= 0;

      if (isAccountTable) {
        for (const row of block.rows) {
          const linked = cell(row.cells, findColumn(header, LINK_COLS));
          const secret = cell(row.cells, findColumn(header, SECRET_COLS));
          builder.addAccount(
            {
              provider: cell(row.cells, providerCol),
              login: cell(row.cells, loginCol),
              purpose: cell(row.cells, findColumn(header, PURPOSE_COLS)),
              statusRaw: cell(row.cells, statusCol),
              secretRef: secret,
              linked: linked ? [linked] : [],
            },
            row.line,
          );
        }
        continue;
      }

      if (portCol >= 0) {
        for (const row of block.rows) {
          const serviceName = cell(row.cells, nameCol >= 0 ? nameCol : findColumn(header, ['service']));
          const machineName = cell(row.cells, machineCol) ?? currentMachine;
          const serviceId = serviceName ? builder.addService({ name: serviceName, machine: machineName }, row.line) : undefined;
          const portText = cell(row.cells, portCol);
          const ports = [...new Set((portText?.match(/\d{1,5}/g) ?? []).flatMap((raw) => {
            const port = validPort(raw);
            return port === undefined ? [] : [port];
          }))];
          if (ports.length === 0) {
            builder.addPort(
              {
                machine: machineName,
                port: portText,
                protocol: cell(row.cells, findColumn(header, PROTOCOL_COLS)),
                healthRaw: cell(row.cells, findColumn(header, ['health', 'status', 'state'])),
                serviceId,
              },
              row.line,
            );
          } else {
            for (const port of ports) {
              builder.addPort(
                {
                  machine: machineName,
                  port,
                  protocol: cell(row.cells, findColumn(header, PROTOCOL_COLS)),
                  healthRaw: cell(row.cells, findColumn(header, ['health', 'status', 'state'])),
                  serviceId,
                },
                row.line,
              );
            }
          }
        }
        continue;
      }

      if (isMachineTable) {
        for (const row of block.rows) {
          const label = cell(row.cells, nameCol >= 0 ? nameCol : machineCol) ?? cell(row.cells, 0) ?? '';
          const description = cell(row.cells, descriptionCol) ?? cell(row.cells, roleCol);
          if (!isMachineRecordRow(label)) continue;
          const machineId = builder.addMachineFromLabel({ label, description, statusRaw: cell(row.cells, statusCol) }, row.line);
          const machineName = machineId ? builder.machines.get(machineId)?.name : undefined;
          const address = cell(row.cells, ipCol) ?? addressOf(label);
          if (address) builder.bindAddress(address, machineName ?? label, row.line);
          for (const port of portNumbers(`${label} ${description ?? ''}`)) {
            builder.addPort({ machine: machineName ?? label, port }, row.line);
          }
        }
        continue;
      }

      if (isWideTable) {
        const entity = wideEntityFor(header, kind);
        for (const row of block.rows) {
          const label = cell(row.cells, wideNameCol);
          const description = cell(row.cells, descriptionCol);
          const statusRaw = cell(row.cells, statusCol);
          if (entity === 'account') {
            builder.addAccountFromLabel({ label, description, statusRaw }, row.line);
          } else if (entity === 'machine') {
            if (!isMachineRecordRow(label ?? '')) continue;
            const machineId = builder.addMachineFromLabel({ label, description, statusRaw }, row.line);
            const machineName = machineId ? builder.machines.get(machineId)?.name : undefined;
            for (const port of portNumbers(`${label ?? ''} ${description ?? ''}`)) {
              builder.addPort({ machine: machineName, port }, row.line);
            }
          } else {
            builder.addService(
              {
                name: label,
                kind: entity === 'repo' ? 'repo' : /site|domain/.test((header[0] ?? '').toLowerCase()) ? 'site' : undefined,
                machine: cell(row.cells, machineCol) ?? currentMachine,
                links: rowUrls(row, description),
                address: addressOf(label, description),
                statusRaw,
              },
              row.line,
            );
          }
        }
        continue;
      }

      if (nameCol >= 0 || urlCol >= 0 || wideNameCol >= 0) {
        for (const row of block.rows) {
          builder.addService(
            {
              name: cell(row.cells, nameCol >= 0 ? nameCol : wideNameCol),
              kind: cell(row.cells, kindCol) ?? (kind === 'repo' ? 'repo' : undefined),
              machine: cell(row.cells, machineCol) ?? currentMachine,
              url: cell(row.cells, urlCol),
              links: rowUrls(row, cell(row.cells, descriptionCol)),
              address: addressOf(cell(row.cells, descriptionCol)),
              account: cell(row.cells, findColumn(header, ['account', 'owner', 'login'])),
              statusRaw: cell(row.cells, statusCol),
            },
            row.line,
          );
        }
        continue;
      }

      for (const row of block.rows) {
        builder.drop(row.line, row.cells.join(' | '), `table not recognised (header: ${header.join(' | ')}) at ${builder.ref(block.line)}`);
      }
      continue;
    }

    if (block.kind === 'bullet') {
      // A key or legend explains the vocabulary; it is reference text, not data.
      if (/how to read|status key|legend|notation|reading this|key to/i.test(pathAt(block.line).join(' '))) {
        builder.reference(block.line, block.text);
        continue;
      }
      handleBullet(builder, block, kind, currentMachine);
      const bulletMachine = currentMachine ?? builder.machineMentioned(block.text);
      if (bulletMachine) {
        for (const port of portNumbers(block.text)) builder.addPort({ machine: bulletMachine, port }, block.line);
      }
      const bulletAddress = addressOf(block.text.replace(URL_RE, ' '));
      if (kind === 'machine' && bulletAddress) {
        const label = machineLabel(splitFields(block.text)[0] ?? '');
        if (label) builder.bindAddress(bulletAddress, label, block.line);
      }
      continue;
    }

    if (block.kind === 'para') {
      if (block.text.includes('|')) continue;

      const urls = [...new Set([...(block.text.match(URL_RE) ?? []), ...block.links])];
      for (const url of urls) builder.addService({ url, machine: currentMachine }, block.line);

      // A port stated in prose: `Hub (8471)`, `foundry (3000/tcp)`.
      NAME_PORT.lastIndex = 0;
      let named: RegExpExecArray | null;
      while ((named = NAME_PORT.exec(block.text)) !== null) {
        const port = Number(named[2]);
        if (port < 1 || port > 65535) continue;
        if (port >= 1900 && port <= 2099) continue; // a year, not a port
        const namedService = named[1]!.trim();
        const serviceMachineId = currentMachine ? undefined : builder.machineOfService(namedService);
        const machine =
          currentMachine ??
          builder.machineMentioned(block.text) ??
          (serviceMachineId ? builder.machines.get(serviceMachineId)?.name : undefined);
        builder.addService(
          { name: namedService, machine, portHint: port, kind: named[3] ? 'app' : undefined },
          block.line,
        );
      }

      const withoutUrls = block.text.replace(URL_RE, ' ');
      HOST_PORT.lastIndex = 0;
      let hostPort: RegExpExecArray | null;
      while ((hostPort = HOST_PORT.exec(withoutUrls)) !== null) {
        const host = hostPort[1]!;
        const port = Number(hostPort[2]);
        if (port < 1 || port > 65535) continue;
        const resolved = IPV4.test(host) && host !== '127.0.0.1' ? builder.resolveMachine(host) : undefined;
        const machine = resolved ? builder.machines.get(resolved)?.name : currentMachine;
        if (!machine) {
          builder.drop(block.line, hostPort[0], 'port mentioned with a host the map does not place on a machine');
          continue;
        }
        builder.addPort({ machine, port }, block.line);
      }
    }
  }

  const parsed = builder.finish();
  // Final guard: nothing secret may be stored, even if a pattern is exotic.
  const offenders: UnparsedRegion[] = [];
  for (const account of parsed.accounts) {
    const scan = blockingFindings(`${account.provider} ${account.login} ${account.purpose} ${account.secretRef ?? ''}`);
    if (scan.length > 0) offenders.push({ sourceRef: account.sourceRef, text: account.id, reason: `looks like it contains a secret (${scan[0]!.code})` });
  }
  if (offenders.length > 0) {
    parsed.unparsed.push(...offenders);
    parsed.accounts = parsed.accounts.filter((a) => !offenders.some((o) => o.text === a.id));
  }
  return parsed;
}

/* ------------------------------------------------------------------ */
/* Bullets                                                             */
/* ------------------------------------------------------------------ */

/**
 * `purpose: preview deploys, dead` -> purpose "preview deploys", status "dead".
 * People write the status at the end of the sentence; take it at face value.
 */
function purposeAndStatus(text: string | undefined): { purpose?: string | undefined; status?: string | undefined } {
  if (!text) return {};
  const parts = text.split(',').map((part) => part.trim()).filter(Boolean);
  if (parts.length < 2) return { purpose: text };
  const last = parts[parts.length - 1]!;
  const word = last.toLowerCase();
  if (LIVE_WORDS.has(word) || DEAD_WORDS.has(word) || UNKNOWN_WORDS.has(word)) {
    return { purpose: parts.slice(0, -1).join(', '), status: last };
  }
  return { purpose: text };
}

function splitFields(text: string): string[] {
  const byPipe = text.split('|');
  if (byPipe.length >= 3) return byPipe.map((p) => p.trim()).filter((p) => p !== '');
  const byDash = text.split(/\s+[—–]\s+|\s+--\s+/);
  if (byDash.length >= 2) return byDash.map((p) => p.trim()).filter((p) => p !== '');
  return [text.trim()];
}

function handleBullet(
  builder: MapBuilder,
  block: BulletBlock,
  kind: string | undefined,
  currentMachine: string | undefined,
): void {
  const text = block.text;
  const line = block.line;
  const fields = splitFields(text);
  const links = block.links;
  const bareUrls = text.match(URL_RE) ?? [];
  const urls = [...new Set([...links, ...bareUrls])];

  // `- **live** — this thing is up` under a status key is explanation.
  if (isStatusKeyBullet(text)) {
    builder.reference(line, text);
    return;
  }

  if (kind === 'machine') {
    const [name, ...rest] = fields;
    if (!name) return builder.drop(line, text, 'machine bullet without a name');
    const address = addressOf(...rest) ?? addressOf(name);
    const label = machineLabel(name);
    const os = rest.find((value) => /\b(linux|windows|macos|mac os|ubuntu|debian|arch|fedora|freebsd|wsl)\b/i.test(value));
    builder.addMachine(
      { name: address && label === name ? address : label, tailnetIp: address, os, role: rest.filter((v) => v !== address && v !== os).join('; ') || undefined, status: 'unknown' },
      line,
    );
    return;
  }

  if (kind === 'account') {
    const [first, second, third, fourth] = fields;
    if (!first) return builder.drop(line, text, 'account bullet without a provider');
    if (second) {
      const split = purposeAndStatus(third);
      builder.addAccount({ provider: first, login: second, purpose: split.purpose, statusRaw: fourth ?? split.status }, line);
      return;
    }
    // `- GitHub: jason@example.com` / `- Gmail — personal [mail](https://mail.google.com)`
    const colon = /^([^:]{1,40}):\s*(.+)$/.exec(first);
    if (colon) {
      const [, provider, rest] = colon;
      const purposeMatch = /\((?:purpose|use)[:\s]+([^)]+)\)/i.exec(rest!);
      const login = rest!.replace(/\([^)]*\)/g, '').trim();
      const split = purposeAndStatus(purposeMatch?.[1]);
      builder.addAccount({ provider, login, purpose: split.purpose, statusRaw: split.status }, line);
      return;
    }
    if (/^\S+@\S+$/.test(first)) {
      builder.addAccount({ login: first }, line);
      return;
    }
    // A label-only bullet: `- Gmail — personal`. Take the label at face value.
    builder.addAccountFromLabel({ label: first, description: third ?? second }, line);
    return;
  }

  if (kind === 'repo') {
    const name = fields[0]?.replace(/\s*\([^)]*\)\s*$/, '').trim();
    if (!name) return builder.drop(line, text, 'repo bullet without a name');
    builder.addService({ name, kind: 'repo', url: urls[0], links: urls }, line);
    return;
  }

  if (kind === 'service' || kind === 'port') {
    // `Metrics UI (8428) sits on the T340` — the name and port come first.
    NAME_PORT.lastIndex = 0;
    const named = NAME_PORT.exec(text);
    const namedPort = named ? Number(named[2]) : 0;
    if (named && !(namedPort >= 1900 && namedPort <= 2099)) {
      const serviceName = named[1]!.trim();
      const port = namedPort;
      const serviceMachine =
        currentMachine ??
        builder.machineMentioned(text) ??
        builder.machines.get(builder.machineOfService(serviceName) ?? '')?.name;
      builder.addService({ name: serviceName, machine: serviceMachine, portHint: port, url: urls[0], links: urls }, line);
      return;
    }
    const [name, ...rest] = fields;
    if (!name) return builder.drop(line, text, 'service bullet without a name');
    const portMatch = /\b(\d{2,5})\b/.exec(rest.join(' '));
    const barePort = portMatch ? Number(portMatch[1]) : 0;
    const serviceId = builder.addService({ name, machine: currentMachine, url: urls[0], links: urls }, line);
    if (portMatch && currentMachine && !(barePort >= 1900 && barePort <= 2099)) {
      builder.addPort({ machine: currentMachine, port: portMatch[1], serviceId }, line);
    }
    return;
  }

  if (kind === 'link') {
    if (fields.length >= 2) {
      const from = builder.resolveAccountRef(fields[0]!);
      if (!from) return builder.drop(line, text, `link bullet names an unknown account "${fields[0]}"`);
      builder.queueLink(from, fields.slice(1).join(', '));
      return;
    }
    builder.drop(line, text, 'link bullet could not be split into two accounts');
    return;
  }

  // ---- No recognised section. The map still carries facts: take the address
  // and the link, then stop. Anything else is reported.
  const bulletIp = addressOf(text.replace(URL_RE, ' '));
  if (bulletIp && fields.length >= 2 && fields[0]) {
    const os = fields.find((value) => /\b(linux|windows|macos|mac os|ubuntu|debian|arch|fedora|freebsd)\b/i.test(value));
    builder.addMachine({ name: machineLabel(fields[0]) || bulletIp, tailnetIp: bulletIp, os, status: 'unknown' }, line);
    return;
  }

  if (urls.length > 0) {
    const name = fields[0]?.replace(/\s*\([^)]*\)\s*$/, '').trim();
    builder.addService({ name, url: urls[0], links: urls, machine: currentMachine }, line);
    return;
  }

  builder.drop(line, text, 'bullet outside any recognised section');
}

/* ------------------------------------------------------------------ */
/* Structured payload ingestion                                        */
/* ------------------------------------------------------------------ */

function ingestStructured(builder: MapBuilder, payload: StructuredPayload, line: number): void {
  for (const machine of payload.machines ?? []) {
    builder.addMachine(
      {
        name: asString(machine['name']) ?? '',
        tailnetIp: asString(machine['tailnetIp']) ?? asString(machine['ip']),
        os: asString(machine['os']),
        role: asString(machine['role']),
        status: parseStatus(asString(machine['status'])).status,
      },
      line,
    );
  }
  for (const account of payload.accounts ?? []) {
    builder.addAccount(
      {
        provider: asString(account['provider']),
        login: asString(account['login']) ?? asString(account['email']),
        purpose: asString(account['purpose']),
        statusRaw: asString(account['status']),
        secretRef: asString(account['secretRef']) ?? asString(account['secret_ref']),
        linked: [...asStringArray(account['linkedTo']), ...asStringArray(account['linked'])],
      },
      line,
    );
  }
  for (const service of payload.services ?? []) {
    builder.addService(
      {
        name: asString(service['name']),
        kind: asString(service['kind']),
        machine: asString(service['machine']),
        url: asString(service['url']),
        account: asString(service['account']),
        statusRaw: asString(service['status']),
      },
      line,
    );
  }
  for (const port of payload.ports ?? []) {
    builder.addPort(
      {
        machine: asString(port['machine']),
        port: asString(port['port']),
        protocol: asString(port['protocol']),
        service: asString(port['service']),
        healthRaw: asString(port['health']) ?? asString(port['status']),
      },
      line,
    );
  }
  for (const link of payload.links ?? []) {
    const from = asString(link['from']);
    const to = asString(link['to']);
    if (!from || !to) {
      builder.drop(line, JSON.stringify(link), 'link entry needs `from` and `to`');
      continue;
    }
    const fromId = builder.resolveAccountRef(from);
    if (!fromId) {
      builder.drop(line, `${from} -> ${to}`, `link source "${from}" does not match an account`);
      continue;
    }
    builder.queueLink(fromId, to);
  }
}
const IPV4 = /\b(?:\d{1,3}\.){3}\d{1,3}\b/;
const EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/;
const HANDLE = /(?:^|[\s(])@([A-Za-z0-9_.-]{2,})/;

/**
 * `Hub (8471)` and `foundry (3000/tcp)`. Years are excluded so a date in prose
 * does not become a port.
 */
const NAME_PORT = /\b([A-Za-z][A-Za-z0-9 .&'/+_-]{1,40}?)\s*\(\s*(\d{2,5})(?:\s*\/\s*(tcp|udp))?\s*\)/g;

/** Long-form table headers: `Thing | What it is | Status`. */
const DESCRIPTION_COLS = ['what it is', 'what it does', 'what', 'description', 'details', 'summary', 'notes'];
const WIDE_NAME_COLS = ['thing', 'site', 'machine', 'service', 'project', 'repo', 'repository', 'name', 'item', 'account', 'app', 'box', 'host'];
