import assert from 'node:assert/strict';
import { test } from 'node:test';
import { blockingFindings, parseSecretRef, scanForSecrets } from '../src/secrets.js';

test('finds obvious credentials', () => {
  const cases: [string, string][] = [
    ['token ghp_abcdefghijklmnopqrstuvwxyz0123456789 in map', 'github-token'],
    ['AWS AKIAIOSFODNN7EXAMPLE', 'aws-access-key-id'],
    ['password: hunter2hunter2', 'assigned-credential'],
    ['-----BEGIN OPENSSH PRIVATE KEY-----', 'private-key-block'],
    ['postgres://user:secretpw@db.local/app', 'basic-auth-url'],
    ['Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456', 'bearer-token'],
  ];
  for (const [text, code] of cases) {
    const codes = blockingFindings(text).map((finding) => finding.code);
    assert.ok(codes.includes(code), `expected ${code} in ${codes.join(', ')} for: ${text}`);
  }
});

test('does not flag references, placeholders, or ordinary map text', () => {
  const safe = [
    'github | fixture-user@example.com | code hosting | live | env:GITHUB_TOKEN',
    'secret ref: 1password:github',
    'password: <not recorded>',
    'token: ${GITHUB_TOKEN}',
    'purpose: password reset flows',
    '| Acer | 8471 | tcp | Hub | up |',
    'repo fixture-owner/network-map',
  ];
  for (const text of safe) {
    assert.deepEqual(blockingFindings(text), [], `unexpected finding for: ${text}`);
  }
});

test('weak heuristics warn but never block', () => {
  const findings = scanForSecrets('commit 0123456789abcdef0123456789abcdef01234567');
  assert.ok(findings.some((finding) => finding.code === 'long-hex'));
  assert.deepEqual(blockingFindings('commit 0123456789abcdef0123456789abcdef01234567'), []);
});

test('never prints the full value in an excerpt', () => {
  const secret = 'ghp_abcdefghijklmnopqrstuvwxyz0123456789';
  const [finding] = blockingFindings(`token ${secret}`);
  assert.ok(finding);
  assert.ok(!finding.excerpt.includes(secret));
  assert.ok(finding.excerpt.includes('*'));
});

test('secret references must be locations, not values', () => {
  const ok = parseSecretRef('env:GITHUB_TOKEN');
  assert.equal(ok.ok, true);
  if (ok.ok) assert.deepEqual(ok.ref, { scheme: 'env', path: 'GITHUB_TOKEN' });

  assert.equal(parseSecretRef('keychain:github-fixture').ok, true);
  assert.equal(parseSecretRef('ghp_abcdefghijklmnopqrstuvwxyz0123456789').ok, false);
  assert.equal(parseSecretRef('teleport:GITHUB_TOKEN').ok, false);
  assert.equal(parseSecretRef('env:GITHUB TOKEN').ok, false);
  assert.equal(parseSecretRef('').ok, false);
});
