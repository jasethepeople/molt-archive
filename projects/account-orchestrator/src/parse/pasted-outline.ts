import { accountId as makeAccountId, portId, serviceId, slug } from '../ids.js';
import type { AccountInput, MachineInput, ParsedMap, PortInput, ServiceInput, ServiceKind, Status, UnparsedRegion } from '../types.js';
import { emptyParsedMap } from '../types.js';

const EMAIL = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i;
const IP = /\b(?:\d{1,3}\.){3}\d{1,3}\b/;
const URL_TOKEN = /(?:https?:\/\/[^\s),]+|(?:github|gitlab)\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+|[A-Za-z0-9.-]+\.(?:netlify\.app|website|com|site|app|org|dev|io)|youtu\.be\/[A-Za-z0-9_-]+)/gi;

function sourceRef(sourcePath: string, line: number): string {
  const file = sourcePath.split(/[\\/]/).pop() || sourcePath;
  return `${file}:${line}`;
}
function clean(value: string): string {
  return value.trim().replace(/^[-*\s]+/, '').replace(/[.,;]+$/, '').trim();
}
function statusFrom(raw: string | undefined): { status: Status; note?: string } {
  const value = (raw ?? '').trim();
  const first = value.split(/[—–(:]/, 1)[0]?.trim() ?? value;
  const state = first.toLowerCase();
  const status: Status = ['live', 'running', 'up', 'active'].includes(state)
    ? 'live'
    : ['dead', 'down', 'offline'].includes(state)
      ? 'dead'
      : 'unknown';
  const known = /^(live|running|up|active|dead|down|offline|unknown)$/i.test(first);
  return { status, note: known || !value ? undefined : `map said: ${value}` };
}
function repoUrl(name: string): string {
  return `https://github.com/jasethepeople/${name}`;
}
function kindFor(url?: string, fallback: ServiceKind = 'other'): ServiceKind {
  if (!url) return fallback;
  try {
    const host = new URL(url).hostname.toLowerCase();
    if (['github.com', 'gitlab.com', 'bitbucket.org'].includes(host)) return 'repo';
    if (host === 'supabase.com') return 'database';
    return 'site';
  } catch {
    return fallback;
  }
}
function normalizeUrl(raw: string): string {
  let value = raw.replace(/[.,;]+$/, '');
  if (/^(github|gitlab)\.com\//i.test(value)) value = `https://${value}`;
  if (/^(?:[A-Za-z0-9-]+\.)+(?:netlify\.app|website|com|site|app|org|dev|io)$/i.test(value)) value = `https://${value}`;
  return value;
}
function accountProvider(login: string): string {
  if (/@gmail\.com$/i.test(login)) return 'Gmail';
  if (login.includes('@')) return login.split('@')[1]!.split('.')[0]!.replace(/^./, (c) => c.toUpperCase());
  return 'Domain';
}
function machineName(raw: string): string {
  const text = clean(raw);
  if (/^Oracle VM/i.test(text)) return 'Oracle VM foundry';
  if (/^T340\b/i.test(text)) return 'T340';
  if (/^Acer\b/i.test(text)) return 'Acer';
  if (/^iknow\b/i.test(text)) return 'iknow';
  if (/^Mom's Windows/i.test(text)) return "Mom's Windows 11 laptop";
  if (/^Phone\b/i.test(text)) return 'S22 Ultra';
  return text;
}
function rawStatus(raw: string): string {
  return raw.trim().split(/[—–]/, 1)[0]?.trim() ?? raw.trim();
}

/** Parse the line-oriented text export of Jason's Network Map without producing a rewritten map file. */
export function parsePastedNetworkMap(text: string, sourcePath: string): ParsedMap | undefined {
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  if (!/^Jason['’]s Network Map\s*$/i.test(lines[0]?.trim() ?? '') || !lines.some((line) => line.trim() === 'Thing')) return undefined;

  const parsed = emptyParsedMap(sourcePath);
  const machineIds = new Map<string, string>();
  const machineIps = new Map<string, string>();
  const serviceByKey = new Map<string, ServiceInput>();
  const serviceNames = new Map<string, ServiceInput>();
  const portByKey = new Map<string, PortInput>();
  const accountIdSet = new Set<string>();
  const serviceIdSet = new Set<string>();
  const machineIdSet = new Set<string>();

  const addMachine = (name: string, description: string, statusRaw: string, line: number): MachineInput => {
    const canonical = machineName(name);
    const key = canonical.toLowerCase();
    const existing = machineIds.get(key);
    if (existing) return parsed.machines.find((item) => item.id === existing)!;
    const status = statusFrom(statusRaw);
    const tailnet = /(?:tailnet|tailscale)\s+(\d{1,3}(?:\.\d{1,3}){3})/i.exec(`${name} ${description}`)?.[1];
    const os = /\b(Ubuntu(?:\s+\d+(?:\.\d+)*)?|Kali|Windows\s+11|Omarchy)\b/i.exec(`${name} ${description}`)?.[1];
    const id = slug(canonical);
    let uniqueId = id;
    let n = 2;
    while (machineIdSet.has(uniqueId)) uniqueId = `${id}#${n++}`;
    machineIdSet.add(uniqueId);
    const machine: MachineInput = {
      id: uniqueId,
      name: canonical,
      tailnetIp: tailnet,
      os,
      role: description.trim() || undefined,
      status: status.status,
      notes: status.note,
      sourceRef: sourceRef(sourcePath, line),
    };
    parsed.machines.push(machine);
    machineIds.set(key, uniqueId);
    if (tailnet) machineIps.set(tailnet, uniqueId);
    for (const ip of `${name} ${description}`.match(/\b(?:\d{1,3}\.){3}\d{1,3}\b/g) ?? []) machineIps.set(ip, uniqueId);
    return machine;
  };

  const addAccount = (label: string, description: string, statusRaw: string, line: number): void => {
    const email = EMAIL.exec(label)?.[0];
    const login = email ?? clean(label);
    const provider = email ? accountProvider(email) : 'Domain';
    const id = makeAccountId(provider, login);
    if (accountIdSet.has(id)) return;
    accountIdSet.add(id);
    const status = statusFrom(statusRaw);
    parsed.accounts.push({
      id,
      provider,
      login,
      purpose: clean(description) || 'unknown',
      status: status.status,
      notes: status.note,
      sourceRef: sourceRef(sourcePath, line),
    });
  };

  const addService = (name: string, url: string | undefined, kind: ServiceKind, statusRaw: string, line: number, machine?: string, notes?: string): ServiceInput => {
    const displayName = clean(name) || (url ? new URL(url).host : 'unnamed');
    const machineId = machine ? machineIds.get(machineName(machine).toLowerCase()) : undefined;
    const canonicalKind = url ? kindFor(url, kind) : kind;
    // A canonical repo can have a separately named project/alias in the map
    // (for example `The Room (molt-room)` and the appendix's `molt-room`). Keep
    // those repo records distinct. Repeated mentions of a hosted site are one.
    const key = url
      ? `${canonicalKind}|${normalizeUrl(url).toLowerCase()}${canonicalKind === 'repo' ? `|${slug(displayName)}` : ''}`
      : `${canonicalKind}|${slug(displayName)}|${machineId ?? ''}`;
    const existing = serviceByKey.get(key);
    if (existing) {
      if (existing.status === 'unknown') existing.status = statusFrom(statusRaw).status;
      if (!existing.machineId && machineId) existing.machineId = machineId;
      existing.notes = existing.notes ?? notes ?? statusFrom(statusRaw).note;
      return existing;
    }
    const idBase = serviceId(canonicalKind, displayName, machineId);
    let id = idBase;
    let n = 2;
    while (serviceIdSet.has(id)) id = `${idBase}#${n++}`;
    serviceIdSet.add(id);
    const status = statusFrom(statusRaw);
    const service: ServiceInput = {
      id,
      name: displayName,
      kind: canonicalKind,
      url: url ? normalizeUrl(url) : undefined,
      status: status.status,
      machineId,
      sourceRef: sourceRef(sourcePath, line),
      notes: notes ?? status.note,
    };
    parsed.services.push(service);
    serviceByKey.set(key, service);
    serviceNames.set(displayName.toLowerCase(), service);
    return service;
  };

  const addPort = (machine: string, port: number, serviceName: string, line: number): void => {
    const machineId = machineIds.get(machineName(machine).toLowerCase());
    if (!machineId || port < 1 || port > 65535) return;
    const service = serviceNames.get(serviceName.toLowerCase());
    const key = `${machineId}|${port}|tcp`;
    if (portByKey.has(key)) return;
    const record: PortInput = {
      id: portId(machineId, port, 'tcp'),
      machineId,
      port,
      protocol: 'tcp',
      health: 'unknown',
      serviceId: service?.id,
      sourceRef: sourceRef(sourcePath, line),
    };
    parsed.ports.push(record);
    portByKey.set(key, record);
  };

  // Identity table is represented as repeated label / description / status rows.
  const thing = lines.findIndex((line) => line.trim() === 'Thing');
  if (thing >= 0) {
    for (let i = thing + 3; i + 2 < lines.length && lines[i]!.trim() && !/^\d+\.\s/.test(lines[i]!); i += 3) {
      addAccount(lines[i]!, lines[i + 1]!, rawStatus(lines[i + 2]!), i + 1);
    }
  }

  // The six rows in section 11 are machine/device records; service rows below it are never hosts.
  for (let i = 0; i < lines.length - 2; i += 1) {
    if (!/^\s*Machine\s*$/.test(lines[i]!)) continue;
    if (!/^\s*What it is\s*$/.test(lines[i + 1]!) || !/^\s*Status\s*$/.test(lines[i + 2]!)) continue;
    for (let j = i + 3; j + 2 < lines.length && lines[j]!.trim(); j += 3) {
      if (/^\d+\.\s/.test(lines[j]!)) break;
      const name = lines[j]!.trim();
      const desc = lines[j + 1]!.trim();
      const status = lines[j + 2]!.trim();
      if (/^Supabase\s+—|^Vercel$|^Netlify|^Google Play/i.test(name)) break;
      addMachine(name, desc, status, j + 1);
      // machine rows continue in triples; service rows begin after the blank delimiter
    }
    break;
  }
  // Machine IDs are known before service parsing, so all URLs/prose can resolve to them.

  // Status legend: all entries are kept verbatim as references, never data or gaps.
  for (let i = 4; i <= 13 && i < lines.length; i += 1) {
    const raw = lines[i]!.trim();
    if (raw) parsed.references.push({ sourceRef: sourceRef(sourcePath, i + 1), text: raw, reason: 'status legend/reference text' });
  }

  const appendUrlService = (rawUrl: string, line: number, context: string): void => {
    const url = normalizeUrl(rawUrl);
    let host = '';
    try { host = new URL(url).hostname.toLowerCase(); } catch { return; }
    if (/^(?:github|gitlab)\.com$/i.test(host)) {
      const repo = url.split('/').filter(Boolean).at(-1)?.replace(/\.git$/i, '') ?? host;
      const label = context.split(/\s+[—–]\s+/)[0]?.trim();
      const name = label && !/^(?:https?:|github\.com|gitlab\.com)/i.test(label) && !/^Repo:/i.test(label) ? label : repo;
      const status = /status:\s*([^.;]+)/i.exec(context)?.[1] ?? '';
      const machine = /Oracle VM/i.test(context) ? 'Oracle VM foundry' : /Acer/i.test(context) && /port\s+8471/i.test(context) ? 'Acer' : undefined;
      addService(name, url, 'repo', status, line, machine, context);
      return;
    }
    const ipHost = IP.test(host);
    let name = host;
    let machine: string | undefined;
    let status = /\b(LIVE|RUNNING|BUILT|EXISTS|IN STORAGE|SUBMITTED|IN TESTING|PARKED|PLANNED|UNCHECKED)\b/i.exec(context)?.[1] ?? '';
    if (host === '100.83.138.76') {
      name = /\b3100\b/.test(url) ? 'integration-gateway' : 'Foundry dashboard';
      machine = 'Oracle VM foundry';
      status = 'running';
    } else if (host === '127.0.0.1' || host === 'localhost') {
      name = 'Intel Hub';
      machine = 'Acer';
      status = 'built';
    } else if (host.endsWith('.netlify.app')) {
      const lead = context.split(/\s+[—–:]\s+/)[0]?.trim();
      if (lead && lead.length < 60 && !lead.includes('github.com')) name = lead.replace(/^Live landing$/i, host);
    } else if (host === 'youtu.be') {
      name = 'MOLT Forge demo video';
    } else if (/thebet\.website/i.test(host)) {
      name = 'The Bet';
    } else if (host === 'jaquelin-hellenberg.com') {
      name = 'jaquelin-hellenberg.com';
    } else if (/molt-forge\.site/i.test(host)) {
      name = 'molt-forge.site';
    } else if (host === 'meshpointsystems.com' || host === 'should.love') {
      name = host;
    }
    if (ipHost && host !== '127.0.0.1' && machineIps.has(host)) machine = parsed.machines.find((item) => item.id === machineIps.get(host))?.name;
    const kind: ServiceKind = host === 'localhost' || ipHost ? 'app' : 'site';
    addService(name, url, kind, status, line, machine, context);
    const port = new URL(url).port;
    if (port) {
      const portMachine = machine ?? (host === '100.83.138.76' ? 'Oracle VM foundry' : undefined);
      if (portMachine) addPort(portMachine, Number(port), name, line);
    }
  };

  // Explicit URLs and repo paths in the named-project sections.
  for (let i = 50; i < Math.min(lines.length, 145); i += 1) {
    const line = lines[i]!.trim();
    if (!line || (i + 1 >= 101 && i + 1 <= 105)) continue;
    URL_TOKEN.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = URL_TOKEN.exec(line)) !== null) appendUrlService(match[0], i + 1, line);
  }

  // Two identity domains are also named as hosted sites in the source prose.
  for (const lineNo of [20, 23, 26]) {
    const value = lines[lineNo - 1]?.trim() ?? '';
    if (value && value.includes('.')) appendUrlService(value, lineNo, value);
  }

  // Infra/platform services listed after the six machine rows.
  const platformRows: Array<[RegExp, string, string, ServiceKind]> = [
    [/^Supabase\s+—\s+The Bet/i, 'Supabase — The Bet', "The Bet's database and Edge Functions", 'database'],
    [/^Supabase\s+—\s+undercurrent-web/i, 'Supabase — undercurrent-web', "Undercurrent's database, 18 tables", 'database'],
    [/^Vercel$/i, 'Vercel', 'Hosts meshpointsystems.com', 'site'],
    [/^Netlify\s+\(/i, 'Netlify', 'Hosts the listed Netlify sites', 'site'],
    [/^Google Play\s+—\s+BugSplat/i, 'Google Play — BugSplat', 'Developer account; The Bet v1.3 internal testing', 'app'],
  ];
  for (const [pattern, name, note, kind] of platformRows) {
    const index = lines.findIndex((line) => pattern.test(line.trim()));
    if (index >= 0) addService(name, undefined, kind, 'live', index + 1, undefined, `${note}; ${lines[index + 2]?.trim() ?? 'LIVE'}`);
  }
  // Intel Hub is a service as well as a repo, and its line explicitly names its local endpoint/port.
  const intelHubLine = lines.findIndex((line) => /^intel-hub\s+—\s+github\.com\//i.test(line.trim()));
  const intelHubSourceLine = intelHubLine >= 0 ? intelHubLine + 1 : 80;
  addService('Intel Hub', 'http://127.0.0.1:8471', 'app', 'built', intelHubSourceLine, 'Acer', 'Verified 15/15; exists, not running; deploy target Acer.');
  addPort('Acer', 8471, 'Intel Hub', intelHubSourceLine);

  // Every row of the public repository appendix is an explicit repo record.
  const appendixStart = lines.findIndex((line) => /^Appendix\s+—\s+full repo estate/i.test(line));
  if (appendixStart >= 0) {
    for (let i = appendixStart + 2; i < lines.length; i += 1) {
      const raw = lines[i]!.trim();
      if (!raw || raw.startsWith('This is every public repo')) continue;
      const name = raw.split(/\s+—\s+/)[0]!.trim();
      if (!name || /^(?:Appendix|jasethepeople)$/i.test(name)) continue;
      addService(name, repoUrl(name), 'repo', 'unknown', i + 1, undefined, raw);
    }
  }

  // Machine-local port statements: Oracle VM rows and the Acer Intel Hub row.
  const foundryLine = lines.findIndex((line) => /^Foundry\s+\(Agent Foundry\)/i.test(line.trim()));
  const gatewayLine = lines.findIndex((line) => /^integration-gateway\s+—\s+github\.com\//i.test(line.trim()));
  addPort('Oracle VM foundry', 3000, 'Foundry dashboard', foundryLine >= 0 ? foundryLine + 1 : 74);
  addPort('Oracle VM foundry', 3100, 'integration-gateway', gatewayLine >= 0 ? gatewayLine + 1 : 75);

  // The five intentionally unresolved items: two planned projects and three SaaS shelf notes.
  const unresolved = [
    /^moltcoin\s+—/i,
    /^molt-verify\s+—/i,
    /^23 single-user browser apps\b/i,
    /^Ledger Notebook,/i,
    /^Three carry \$4\.99 one-time Pro unlocks/i,
  ];
  for (let index = 0; index < lines.length; index += 1) {
    const value = lines[index]!.trim();
    if (unresolved.some((pattern) => pattern.test(value))) {
      parsed.unparsed.push({ sourceRef: sourceRef(sourcePath, index + 1), text: value, reason: 'not represented as a Phase 1 account, machine, service, or port record' });
    }
  }

  // Keep all maps deterministic and ensure account IDs remain unique.
  parsed.accounts.sort((a, b) => a.id.localeCompare(b.id));
  parsed.machines.sort((a, b) => a.id.localeCompare(b.id));
  parsed.services.sort((a, b) => a.id.localeCompare(b.id));
  parsed.ports.sort((a, b) => a.id.localeCompare(b.id));
  parsed.unparsed.sort((a, b) => a.sourceRef.localeCompare(b.sourceRef));
  return parsed;
}
