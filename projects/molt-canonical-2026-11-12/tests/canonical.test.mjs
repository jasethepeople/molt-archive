import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { canonicalizeJsonText } from '../molt-canonical.mjs';

const cli = fileURLToPath(new URL('../molt-canonical.mjs', import.meta.url));
const digest = text => createHash('sha256').update(text, 'utf8').digest('hex');

test('key sort uses canonical key order', () => {
  assert.equal(canonicalizeJsonText('{"b":2,"a":1}'), '{"a":1,"b":2}');
});

test('nested objects sort keys and arrays preserve order', () => {
  assert.equal(canonicalizeJsonText('{"z":{"b":2,"a":1},"a":[3,2,1]}'), '{"a":[3,2,1],"z":{"a":1,"b":2}}');
});

test('object keys sort by UTF-16 code units, not Unicode code points', () => {
  assert.equal(canonicalizeJsonText('{"":2,"𐀀":1}'), '{"𐀀":1,"":2}');
});

test('1.0 serializes as 1', () => {
  assert.equal(canonicalizeJsonText('{"n":1.0}'), '{"n":1}');
});

test('1e30 serializes as 1e+30', () => {
  assert.equal(canonicalizeJsonText('{"n":1e30}'), '{"n":1e+30}');
});

test('0.004 keeps its decimal representation', () => {
  assert.equal(canonicalizeJsonText('{"n":0.004}'), '{"n":0.004}');
});

test('negative zero serializes as zero', () => {
  assert.equal(canonicalizeJsonText('{"n":-0}'), '{"n":0}');
});

test('strings escape newline as backslash-n', () => {
  assert.equal(canonicalizeJsonText('{"s":"a\\nb"}'), '{"s":"a\\nb"}');
});

test('non-ASCII characters remain unescaped', () => {
  assert.equal(canonicalizeJsonText('{"s":"€"}'), '{"s":"€"}');
});

test('literals remain unchanged and object keys sort', () => {
  assert.equal(canonicalizeJsonText('{"t":true,"f":false,"n":null}'), '{"f":false,"n":null,"t":true}');
});

test('empty object and array canonicalize as empty', () => {
  assert.equal(canonicalizeJsonText('{}'), '{}');
  assert.equal(canonicalizeJsonText('[]'), '[]');
});

test('SHA-256 is computed from the canonical UTF-8 string', () => {
  const canonical = canonicalizeJsonText('{"b":2,"a":1}');
  assert.equal(canonical, '{"a":1,"b":2}');
  assert.equal(digest(canonical), digest('{"a":1,"b":2}'));
});

test('file CLI prints canonical JSON and labeled SHA-256 exactly', () => {
  const directory = mkdtempSync(join(tmpdir(), 'molt-canonical-'));
  try {
    const input = join(directory, 'input.json');
    writeFileSync(input, '{"b":2,"a":1}', 'utf8');
    const full = spawnSync(process.execPath, [cli, input], { encoding: 'utf8' });
    assert.equal(full.status, 0, full.stderr);
    assert.equal(full.stdout, `{"a":1,"b":2}\nsha256: ${digest('{"a":1,"b":2}')}\n`);
    assert.equal(full.stderr, '');
    const hashOnly = spawnSync(process.execPath, [cli, '--hash', input], { encoding: 'utf8' });
    assert.equal(hashOnly.status, 0, hashOnly.stderr);
    assert.equal(hashOnly.stdout, `${digest('{"a":1,"b":2}')}\n`);
    assert.equal(hashOnly.stderr, '');
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('invalid JSON exits non-zero without partial stdout', () => {
  const result = spawnSync(process.execPath, [cli], { input: '{bad json', encoding: 'utf8' });
  assert.notEqual(result.status, 0);
  assert.equal(result.stdout, '');
  assert.match(result.stderr, /molt-canonical: Invalid JSON/);
});

test('duplicate object keys are rejected', () => {
  assert.throws(() => canonicalizeJsonText('{"a":1,"a":2}'), /duplicate object key/);
});

test('lone Unicode surrogates are rejected', () => {
  assert.throws(() => canonicalizeJsonText('{"s":"\\ud800"}'), /unpaired high surrogate/);
  assert.throws(() => canonicalizeJsonText('{"s":"\\udc00"}'), /unpaired low surrogate/);
});

test('numbers that overflow the IEEE 754 range are rejected', () => {
  assert.throws(() => canonicalizeJsonText('{"n":1e400}'), /finite IEEE 754 range/);
});

test('stdin hash-only form prints only the digest', () => {
  const result = spawnSync(process.execPath, [cli, '--hash'], { input: '{"b":2,"a":1}', encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, `${digest('{"a":1,"b":2}')}\n`);
  assert.equal(result.stderr, '');
});

test('bad flags and missing files fail clearly without stdout', () => {
  const badFlag = spawnSync(process.execPath, [cli, '--nope'], { encoding: 'utf8' });
  assert.notEqual(badFlag.status, 0);
  assert.equal(badFlag.stdout, '');
  assert.match(badFlag.stderr, /usage:/);
  const missing = spawnSync(process.execPath, [cli, '/no/such/molt-input.json'], { encoding: 'utf8' });
  assert.notEqual(missing.status, 0);
  assert.equal(missing.stdout, '');
  assert.match(missing.stderr, /molt-canonical:/);
});

test('invalid UTF-8 input is rejected without stdout', () => {
  const result = spawnSync(process.execPath, [cli], { input: Buffer.from([0x22, 0xff, 0x22]) });
  assert.notEqual(result.status, 0);
  assert.equal(result.stdout.length, 0);
  assert.match(result.stderr.toString('utf8'), /input is not valid UTF-8/);
});
