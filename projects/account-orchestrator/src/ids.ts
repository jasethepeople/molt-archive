/**
 * Deterministic identifiers.
 *
 * Re-seeding the same map must produce the same ids, byte for byte. That is the
 * whole point: a record's id is a function of what the map says, not of the order
 * rows were inserted. Anything else makes cross-run diffing (and Phase 2 health
 * history) meaningless.
 */

/** Lowercase, ASCII-ish, dash-separated. `Oracle VM` -> `oracle-vm`. */
export function slug(input: string): string {
  const normalised = input
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return normalised.length > 0 ? normalised : 'x';
}

export function machineId(name: string): string {
  return slug(name);
}

export function accountId(provider: string, login: string): string {
  return `${slug(provider)}:${slug(login)}`;
}

export function serviceId(kind: string, name: string, machine?: string): string {
  const base = `${slug(kind)}:${slug(name)}`;
  return machine ? `${base}@${slug(machine)}` : base;
}

export function portId(machine: string, port: number, protocol: string): string {
  return `${slug(machine)}:${port}/${protocol}`;
}

/**
 * Hand out unique ids in insertion order: `repo:api`, `repo:api@acer`, then
 * `repo:api#2` ... Deterministic for a given input order.
 */
export class IdAllocator {
  private readonly used = new Set<string>();

  constructor(seed: Iterable<string> = []) {
    for (const id of seed) this.used.add(id);
  }

  has(id: string): boolean {
    return this.used.has(id);
  }

  next(preferred: string, fallbacks: string[] = []): string {
    if (!this.used.has(preferred)) {
      this.used.add(preferred);
      return preferred;
    }
    for (const candidate of fallbacks) {
      if (!this.used.has(candidate)) {
        this.used.add(candidate);
        return candidate;
      }
    }
    let n = 2;
    while (this.used.has(`${preferred}#${n}`)) n += 1;
    const id = `${preferred}#${n}`;
    this.used.add(id);
    return id;
  }
}
