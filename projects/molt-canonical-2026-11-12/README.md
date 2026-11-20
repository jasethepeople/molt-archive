# molt-canonical

A standalone Node.js CLI that canonicalizes a JSON document using RFC 8785 (JSON Canonicalization Scheme) and computes the SHA-256 hash of the canonical UTF-8 bytes. It uses only Node.js built-ins; there are no dependencies, build steps, network access, or keys.

## Requirements

Node.js 22 or 24. No install step is needed.

## Run

```sh
node molt-canonical.mjs file.json
```

This prints the canonical JSON, followed by `sha256: ` and the 64-character lowercase SHA-256 digest on the next line. To print only the digest:

```sh
node molt-canonical.mjs --hash file.json
```

Both forms also accept input from standard input when no file is given:

```sh
cat file.json | node molt-canonical.mjs
cat file.json | node molt-canonical.mjs --hash
```

Invalid JSON, invalid UTF-8, duplicate object member names, unpaired Unicode surrogates, non-finite parsed numbers, missing files, and unsupported flags produce an error on stderr, a non-zero exit status, and no stdout output.

## RFC 8785 rules implemented

- Objects and arrays have no insignificant whitespace; arrays retain their input order.
- Object names are sorted at every level by JavaScript's default UTF-16 code-unit order, as required by JCS.
- Numbers use ECMAScript `Number` serialization (shortest round-trip form); negative zero becomes `0`. Values that parse outside the finite IEEE 754 number range are rejected.
- Strings use JSON escaping for quotation marks, reverse solidus, and control characters. Standard short escapes are used where available; other U+0000–U+001F characters use `\u00xx`. Valid non-ASCII characters are emitted without escaping as UTF-8.
- `true`, `false`, and `null` are unchanged.
- Duplicate member names and invalid Unicode surrogate sequences are rejected rather than silently canonicalized.

## Tests

Run the built-in Node.js test suite:

```sh
node --test
```

Or use the package script:

```sh
npm test
```
