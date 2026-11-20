# Secret scan

Scanned the exact staged bundle with Gitleaks 8.30.1, using redacted output. It reported 16 `generic-api-key` candidates, all in `evidence/demo-output/ledger-before-restart.jsonl`:

- 8 matches are the `issuer` field’s `LOCAL_TEST_ONLY` marker.
- 8 matches are the `executionId` field’s randomly generated UUIDs.

These are labels and event identifiers, not credentials. The test approval token is not written to the ledger or demo report; the ephemeral Ed25519 private key is never serialized. No other scanner candidates were found in the bundle.
