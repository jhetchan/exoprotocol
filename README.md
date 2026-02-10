# ExoProtocol Kernel (MVP)

Local, repo-native governance kernel with a strict ticket lock + policy compiler loop.

## Quick start

```bash
python3 -m exo.cli init --seed
python3 -m exo.cli status
python3 -m exo.cli next
python3 -m exo.cli do
```

## CLI verbs

- `exo init`
- `exo sidecar-init`
- `exo build-governance`
- `exo audit`
- `exo plan <input>`
- `exo next`
- `exo lease-renew [--ticket-id <id>] [--owner <owner>] [--role <role>] [--hours N]`
- `exo lease-heartbeat [--ticket-id <id>] [--owner <owner>] [--hours N]`
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
- `exo read-ledger [ref-id] [--type <record-type>] [--since <line:N>] [--topic <topic-id>] [--intent <intent-id>]`
- `exo escalate-intent <intent-id> --kind <kind> [--ctx-ref <ref>]`
- `exo observe --ticket <id> --tag <tag> --msg "..."`
- `exo propose --ticket <id> --kind <practice_change|governance_change|tooling_change> --symptom "..." --root-cause "..."`
- `exo approve <PROP-ID> [--decision approved|rejected]`
- `exo apply <PROP-ID>`
- `exo distill <PROP-ID>`

## JSON mode

All commands support `--format json`.

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
```

## MCP server

Install extra deps and run:

```bash
pip install -e .[mcp]
exo-mcp
```
