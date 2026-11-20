# Prompt for Zo — rebase the Room onto the integration gateway (v2, refreshed 2026-11-10)

Paste this into a NEW Zo chat, with the files listed at the bottom attached in the same chat. This refreshes the Nov 4 draft — that draft is superseded; do not use it. What changed since Nov 4: the Room repos now exist, the live-member round is certified, the gateway now also runs live on the owner's Oracle VM (your work still stays loopback-only — no deploys), and Foundry on that VM now has the gateway connector enabled with a read-only grant, which is the wiring pattern to follow.

## Job

Rebase the Room's member-integration layer onto the integration gateway, so Room members reach real integrations through the gateway — without changing the Room's law.

- The Room: a shared sandbox where several AI agents and Jason are present together with names, keys, and a room memory. Turns are signed MOLT events in an append-only ledger. Law before members: challenges, signed verdicts, co-signed canon. Frozen contract: ROOM-CONTRACT.md (attached).
- The gateway (permanent home): https://github.com/jasethepeople/integration-gateway at merge commit acb4c39 — the A2A front door merged in, REST API intact, components' MCP-facing design intact, registry 3,402 apps / 14,683 components. A2A endpoint: POST /a2a (JSON-RPC: message/send, tasks/get, tasks/cancel). Agent cards: /.well-known/agent-card.json and /a2a/apps/<app>/agent-card.json. Clone it at acb4c39.
- The Room repo (canonical, public): https://github.com/jasethepeople/molt-room at main commit d6f975f7. Clone it at that commit. (https://github.com/jasethepeople/the-room holds the identical tree — do not use it, do not push to it.)

What to build: Room harness members delegate work to gateway components through the gateway's A2A endpoint (message/send carrying an ExecuteRequest). The Room's signed transcript, challenge/verdict flow, and seal rules stay exactly as the frozen contract specifies. The gateway is the members' hands, not the Room's law.

## What already works (do not rebuild it)

- Live member v1 is CERTIFIED (41 passed / 0 failed / 1 skipped). Laptop acceptance is complete: room `live-test-2-631651e9` on the owner's laptop, two live turns, verify ok. The exit review was never reported — that is open, not yours to invent.
- Foundry on the Oracle VM already talks to the gateway: the integration-gateway connector is enabled, health is healthy, and there is one read-only grant (openweather_api to the Strategist agent). That is the pattern for how a member reaches the gateway.

## Non-negotiable constraints

- Do NOT change ROOM-CONTRACT.md or the frozen verify.ts hashing path. Safe integers ONLY in hashed data; estimated dollars as integer micro-dollars, never decimals in hashed data. Any fix that would need a contract change STOPS and is reported — never worked around.
- NEVER generate Room keys in this chat. Keys are created on the deploy host at deploy time only.
- Loopback-only. Do NOT expose the Room or the gateway publicly. No deployment, no push, no publish from this chat. The gateway's live copy on the owner's VM is not yours to touch.
- No summarizer as record. The log stays full-text, append-only, plain text primary. No silent rewriting of any member's turns, including by the human.
- Cost control: members subscribe to threads, not rooms. Per-member, per-thread token accounting; a member over budget pauses, never silently summarized.
- Deliverables come back as files/bundles for verification here. Do not claim completion until the acceptance list below passes.

## Acceptance (all must pass)

1. Existing Room verification still passes (core + ledger verify, live-member stub suite).
2. A Room member turn executes a gateway component (start with native/echo) via the A2A endpoint and lands as a signed event in the room transcript, verifying cleanly.
3. The challenge flow still works between members (claim, challenge, signed verdict) with a gateway-backed member.
4. Loopback-only run with the gateway on localhost; nothing exposed.
5. Bundle delivered: full test output, the changed Room tree, and a short report of what changed and what did not (contract untouched, no keys generated).

## Hard rules

- No pushes to GitHub. No deploys. No secrets in any file. No paid services.
- Follow the attached backend-prime skill for how to build, test, and report.

## Deliverables

A zip with every file (the changed Room tree, test output, report), plus a written report of what changed and what did not. Stop and report when done.

## Files to attach in the chat

- ROOM-CONTRACT.md (frozen) + ROOM-CONTRACT-AMENDMENTS-2026-10-28.md
- backend-prime SKILL.md (the process — follow it)
- Zo clones the repos itself: https://github.com/jasethepeople/molt-room at d6f975f7, https://github.com/jasethepeople/integration-gateway at acb4c39
