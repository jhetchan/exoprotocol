# AGENTS.md

This repository uses Exo's vendor-agnostic protocol as the primary agent contract.

## Required Read Order

1. `.exo/agents/EXO_PROTOCOL.md` (canonical source of truth)
2. Vendor adapter in `.exo/agents/vendors/` (for execution style only)

## Enforcement Boundary

- Lifecycle and safety are enforced in code, not prompt docs.
- Required lifecycle: `session-start -> execute -> session-finish`.
- Worker execution should use `--require-session`.

## Conflict Rule

If an adapter conflicts with canonical protocol:
- `EXO_PROTOCOL.md` wins.
