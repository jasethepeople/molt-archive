/**
 * Shared types for the Phase 1 registry.
 *
 * The map parser produces a `ParsedMap`: a plain, dependency-free description of
 * what the network map says. Nothing in here is written to the database until
 * `seed.ts` has validated it. Every entity carries a `sourceRef` (file:line) so a
 * record can always be traced back to the line it came from — that is what makes
 * the verification report honest.
 */

export type Status = 'live' | 'dead' | 'unknown';
export type Health = 'up' | 'down' | 'unknown';
export type ServiceKind = 'repo' | 'site' | 'database' | 'vm' | 'app' | 'api' | 'other';

export interface MachineInput {
  /** Stable slug, e.g. `acer`. */
  id: string;
  name: string;
  tailnetIp?: string | undefined;
  os?: string | undefined;
  role?: string | undefined;
  status: Status;
  sourceRef: string;
  notes?: string | undefined;
}

export interface AccountInput {
  /** Stable id, e.g. `github:jason`. */
  id: string;
  provider: string;
  login: string;
  /** Never empty. `unknown` means the map did not say — it is not a guess. */
  purpose: string;
  status: Status;
  /** Location of the credential, never the credential. */
  secretRef?: string | undefined;
  sourceRef: string;
  notes?: string | undefined;
}

export interface ServiceInput {
  id: string;
  name: string;
  kind: ServiceKind;
  accountId?: string | undefined;
  url?: string | undefined;
  status: Status;
  machineId?: string | undefined;
  sourceRef: string;
  notes?: string | undefined;
}

export interface PortInput {
  id: string;
  serviceId?: string | undefined;
  machineId: string;
  port: number;
  protocol: 'tcp' | 'udp';
  health: Health;
  sourceRef: string;
}

export interface LinkInput {
  fromAccount: string;
  toAccount: string;
  relation: string;
  sourceRef: string;
}

/** Something in the map the parser could not place. Reported, never dropped. */
export interface UnparsedRegion {
  sourceRef: string;
  text: string;
  reason: string;
}

export interface ParsedMap {
  sourcePath: string;
  machines: MachineInput[];
  accounts: AccountInput[];
  services: ServiceInput[];
  ports: PortInput[];
  links: LinkInput[];
  unparsed: UnparsedRegion[];
  /** Map text kept as explanation rather than data (status keys, legends). */
  references: UnparsedRegion[];
  warnings: string[];
}

export function emptyParsedMap(sourcePath: string): ParsedMap {
  return {
    sourcePath,
    machines: [],
    accounts: [],
    services: [],
    ports: [],
    links: [],
    unparsed: [],
    references: [],
    warnings: [],
  };
}
