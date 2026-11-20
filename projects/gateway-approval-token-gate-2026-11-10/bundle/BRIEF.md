# Approval-token gate build brief

**Status:** Implementation and independent archive verification complete. **Source of truth:** `jasethepeople/integration-gateway` at merge commit `acb4c399ad6ba19ee87ba01bb7bbd5b678591791`. Older working snapshots are reference only; changes are being applied to this pinned copy.

Implement the owner-approved gateway gate at the shared `Router.execute` entry so REST and A2A cannot bypass it. Require an unexpired single-use token bound to caller, target, exact event payload, and a signed approval ledger event. Deny before component load and app credential access. Preserve the existing retry semantics below authorization, and keep app credentials additional to—not replaced by—the approval token.

The test issuer is local-only, clearly labeled, and confined to tests and the loopback demo. Production defaults fail closed until a trusted Foundry approval authority and caller resolver are connected. Do not push to GitHub, deploy, alter bind behavior, use paid services, or put secrets in the bundle. Deliver an overlay of every changed/new source file for the pinned checkout, test/demo evidence, and a report that names the Foundry integration as open work. The archive does not duplicate the untouched registry; apply `source/` to the exact base commit.
