# MOLT/GHOST Batch Report

**Status:** All five jobs are implemented and verified in the working tree on `feat/ghost-agent-glue`. No commit or push was made. No dependencies were added.

## 1. Kernel expiry fix

Added optional `expires_logical` to GHOST bundles with a serde default, preserving old v2 bundles as unexpired. `GhostBundle::decrypt` and `GhostStore::load` reject at `now >= expires_logical`; expired bundles are not silently deleted. Added an expired-bundle listing API, returning a fallible result so corrupt or unreadable entries are not silently ignored. Threaded the caller-supplied logical clock through agent spawning.

**Checks:** `cargo check`, `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, and `cargo test` — all passed at this stage.

**Limit:** Expiry is a logical-clock policy check, not a cryptographic time lock or trusted deadline.

## 2. GHOST Vault

Added the `ghost-vault` CLI and vault library for sealing UTF-8 or binary records, opening them after an optional logical release point, listing status, verifying, and shredding. Event logs contain signed metadata and commitments; sealed content stays in GHOST bundles. Identity seed files are local and permission-restricted.

**Checks:** `cargo check`, `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, and `cargo test` — all passed at this stage. CLI smoke test covered seal, verify, and open.

**Limit:** Release and expiry are enforced against caller-supplied logical time; a holder of the scope key can bypass the CLI policy. There is no key broker or trusted time source.

## 3. GHOST Bid

Added the `ghost-bid` CLI and auction library. Bids are sealed under bidder-scoped keys; commits after close and reveals before close or at/after expiry are refused. The default reveal window is 100 logical ticks. Highest valid revealed amount wins; equal amounts break by earliest commit order. `verify` checks event signatures, deadline rules, bundle commitments/expiry, and shredding records. Losing and unrevealed bundles can be shredded; `--include-winner` opts into shredding the winner too.

**Checks:** `cargo check`, `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, and `cargo test` — all passed at this stage. Tests cover hidden bids, deadlines, wrong keys, tampering, tie-breaking, late commits, and shredding. CLI smoke test covered a full auction and verification.

**Limits:** Caller-supplied logical time is not a trusted clock. Once a bundle has been shredded, verification can no longer recompute that bid's plaintext commitment; it checks the signed reveal/commit linkage and the shred record instead.

## 4. Molt Receipts

Added the `molt-receipts` CLI and receipt library. `start` calls `spawn_agent`, stores a restricted local agent signing seed, and logs `JOB_STARTED`; `log` and `finish` append agent-signed work events and recompute step/gas totals. Receipt creation verifies the job events before deterministic export and signs the final event/hash with one witness. `verify` works from the receipt file alone: it rechecks event integrity/signatures, ordering, chain/final hash, witness signature, and gas totals. Cargo exposes the requested hyphenated executable name.

**Checks:** `cargo check`, `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, and `cargo test` — all passed at this stage. Tests cover a full session, payload and chain tampering, a wrong witness key, gas recomputation, and out-of-order exporter input. CLI smoke test covered start → log → finish → receipt → verify.

**Limits:** A witness receipt is one witness, not quorum or multisignature assurance. The GHOST tier label does not encrypt work-step payloads; as specified by the current meter, a GHOST step without ciphertext/commitment fields costs 50 gas. Gas is a prototype accounting unit, not currency. A receipt records signed work; it does not prove quality or move money.

## 5. Molt Audit

Added the `molt-audit` CLI and audit library. JSONL ingest signs each source record; named fields are replaced by commitment/payload references and sealed into GHOST bundles. Verification runs Observatory history checks plus per-event integrity/signature checks. Replay supports a logical-clock or event-ID target and refuses a state claim with `Unavailable` if a sealed bundle in the replay prefix is missing. Export verifies before building a digest-only CAMP1 chain and can include one witness receipt. `sealed-open` checks the logged commitment, and `report` returns deterministic event/actor counts, sealed-field count, state hash, and Chronicle entries.

**Checks:** `cargo check`, `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, and `cargo test` — all passed at this stage. Tests cover ingest/verify, payload and signature tampering, point-in-time replay, digest privacy, correct/wrong keys, and missing bundles. CLI smoke test covered ingest → verify → replay → sealed-open → export → report.

**Limits:** The embedded actor public key is self-asserted; there is no identity registry or external trust anchor. The witness is singular, with no quorum. There is no external timestamp or chain anchor. Replay hashes the kernel's generic `WorldState`; it is not a domain-specific reconstruction of financial books. Missing sealed data is reported as `Unavailable` rather than skipped.

## Final verification

After all five jobs, the exact required commands were run successfully:

- `cargo check` — passed.
- `cargo fmt --check` — passed.
- `cargo clippy --all-targets -- -D warnings` — passed with no warnings.
- `cargo test` — passed: 50 library unit tests and 17 integration tests; 0 failures. Binary and doc-test targets also completed successfully.
- `cargo build --bins` — passed; the requested `ghost-vault`, `ghost-bid`, `molt-receipts`, and `molt-audit` executables were built.
- End-to-end CLI smoke flows for all four apps — passed using temporary local stores and keys.

No implementation blocker remains. External identity/key custody, trusted time, independent witness policy, external anchoring, domain-specific audit replay, work quality, pricing, and payment execution remain outside these prototypes and were not claimed or independently verified.
