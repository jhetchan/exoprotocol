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

<!-- exo:governance:begin -->
<!-- Governance hash: 88fe490d86f8c193 -->
# ExoProtocol — Agent Operating Instructions

This repository is governed by ExoProtocol. All AI agent work must follow the session lifecycle.

## ExoProtocol Governance

- kernel: exo-kernel 0.1.0
- lock hash: `88fe490d86f8c193...`
- generated: 2026-02-18T11:30:03+08:00

### Filesystem Deny Rules

- **RULE-SEC-001**: deny read, write on `~/.aws/**`, `~/.ssh/**`, `**/.env*`
- **RULE-GIT-001**: deny read, write, delete on `.git/**`
- **RULE-KRN-001**: deny write, delete on `exo/kernel/**`
- **RULE-DEL-001**: deny delete on `src/**`

### Structural Rules

- **RULE-LOCK-001** (require_lock): Blocked by RULE-LOCK-001 (acquire a ticket lock first).
- **RULE-CHECK-001** (require_checks): Blocked by RULE-CHECK-001 (checks must pass before done).
- **RULE-EVO-001** (evolution_gate): Practice is mutable, governance requires explicit human approval.
- **RULE-EVO-002** (patch_first): Patch-first evolution required.

### Default Budgets

- max files changed: 12

### Approved Checks

- `npm test`
- `npm run lint`
- `pytest`
- `python -m pytest`
- `python3 -m compileall exo`
- `python3 -m pytest`
- `ruff format --check exo/ tests/`
- `ruff check exo/ tests/`

### Active Intents

- **INT-20260216-040435-LIBD**: True Enforcement: mechanical governance guarantees for stateless agents — boundary: *No kernel changes. No new Python dependencies except watchdog (for daemon). All enforcement is deterministic (no LLM in the loop). Sealed policy is JSON, not a new DSL.*
  - TKT-20260216-040444-AWF3-EPIC: Watchdog Daemon — exo-watchd out-of-band enforcement
- **INT-20260217-220647-KXU0**: Intent-to-Verdict Rendering: surface full traceability chain in PR check and adapters — boundary: *Only modify pr_check.py, adapters.py, and their tests. No kernel changes. No new CLI commands.*
  - TKT-20260217-220656-MAYA: PR check: resolve intent context per session and render in human output [allow: exo/stdlib/pr_check.py, tests/test_pr_check.py]
  - TKT-20260217-220659-KSCJ: Adapter-generate: embed ticket and requirement provenance in generated files [allow: exo/stdlib/adapters.py, tests/test_adapter_generation.py]
  - TKT-20260217-221056-YMDG: Drop LOC budget from drift score formula [allow: exo/stdlib/reconcile.py, exo/stdlib/adapters.py, tests/test_intent_accountability.py, tests/test_adapter_generation.py]

### Source of Truth

The values above are a **snapshot** generated from the governance manifest.

Manifest paths:
- `.exo/config.yaml` — budgets, checks allowlist, scheduler config
- `.exo/governance.lock.json` — compiled rules, deny patterns, source hash

### Test-Driven, Manifest-First Workflow

This principle applies to **all code you write** — governance and application logic alike.

1. **Config/contract is the source of truth.** When a value is defined in a config file,
   schema, manifest, or contract — code must load it from that source at runtime.
   Never copy a value from a config file and paste it as a literal in source code.
2. **Tests verify the wiring, not the value.** Tests must assert that code reads from
   the config/contract, not that it produces a specific hardcoded result.
   A test that passes when you swap the config value *and* swap the assertion is useless —
   it only proves both sides were copy-pasted from the same place.
3. **If you can change a config value and no test breaks, the test is missing.**
   Every configurable value should have at least one test that will vary the input
   and verify the output follows.

Examples:
- **BAD**: `assert budget == 10` (hardcoded, passes even if config is ignored)
- **GOOD**: set config to 42, assert output contains 42 and not the old default
- **BAD**: `MAX_RETRIES = 3` (literal in source when retries is in config)
- **GOOD**: `max_retries = load_config()['max_retries']`

### Operational Learnings

When you discover a reusable pattern, gotcha, or operational insight during a session:
- Record it with `exo reflect` (CLI) or `exo_reflect` (MCP) — NOT your private memory
- ExoProtocol reflections are injected into future session bootstraps for all agents
- Private memory files (MEMORY.md, .cursorrules, etc.) are agent-specific and invisible to the team
- If you must write to private memory, also create an ExoProtocol reflection with the same insight

**Private memory monitoring**: If `private_memory.watch_paths` in `.exo/config.yaml` is empty,
add the absolute path to your memory file (e.g., `~/.claude/.../memory/MEMORY.md`) so that
ExoProtocol can detect when you write to private memory without creating a shared reflection.

### End-of-Work Reflection

When you complete significant work or the user appears to be wrapping up:
- **Proactively** run `exo reflect --pattern '<what kept happening>' --insight '<what was learned>'`
  for each non-trivial insight discovered during the conversation
- Do NOT wait for `session-finish` — many users close the editor without explicit session end
- Good reflection triggers: bug fixes, CI failures, gotchas, architectural decisions, workflow improvements

### Feature Governance Protocol

All source code must be governed by the feature manifest (`.exo/features.yaml`).

Before writing code:
1. Check which feature your work belongs to: `exo features`
2. Add `@feature:<feature-id>` / `@endfeature` tags around new code blocks

After finishing:
- Run `exo trace` to verify no uncovered code
- If you built a new subsystem, add it to `.exo/features.yaml` first

### Tool Reuse Protocol

Before writing new utility functions, SEARCH the tool registry:
  `exo tool-search "<keywords>"`

After building a reusable utility, REGISTER it:
  `exo tool-register <module> <function> --description "..."`

Mark a tool as used when you import/call it:
  `exo tool-use <tool_id>`


## Session Lifecycle

1. `exo session-start --ticket-id <TICKET> --vendor <VENDOR> --model <MODEL> --task "<TASK>"`
2. Read `.exo/cache/sessions/<actor>.bootstrap.md`
3. Execute work within ticket scope
4. `exo session-finish --ticket-id <TICKET> --summary "<SUMMARY>" --set-status review`

## Enforcement

- Governance rules are enforced at the kernel level, not by prompt
- The bootstrap file contains your session's scope, checks, and lifecycle commands
- Drift detection runs at session-finish and is recorded in the session memento
- Audit sessions may be triggered to review your work independently

## Governed Push

Before pushing code, ALWAYS run checks first:

```
exo push                      # runs exo check, then git push (recommended)
# OR
exo check && git push         # manual equivalent
```

Do NOT use bare `git push` — it bypasses governance checks.
If checks fail, fix the issues before pushing.

## Non-Negotiables

- No governed execution without active session
- Respect lock ownership and ticket scope
- Verification is default at finish; break-glass must be explicit
- All configurable values must be loaded from their source of truth at runtime — never hardcode, always test
- Read `.exo/LEARNINGS.md` for operational learnings from prior sessions

<!-- exo:governance:end -->
