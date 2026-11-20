/**
 * Secret hygiene.
 *
 * The registry stores *where* a credential lives, never the credential. Two
 * guards live here:
 *
 *  1. `scanForSecrets` — runs over every string the importer is about to store
 *     (and over the source map during `verify`). Strong patterns are blockers:
 *     the seed aborts and prints the file:line. Weak/heuristic patterns only warn,
 *     because a 40-character hex string is just as likely to be a commit SHA.
 *  2. `parseSecretRef` — a `secret_ref` must be a location string with a known
 *     scheme (`env:GITHUB_TOKEN`), not a value.
 *
 * Both are deliberately boring and easy to extend. No cleverness, no network.
 */

export type Severity = 'block' | 'warn';

export interface SecretFinding {
  code: string;
  severity: Severity;
  /** Masked excerpt — never the full value. */
  excerpt: string;
  index: number;
  message: string;
}

interface Rule {
  code: string;
  severity: Severity;
  pattern: RegExp;
  message: string;
}

/** Known credential shapes. These must never reach the database. */
const STRONG_RULES: Rule[] = [
  {
    code: 'private-key-block',
    severity: 'block',
    pattern: /-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----/,
    message: 'PEM private key block',
  },
  {
    code: 'aws-access-key-id',
    severity: 'block',
    pattern: /\b(?:AKIA|ASIA|AGPA|AIDA)[0-9A-Z]{16}\b/,
    message: 'AWS access key id',
  },
  {
    code: 'github-token',
    severity: 'block',
    pattern: /\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b/,
    message: 'GitHub token',
  },
  {
    code: 'openai-key',
    severity: 'block',
    pattern: /\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}\b/,
    message: 'sk- style API key',
  },
  {
    code: 'anthropic-key',
    severity: 'block',
    pattern: /\bsk-ant-[A-Za-z0-9_-]{20,}\b/,
    message: 'Anthropic API key',
  },
  {
    code: 'slack-token',
    severity: 'block',
    pattern: /\bxox[abprs]-[A-Za-z0-9-]{10,}\b/,
    message: 'Slack token',
  },
  {
    code: 'google-api-key',
    severity: 'block',
    pattern: /\bAIza[0-9A-Za-z_-]{35}\b/,
    message: 'Google API key',
  },
  {
    code: 'stripe-key',
    severity: 'block',
    pattern: /\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b/,
    message: 'Stripe secret key',
  },
  {
    code: 'jwt',
    severity: 'block',
    pattern: /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/,
    message: 'JWT',
  },
  {
    code: 'basic-auth-url',
    severity: 'block',
    pattern: /\b[a-z][a-z0-9+.-]*:\/\/[^\s/:@]+:[^\s/@]+@/i,
    message: 'URL with embedded credentials',
  },
  {
    code: 'bearer-token',
    severity: 'block',
    pattern: /\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*/,
    message: 'Bearer token',
  },
  {
    code: 'assigned-credential',
    severity: 'block',
    pattern:
      /\b(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|client[_-]?secret)\b\s*[:=]\s*["']?([^\s"',;]{6,})["']?/i,
    message: 'credential assigned inline',
  },
];

/** Heuristics. Reported, never used to block. */
const WEAK_RULES: Rule[] = [
  {
    code: 'long-hex',
    severity: 'warn',
    pattern: /\b[0-9a-fA-F]{40,}\b/,
    message: 'long hex string (could be a commit SHA, or a key)',
  },
  {
    code: 'long-opaque-token',
    severity: 'warn',
    pattern: /\b(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{48,}\b/,
    message: 'long opaque string',
  },
];

/** Values that look like assignments but are obviously not credentials. */
const BENIGN_ASSIGNMENT = [
  /^<.*>$/,
  /^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?$/,
  /^(?:env|keychain|vault|1password|bitwarden|foundry|file|systemd):[A-Za-z0-9._@/+-]+$/i,
  /^(?:redacted|removed|none|null|n\/?a|unknown|unset|changeme|placeholder|example|xxx+|\*+)$/i,
  /^(?:true|false|\d{1,4})$/i,
];

function mask(value: string): string {
  const trimmed = value.trim();
  if (trimmed.length <= 6) return '*'.repeat(trimmed.length);
  return `${trimmed.slice(0, 3)}${'*'.repeat(Math.min(8, trimmed.length - 5))}${trimmed.slice(-2)}`;
}

/**
 * Iterate every match of `pattern` in `text`.
 *
 * The clone is forced global (a shared non-global regex would return the same
 * match forever) and the loop is bounded, so a pathological pattern costs one
 * scan instead of the process.
 */
function eachMatch(pattern: RegExp, text: string, visit: (match: RegExpExecArray) => void): void {
  const re = new RegExp(pattern.source, pattern.flags.includes('g') ? pattern.flags : `${pattern.flags}g`);
  let guard = 0;
  let match: RegExpExecArray | null;
  while ((match = re.exec(text)) !== null && guard < 20_000) {
    guard += 1;
    visit(match);
    if (match[0].length === 0) re.lastIndex += 1;
  }
}

/** Scan a single string. Returns findings, strongest first. */
export function scanForSecrets(text: string): SecretFinding[] {
  const findings: SecretFinding[] = [];
  if (!text) return findings;

  for (const rule of STRONG_RULES) {
    eachMatch(rule.pattern, text, (match) => {
      const captured = match[1];
      if (rule.code === 'assigned-credential' && captured && BENIGN_ASSIGNMENT.some((b) => b.test(captured.trim()))) {
        return;
      }
      findings.push({
        code: rule.code,
        severity: rule.severity,
        excerpt: mask(match[0]),
        index: match.index,
        message: rule.message,
      });
    });
  }

  for (const rule of WEAK_RULES) {
    eachMatch(rule.pattern, text, (match) => {
      findings.push({
        code: rule.code,
        severity: rule.severity,
        excerpt: mask(match[0]),
        index: match.index,
        message: rule.message,
      });
    });
  }

  return findings.sort((a, b) => (a.severity === b.severity ? a.index - b.index : a.severity === 'block' ? -1 : 1));
}

export function blockingFindings(text: string): SecretFinding[] {
  return scanForSecrets(text).filter((f) => f.severity === 'block');
}

/** Schemes a secret reference may use. Anything else is a typo or a value. */
export const SECRET_REF_SCHEMES = [
  'env',
  'keychain',
  'vault',
  '1password',
  'bitwarden',
  'foundry',
  'file',
  'systemd',
] as const;

export type SecretRefScheme = (typeof SECRET_REF_SCHEMES)[number];

export interface SecretRef {
  scheme: SecretRefScheme;
  path: string;
}

export type SecretRefResult = { ok: true; ref: SecretRef } | { ok: false; error: string };

/**
 * A secret reference is `scheme:path`, e.g. `env:GITHUB_TOKEN`,
 * `keychain:github-jason`, `foundry:secret/github-jason`. It names a location.
 * `unset` is allowed and means "location not recorded yet".
 */
export function parseSecretRef(value: string): SecretRefResult {
  const raw = value.trim();
  if (raw === '') return { ok: false, error: 'empty secret reference' };
  if (raw.toLowerCase() === 'unset') return { ok: false, error: '`unset` means no location is recorded — leave the field empty instead' };

  const match = /^([a-z0-9]+):(.+)$/i.exec(raw);
  if (!match) {
    return { ok: false, error: 'expected `scheme:path` (for example `env:GITHUB_TOKEN`), never a secret value' };
  }
  const scheme = match[1]!.toLowerCase();
  const path = match[2]!.trim();

  if (!(SECRET_REF_SCHEMES as readonly string[]).includes(scheme)) {
    return { ok: false, error: `unknown reference scheme \`${scheme}\` (allowed: ${SECRET_REF_SCHEMES.join(', ')})` };
  }
  if (!/^[A-Za-z0-9._@/+-]+$/.test(path)) {
    return { ok: false, error: 'reference path must be letters, digits, and . _ @ / + - only' };
  }
  const scan = blockingFindings(path);
  if (scan.length > 0) {
    return { ok: false, error: `reference path looks like a credential (${scan[0]!.code})` };
  }
  return { ok: true, ref: { scheme: scheme as SecretRefScheme, path } };
}
