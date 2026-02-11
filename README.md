# ExoProtocol Kernel (MVP)

Repo-native governance kernel for multi-agent development. Enforces ticket locks, policy compilation, session lifecycle, and audit trails — all stored in your git repo, no external services needed.

## Install

```bash
pip install -e .          # or: pipx install .
```

## 5-minute quickstart

### 1. Initialize a governed repo

```bash
exo init --seed
```

This creates `.exo/` with a constitution, governance lock, starter SPEC, and seed tickets.

### 2. Check status

```bash
exo status
```

Shows ticket counts, active lock, dispatch candidate, and integrity check results.

### 3. Pick up a ticket

```bash
exo next --owner your-name
```

Dispatches the highest-priority todo ticket with resolved blockers, acquires a lock, and prints what to work on.

### 4. Do the work (governed execution)

```bash
exo do
```

Runs the controlled execution pipeline: scope check, action validation, audit trail.

### 5. Run checks and finish

```bash
exo check
exo session-finish --summary "What I did" --set-status review
```

### Full agent session flow (recommended)

```bash
# Start a session (creates bootstrap context for the agent)
EXO_ACTOR=agent:claude exo session-start \
  --ticket-id TICKET-001 \
  --vendor anthropic --model claude-code \
  --context-window 200000 \
  --task "implement the feature described in the ticket"

# ... agent does work ...

# If context runs out, suspend and resume later
EXO_ACTOR=agent:claude exo session-suspend --reason "context window exhausted"
EXO_ACTOR=agent:claude exo session-resume

# Finish and hand off for review
EXO_ACTOR=agent:claude exo session-finish \
  --summary "implemented feature, added tests" \
  --set-status review
```

### Recovery after crashes

```bash
# Scan for orphaned sessions
exo session-scan --stale-hours 24

# Clean up stale sessions and release orphaned locks
exo session-cleanup --stale-hours 24 --release-lock
```

## CLI verbs

- `exo init`
- `exo sidecar-init`
- `exo build-governance`
- `exo audit`
- `exo plan <input>`
- `exo next [--hours N] [--distributed --remote <name>]`
- `exo lease-renew [--ticket-id <id>] [--owner <owner>] [--role <role>] [--hours N] [--distributed --remote <name>]`
- `exo lease-heartbeat [--ticket-id <id>] [--owner <owner>] [--hours N] [--distributed --remote <name>]`
- `exo lease-release [--ticket-id <id>] [--owner <owner>] [--distributed --remote <name>]`
- `exo do [TICKET-ID]`
- `exo check`
- `exo status`
- `exo jot "..."`
- `exo thread "..."`
- `exo promote <thread-id> --to ticket`
- `exo recall "query"`
- `exo subscribe [--topic <topic-id>] [--since <line:N>] [--limit N]`
- `exo ack <ref-id> [--required N] [--actor-cap <cap>]`
- `exo quorum <ref-id> [--required N]`
- `exo head --topic <topic-id>`
- `exo cas-head --topic <topic-id> --cap <cap> [--expected <ref>] [--new <ref>] [--max-attempts N]`
- `exo decide-override <intent-id> --override-cap <cap> --rationale-ref <ref> [--outcome ALLOW|DENY|ESCALATE|SANDBOX]`
- `exo policy-set --policy-cap <cap> [--bundle <path>] [--version <v>]`
- `exo submit-intent --intent "<text>" [--topic <topic-id>] [--scope-allow <glob>] [--scope-deny <glob>] [--action-kind <kind>] [--target <path>]`
- `exo check-intent <intent-id>`
- `exo begin-effect <decision-id> --executor-ref <name> --idem-key <key>`
- `exo commit-effect <effect-id> --status OK|FAIL|RETRYABLE_FAIL|CANCELED [--artifact-ref <ref>]`
- `exo session-start [--ticket-id <id>] [--vendor <vendor>] [--model <model>] [--context-window <tokens>] [--role <role>] [--task "<text>"] [--acquire-lock] [--distributed --remote <name>]`
- `exo session-suspend --reason "<why>" [--ticket-id <id>] [--no-release-lock] [--stash]`
- `exo session-resume [--ticket-id <id>] [--no-acquire-lock] [--pop-stash] [--distributed --remote <name>] [--hours N] [--role <role>]`
- `exo session-scan [--stale-hours N]`
- `exo session-cleanup [--stale-hours N] [--force] [--release-lock]`
- `exo session-finish --summary "<text>" [--ticket-id <id>] [--set-status keep|review|done] [--artifact <ref>] [--blocker <text>] [--next-step <text>] [--skip-check --break-glass-reason "<why>"] [--release-lock|--no-release-lock]`
- `exo worker-poll [--topic <topic-id>] [--since <line:N>] [--limit N] [--cursor-file <path>] [--no-cursor] [--require-session]`
- `exo worker-loop [--topic <topic-id>] [--since <line:N>] [--limit N] [--iterations N] [--sleep-seconds N] [--stop-when-idle] [--cursor-file <path>] [--no-cursor] [--require-session]`
- `exo read-ledger [ref-id] [--type <record-type>] [--since <line:N>] [--topic <topic-id>] [--intent <intent-id>]`
- `exo escalate-intent <intent-id> --kind <kind> [--ctx-ref <ref>]`
- `exo observe --ticket <id> --tag <tag> --msg "..."`
- `exo propose --ticket <id> --kind <practice_change|governance_change|tooling_change> --symptom "..." --root-cause "..."`
- `exo approve <PROP-ID> [--decision approved|rejected]`
- `exo apply <PROP-ID>`
- `exo distill <PROP-ID>`

## JSON mode

All commands support `--format json`.

## Lane-aware dispatch (optional)

`exo next` supports lane-aware scheduling via `.exo/config.yaml`:

```yaml
scheduler:
  enabled: true
  global_concurrency_limit: 3
  lanes:
    - name: Feature Lane
      allowed_types: [feature, refactor]
      count: 1
    - name: Bug Lane
      allowed_types: [bug, security]
      count: 1
    - name: Chore Lane
      allowed_types: [docs, test]
      count: 1
```

Behavior:
- picks highest-scoring `todo` ticket with resolved blockers,
- skips tickets whose lane is at capacity,
- returns explicit reasoning when all candidates are lane-blocked or global capacity is reached.

Note: local kernel lock is still single-active-lock per repo. Lane scheduling primarily helps queue selection and distributed/multi-actor coordination.

## Self-evolving loop

```bash
python3 -m exo.cli observe --ticket TICKET-000-EPIC --tag drift --msg "Observed failure"
python3 -m exo.cli propose --ticket TICKET-000-EPIC --kind practice_change --summary "..." --symptom "..." --root-cause "..."
python3 -m exo.cli approve PROP-001
python3 -m exo.cli apply PROP-001
python3 -m exo.cli distill PROP-001
```

Kernel-seeded templates and schema:
- `.exo/templates/OBS.template.md`
- `.exo/templates/PROP.template.yaml`
- `.exo/templates/REV.template.md`
- `.exo/templates/memory.index.template.yaml`
- `.exo/schemas/proposal.schema.json`

## Architecture boundary

- `exo/kernel/` is the enforcement core only (governance, tickets/locks, audit, error taxonomy, rule checks).
- `exo/control/` contains transport-neutral control-plane wrappers over kernel primitives.
- `exo/stdlib/` contains orchestration and userland behaviors (dispatch, recall, scratchpad, evolution protocol, CLI workflow engine).
- `exo/orchestrator/` is explicit Layer-3 agent/task/workflow orchestration; execution routes through kernel syscall checks (`submit/check/begin/commit`).

Layer-4 memory boundary:
- governed execution paths (`do` / intent `check`) deny writes to `.exo/memory/**`
- memory updates must flow through explicit distillation (`exo distill`)

Layer-3 orchestration API example:

```python
from exo.orchestrator import Orchestrator, OrchestratorTask

orchestrator = Orchestrator(".", actor="agent:builder")
task = OrchestratorTask(
    task_id="TASK-001",
    intent="Write README",
    action_kind="write_file",
    target="README.md",
    scope_allow=["README.md"],
)
result = orchestrator.run_task(task)
```

## Frozen Kernel API

Public kernel surface is intentionally fixed to 10 functions:

- `load_governance(root)`
- `verify_governance(gov)`
- `open_session(root, actor)`
- `mint_ticket(session, intent, scope, ttl)`
- `validate_ticket(gov, ticket)`
- `check_action(gov, session, ticket, action)`
- `resolve_requirements(decision, evidence)`
- `commit_plan(session, ticket, action)`
- `append_audit(root, event)`
- `seal_result(session, ticket, action, result, audit_refs)`

Import from `exo.kernel` to use this contract.

Kernel evolution guardrail is pinned in:
- `KERNEL_EVOLUTION_POLICY.md`

Phase A ledger primitives are written to:
- `.exo/logs/ledger.log.jsonl`

Typed records:
- `IntentSubmitted`
- `DecisionRecorded`
- `ExecutionBegun`
- `ExecutionResult`
- `Escalated`
- `Acked`

Phase B invariants now enforced in ledger:
- `ExecutionBegun` idempotency uniqueness per `(decision_id, idempotency_key)`
- `ExecutionResult` write-once per `effect_id` (identical replay returns same record)
- `ExecutionResult` requires prior `ExecutionBegun`

Phase C ledger primitives now available:
- topic head pointer via `head(topic_id)` with compare-and-swap updates via `cas_head(...)`
- topic-scoped ledger reads through `read_records(..., topic_id=..., since_cursor=...)`
- deterministic intent parent ordering via `intent_causal_order(topic_id)` with cycle detection

Phase D ledger primitives now available:
- cursor-based event subscription via `subscribe(..., since_cursor=...)`
- strict `acked(...)` references (cannot ack unknown refs)
- quorum evaluation via `ack_status(ref_id, required=N)`

Phase E privileged control primitives now available:
- `decide_override(intent_id, override_cap, rationale_ref, outcome)` writes explicit override decisions with audit + receipt chain
- `policy_set(policy_cap, policy_bundle, version)` recompiles and installs governance lock with audit + receipt chain
- both are capability-gated by `.exo/config.yaml` `control_caps`

Phase F multi-writer head controls now available:
- explicit head inspection via `head(topic_id)`
- compare-and-swap with retry semantics via `cas_head_retry(...)`
- governed `cas_head` control path (cap-gated, audited, receipted) with deterministic conflict errors (`CAS_HEAD_CONFLICT`)

Phase G optimistic submit flow now available:
- `intent_submitted(...)` enforces head compare-and-swap before commit (retryable stale-head detection)
- `mint_ticket(...)` automatically snapshots topic head and submits with CAS retries
- automatic parent linkage from prior topic head for intent causality

Phase H 12-syscall surface now available:
- `exo/control/syscalls.py` provides transport-neutral calls: `submit`, `check`, `decide_override`, `begin`, `commit`, `read`, `head`, `cas_head`, `subscribe`, `ack`, `escalate`, `policy_set`
- syscall surface is additive and does not change frozen `exo.kernel` 10-function API
- CLI and MCP control-plane handlers now route through this syscall surface for low-level operations

## Strict git controls

`exo do` now enforces lock-branch lifecycle and budgets from actual `git diff` delta when `git_controls.enabled=true`.
Defaults are in `.exo/config.yaml` under `git_controls`.
`exo audit` also reports lock-branch policy violations and stale lock-branch drift signals when a lock is active.

## Dual timeline sidecar workflow

Use `exo sidecar-init` to mount `.exo/` as a dedicated governance worktree:

```bash
python3 -m exo.cli sidecar-init --branch exo-governance --sidecar .exo
```

What this does:
- bootstraps git repo locally if missing (unless disabled),
- ensures `.exo/` is ignored by app timeline,
- creates/fetches `exo-governance`,
- mounts `.exo/` to that branch,
- migrates existing `.exo/` content into governance timeline.

### Local dev routine (no remote push)

Inspect both lanes:

```bash
git status --short --branch
git -C .exo status --short --branch
git worktree list
```

Commit app/code lane (`main`):

```bash
git add -A
git commit -m "feat: <app change>"
```

Commit governance lane (`exo-governance`):

```bash
git -C .exo add -A
git -C .exo commit -m "chore(governance): <rule/ticket/memory change>"
```

Run safety checks before ending a session:

```bash
python3 -m exo.cli audit
PYTHONPATH=. /tmp/build-exo-test-venv/bin/pytest -q
```

Lease upkeep commands (active lock only):

```bash
python3 -m exo.cli lease-renew --hours 2
python3 -m exo.cli lease-heartbeat --hours 2
python3 -m exo.cli lease-release
```

Distributed workflow (shared git remote required):

```bash
python3 -m exo.cli next --owner agent-a --distributed --remote origin
python3 -m exo.cli lease-heartbeat --owner agent-a --distributed --remote origin --hours 2
python3 -m exo.cli lease-renew --owner agent-a --distributed --remote origin --hours 2
python3 -m exo.cli lease-release --owner agent-a --distributed --remote origin
```

Distributed execution worker loop:

```bash
# bootstrap agent session context first
EXO_ACTOR=agent:worker-a python3 -m exo.cli session-start \
  --ticket-id TICKET-001 \
  --vendor openai \
  --model gpt-5 \
  --context-window 200000 \
  --task "execute queued intents for this ticket"

# one-shot poll/execute cycle
EXO_ACTOR=agent:worker-a python3 -m exo.cli worker-poll --require-session --limit 50

# continuous polling loop (stops after idle cycle)
EXO_ACTOR=agent:worker-a python3 -m exo.cli worker-loop --require-session --iterations 20 --sleep-seconds 1 --stop-when-idle

# close out + write memento
EXO_ACTOR=agent:worker-a python3 -m exo.cli session-finish \
  --summary "processed queued intents and committed results" \
  --set-status review
```

Worker behavior:
- consumes `IntentSubmitted` records from a topic stream
- reuses existing `DecisionRecorded` per intent (idempotent check)
- claims execution via deterministic idempotency key (`intent:<id>:decision:<id>`)
- commits `ExecutionResult`; completed intents are skipped on subsequent polls
- `session-start` writes actor/vendor/model/context bootstrap packet under `.exo/cache/sessions/`
- `session-finish` enforces verify gate by default, writes closeout memento under `.exo/memory/sessions/`, and clears lock unless `--set-status keep`
- `session-suspend` snapshots active session context to `.exo/memory/suspended/`, transitions ticket to `paused`, releases lock (by default), and optionally git-stashes uncommitted changes
- `session-resume` restores a suspended session, transitions ticket back to `active`, reacquires lock (by default), and optionally pops a git stash

Session suspend/resume lifecycle:

```bash
# suspend current session (releases lock, pauses ticket)
EXO_ACTOR=agent:worker-a python3 -m exo.cli session-suspend \
  --reason "Context window exhausted, handing off."

# resume suspended session (reacquires lock, reactivates ticket)
EXO_ACTOR=agent:worker-a python3 -m exo.cli session-resume

# suspend with git stash and resume with pop
EXO_ACTOR=agent:worker-a python3 -m exo.cli session-suspend \
  --reason "Rate limited" --stash
EXO_ACTOR=agent:worker-a python3 -m exo.cli session-resume --pop-stash
```

## MCP server

Install extra deps and run:

```bash
pip install -e .[mcp]
exo-mcp
```

### Example MCP configs

**Claude Desktop / Claude Code** (`~/.claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "exo": {
      "command": "exo-mcp",
      "args": [],
      "env": { "EXO_ACTOR": "agent:claude" }
    }
  }
}
```

**Cursor** (`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "exo": {
      "command": "exo-mcp",
      "args": [],
      "env": { "EXO_ACTOR": "agent:cursor" }
    }
  }
}
```

**Generic (any MCP-compatible client)**:

```json
{
  "mcpServers": {
    "exo": {
      "command": "python3",
      "args": ["-m", "exo.mcp_server"],
      "env": { "EXO_ACTOR": "agent:generic" }
    }
  }
}
```
