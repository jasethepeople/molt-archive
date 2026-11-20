# Round 4 — exact Markdown parser verification

**Input:** `jason-network-map-2026-11-09.md`, 468 lines  
**SHA-256:** `387420fdbe07cf16790b06b3dab95d2df48ac71433d8726589296e54900a5f12`

## Fresh-database seed

Seed exited **0**. JSON summary:

```json
{
  "accounts": 10,
  "machines": 6,
  "services": 315,
  "ports": 3,
  "gaps": 5,
  "references": 10,
  "warnings": 3
}
```

The three ports are 3000 and 3100 on **Oracle VM "foundry"**, and 8471 on **Acer Aspire 5**. The six machine records are Oracle VM "foundry", T340 (Dell PowerEdge T340), Acer Aspire 5, iknow (Kali laptop), Mom's Windows 11 laptop, and Phone S22 Ultra. The five gaps are exactly map lines 53, 54, and 112–114.

## Verification

`verify --json` exited **0** with verdict **PASS**: 12 hard checks passed, 0 failed. The no-secrets check passed. The three warnings concern account labels on lines 25–27 that have no login address in the map.

## Empty-map control

An empty Markdown file was seeded with `--allow-empty` (exit 0); verification exited **1**, verdict **FAIL**. The failed hard checks were `extraction-nonzero` and `registry-nonzero`.

## Build and tests

- `npx tsc -p tsconfig.json`: exit 0, clean.
- `npm test`: 38 passed, 0 failed.
- Dependency scope unchanged: `better-sqlite3` remains the only runtime dependency.

The exact source file was parsed as provided; it was not normalized or rewritten. No repository push or Phase 2 work was performed.
