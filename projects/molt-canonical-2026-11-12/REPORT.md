Verdict: Yes — implements RFC 8785 JCS canonicalization and SHA-256 for valid JSON/I-JSON input.
Tests: 20 passed, 0 failed (`npm test`) on Node v22.23.4.

## Files

- `molt-canonical.mjs` — standalone CLI and canonicalizer.
- `tests/canonical.test.mjs` — built-in Node test-runner coverage.
- `package.json` — `npm test` runs `node --test`; no dependencies.
- `README.md` — usage, CLI forms, and implemented JCS rules.
- `REPORT.md` — this report.

## Test results

- Command: `npm test` (runs `node --test`).
- Runtime: Node v22.23.4.
- Result: 20 tests passed; 0 failed, skipped, or cancelled.
- Coverage includes all 13 requested cases, file and stdin CLI forms, SHA-256 computed with Node crypto, UTF-16 key ordering, duplicate names, invalid Unicode, overflow numbers, invalid UTF-8, bad flags, and missing files.

## RFC 8785 rules implemented

- No insignificant whitespace; arrays preserve order; object keys sort at every level by UTF-16 code units.
- ECMAScript number serialization is used, including `1.0` → `1`, `1e30` → `1e+30`, and negative zero → `0`; non-finite parsed values are rejected.
- JSON string escaping is used for quotes, reverse solidus, and control characters, with standard short escapes where available; valid non-ASCII text remains unescaped and is hashed as UTF-8.
- Booleans and null retain their JSON spellings.
- Duplicate object member names, unpaired surrogates, and invalid UTF-8 are rejected instead of being silently altered.
- The hash is SHA-256 of the canonical UTF-8 bytes.

## Open items

- Node 24 was not installed in this environment, so runtime execution was verified on Node 22 only. The implementation uses built-in APIs and syntax supported by Node 22 and Node 24.
- The optional extra RFC examples were not added; all required cases pass and the key-order suite also checks the UTF-16/code-point distinction.
- No unresolved product decisions.

## What was NOT done

No repo was pushed, nothing was deployed, no network was accessed, no key was generated, and no existing MOLT file, ledger, key, or config was changed.
