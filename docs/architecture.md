# Architecture

## Layer model

```
┌─────────────────────────────────────────┐
│  CLI / MCP Server                       │  User interface
├─────────────────────────────────────────┤
│  exo/orchestrator/                      │  Session lifecycle, bootstrap, closeout
├─────────────────────────────────────────┤
│  exo/stdlib/                            │  Governance subsystems
├─────────────────────────────────────────┤
│  exo/control/                           │  Transport-neutral syscall wrappers
├─────────────────────────────────────────┤
│  exo/kernel/                            │  Frozen 10-function enforcement core
└─────────────────────────────────────────┘
```

### Kernel (`exo/kernel/`)

Frozen 10-function enforcement core. Handles governance compilation, ticket/lock management, audit trails, and rule checks. Do not expand without RFC.

Public API:

- `load_governance(root)`, `verify_governance(gov)`
- `open_session(root, actor)`, `mint_ticket(session, intent, scope, ttl)`
- `validate_ticket(gov, ticket)`, `check_action(gov, session, ticket, action)`
- `resolve_requirements(decision, evidence)`, `commit_plan(session, ticket, action)`
- `append_audit(root, event)`, `seal_result(session, ticket, action, result, audit_refs)`

Evolution guardrail in `KERNEL_EVOLUTION_POLICY.md`.

### Control (`exo/control/`)

Transport-neutral control-plane wrappers (12-syscall surface). Thin layer between kernel and stdlib.

### Stdlib (`exo/stdlib/`)

Orchestration and governance subsystems:

| Module | Purpose |
|---|---|
| `engine.py` | CLI workflow engine (init, plan, do, check) |
| `adapters.py` | Agent config generation (CLAUDE.md, .cursorrules, AGENTS.md, CI) |
| `scan.py` | Brownfield repo scanner |
| `drift.py` | Composite governance drift check |
| `pr_check.py` | PR governance check |
| `conflicts.py` | Session-start scope conflict and unmerged work detection |
| `features.py` | Feature manifest and traceability linter |
| `requirements.py` | Requirement registry and traceability |
| `reconcile.py` | Intent drift detection |
| `timeline.py` | Intent timeline builder |
| `reflect.py` | Error reflection and LEARNINGS.md generation |
| `doctor.py` | Unified health check (scaffold, config, drift, scan, git tracking) |
| `config_schema.py` | Config validation |
| `upgrade.py` | Schema migration |
| `gc.py` | Garbage collection |
| `distributed_leases.py` | Git-ref-based distributed locks |
| `evolution.py` | Self-evolution protocol |
| `dispatch.py` | Lane-aware scheduling |
| `recall.py` | Memory search |
| `scratchpad.py` | Scratch notes and threads |
| `defaults.py` | Default config and constitution templates |
| `coherence.py` | Co-update rules and docstring freshness checks |
| `sidecar.py` | Dual-timeline sidecar worktree management and auto-commit |
| `tools.py` | Tool registry, search, usage tracking |
| `suggest.py` | Duplication detection, tool registration suggestions |
| `follow_up.py` | Chain reaction: auto-create follow-up tickets from governance gaps |
| `metrics.py` | Governance metrics API for dashboards |
| `traces.py` | OTel-compatible JSONL trace export |
| `hooks.py` | Claude Code lifecycle hooks, git pre-commit enforcement, PreToolUse gating, tool auto-discovery |
| `ci_fix.py` | CI failure detection, auto-fix, and push |

### Orchestrator (`exo/orchestrator/`)

Layer-3 session/worker lifecycle. Handles bootstrap prompt generation, closeout mementos, suspend/resume, audit sessions, agent handoff, and crash recovery.

### Integrations (`exo/integrations/`)

SDK-specific hooks for agent frameworks:

| Module | Purpose |
|---|---|
| `openai_agents.py` | `ExoRunHooks` for OpenAI Agents SDK (`Runner.run(agent, hooks=...)`) |

## Key concepts

### Smart init (brownfield on-ramp)

`exo init` scans the repo and generates project-aware governance:
- **Language detection**: Python, Node, Go, Rust, Java, Ruby — sets checks, budgets, and ignore paths
- **Sensitive files**: Detects .pem, .key, credentials — adds deny rules
- **Build dirs**: Detects node_modules, target, dist — adds to ignore paths
- **Source dirs**: Detects src/, lib/, app/ — customizes delete protection rules
- **Adapters**: Auto-generates CLAUDE.md, .cursorrules, AGENTS.md with governance rules
- **CI**: Detects GitHub Actions, GitLab CI, Jenkins

### Operational learnings

Accumulated knowledge from governed sessions lives in `.exo/LEARNINGS.md` — a vendor-neutral, portable file that any agent can read. Auto-generated from active reflections and failure modes. All adapter files reference it.

### Lane-aware dispatch

`exo next` dispatches the highest-priority dispatchable ticket and acquires a lock.

**Priority**: 1 = highest urgency, 5 = lowest. Scoring formula: `(6 - priority)*100 + blockers_count*30 + age_hours*0.1`. Tie-breakers: priority number, unblocks count, age, ticket ID.

**Kind filtering**: Only `task` tickets are dispatchable. Epics and intents are containers — they define scope and hierarchy but are never dispatched directly. Tickets without a `kind` field default to `task` for backward compatibility.

**Lane-aware scheduling** via `.exo/config.yaml`:

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
```

The kernel lock is single-active-ticket per `.exo/` instance. Each git worktree (via `exo sidecar-init`) has its own `.exo/` with independent locks. For cross-clone coordination, use distributed leases (`--distributed` flag).

### Audit sessions

```bash
exo session-audit --ticket-id TICKET-001 --vendor anthropic --model claude-code
```

Starts an adversarial review session with context isolation (denies .exo/cache, .exo/memory), built-in Red Team Auditor persona, and model-mismatch warnings. For PR reviews:

```bash
exo session-audit --ticket-id TICKET-001 --pr-base main --pr-head HEAD
```

Auto-runs `exo pr-check` and injects the governance report into the audit bootstrap.

### Feature manifest

`.exo/features.yaml` declares what code features can exist. Feature lifecycle: `active → experimental → deprecated → deleted`. Code is tagged with `@feature:` / `@endfeature` annotations. `exo trace` scans for violations. `exo prune` removes deleted feature code blocks.

### Requirement registry

`.exo/requirements.yaml` tracks what the system must do. Code uses `@req:` / `@implements:` annotations. `exo trace-reqs` scans for orphan references and uncovered requirements.

### Dual-timeline sidecar workflow

Use `exo sidecar-init` to mount `.exo/` as a dedicated governance worktree:

```bash
exo sidecar-init --branch exo-governance --sidecar .exo
```

This gives you parallel git histories: app code on `main`, governance state on `exo-governance`.

### Intent accountability

Tickets can be `intent`, `epic`, or `task`. Intents carry `brain_dump`, `boundary`, `success_condition`, and `risk` fields. The hierarchy is validated with `exo validate-hierarchy`. Drift detection at session-finish tracks scope compliance, boundary violations, and budget tracking.

### Session-start intelligence

When a session starts, ExoProtocol detects:
- **Scope conflicts**: warns if a sibling session's ticket overlaps your scope
- **Unmerged work**: flags relevant changes on unmerged branches
- **Ticket contention**: warns if another agent is actively working on the same ticket
- **Branch mismatch**: flags if a ticket was previously worked on a different branch

### Tool reuse protocol

Agents must search before building. `.exo/tools.yaml` is the registry of reusable tools — functions, scripts, and utilities that can be shared across sessions.

- `exo tool-register` adds a tool with module path, function name, description, and tags
- `exo tool-search` finds existing tools by keyword/tag before agents create new ones
- `exo tool-use` records usage for tracking
- `exo tool-suggest` detects duplication patterns and suggests registration
- Session bootstrap injects tool awareness: search prompts and available tool list

### Chain reaction (follow-up tickets)

Session-finish inspects governance results and auto-creates follow-up tickets for detected gaps. This creates a self-sustaining loop: session finishes → gaps detected → tickets created → next session picks them up.

Detection rules:
- **Uncovered code**: source files with no `@feature:` tags and no feature glob coverage
- **Unbound features**: features in manifest but no code tags
- **High drift**: drift score exceeds threshold (default 0.7)
- **Uncovered requirements**: requirements with no code annotations
- **Unused tools**: tools created but none of the existing tools used

Configuration in `.exo/config.yaml`:

```yaml
follow_up:
  enabled: true
  max_per_session: 5
```

Follow-up tickets are linked to the parent ticket, deduplicated by title, and capped per session. Use `exo follow-ups` for dry-run detection without creating tickets.

### Semantic coherence

`exo coherence` detects when agents change code without updating corresponding files or documentation.

Two check types:
1. **Co-update rules** — config-driven file pairs that must change together
2. **Docstring freshness** — flags functions whose body changed but docstring didn't

Configuration in `.exo/config.yaml`:

```yaml
coherence:
  enabled: true
  co_update_rules:
    - files: ["exo/cli.py", "docs/cli-reference.md"]
      label: "CLI commands changed without updating CLI reference docs"
    - files: ["exo/cli.py", "exo/mcp_server.py"]
      label: "CLI commands changed without updating MCP server (1:1 mirror)"
```

Coherence is also included as a subsystem in the composite `exo drift` check.

### Agent handoff

Governed transfer of work between agents on the same ticket:

```bash
EXO_ACTOR=agent:claude-opus exo session-handoff \
  --to agent:claude-sonnet --ticket-id TICKET-001 \
  --summary "Built API endpoints" --reason "Needs test expertise" \
  --next-step "Write integration tests"
```

Flow: Agent A finishes session → writes handoff record → releases lock → Agent B starts session → handoff context injected into bootstrap → handoff record consumed.

Handoff records are stored at `.exo/cache/sessions/handoff-{ticket_id}.json` and consumed (deleted) when the receiving agent starts its session.

### SDK integrations

ExoProtocol provides native hooks for agent frameworks:

**OpenAI Agents SDK:**

```python
from exo.integrations.openai_agents import ExoRunHooks
from agents import Runner

hooks = ExoRunHooks(repo=".", ticket_id="TKT-...", actor="agent:openai")
result = await Runner.run(agent, hooks=hooks)
```

`ExoRunHooks` wraps the session lifecycle around agent runs — `on_agent_start` creates a governed session, `on_agent_end` finishes it, `on_tool_start` logs tool invocations. All governance calls are wrapped in try/except and never crash the agent run.

**Claude Code hooks:**

```bash
pip install exoprotocol
```

Claude Code hooks are installed via `exo hook-install` and automatically start/finish governed sessions on `SessionStart`/`SessionEnd` events.

**Tool auto-discovery:**

`discover_tools()` in `exo/stdlib/hooks.py` scans `importlib.metadata` entry points under the `exoprotocol.integrations` group, plus core CLI and MCP tools. Install extras to register integrations:

```bash
pip install exoprotocol[openai-agents]
```

### Observability

**Governance metrics** (`exo metrics`): Dashboard-ready aggregate stats — verify pass rate, drift distribution (low/medium/high), ticket throughput, actor breakdown, mode counts (work vs audit).

**Fleet drift** (`exo fleet-drift`): Multi-agent drift aggregation across active, suspended, and recent finished sessions. Surfaces high-drift agents and stale sessions.

**Structured traces** (`exo export-traces`): Converts session index entries to OTel-compatible JSONL spans. Each session becomes a span with `exo.*` namespaced attributes, nanosecond timestamps, and events for drift checks and feature traces. Output at `.exo/logs/traces.jsonl` can be ingested by Jaeger, Grafana Tempo, or any OTel-compatible backend.

**Sandbox policy preview** (`exo sandbox-policy`): Derives Claude Code sandbox permissions from constitution deny rules. Maps `read` → `Read(pattern)`, `write` → `Edit(pattern)`, `delete` → `Edit(pattern)` + `Bash(rm pattern)`.

### `.exo/` git tracking

Governance files (constitution, config, lock, tickets) must be committed to git so that CI pipelines, other agents, and team members can see them. Ephemeral data (cache, logs, locks, sessions) is excluded via `.exo/.gitignore`.

`exo install` automatically commits governance files as its final step. `exo doctor` checks tracking status and fails if `.exo/` is untracked. Session-start injects a bootstrap warning when governance files are not committed.

Trackable paths: `.exo/.gitignore`, `.exo/CONSTITUTION.md`, `.exo/config.yaml`, `.exo/governance.lock.json`, `.exo/LEARNINGS.md`, `.exo/tickets/`.

### Sidecar worktree (dual-timeline)

`exo sidecar-init` mounts `.exo/` as a dedicated git worktree on an orphan `exo-governance` branch, giving app code and governance state independent git histories.

Auto-commit at lifecycle boundaries:
- **session-start**: commits bootstrap, active session file
- **session-finish**: commits memento, index row, ticket status changes
- **session-suspend**: commits suspended payload, ticket pause
- **session-resume**: commits restored session, stash pop

All commits use `ExoProtocol <exo@local.invalid>` as author, with structured messages like `chore(exo): session-finish SES-xxx [TICKET-yyy]`. Auto-commit is advisory — failures never block the lifecycle event.

Public API:
- `is_sidecar_worktree(repo)` — detect if `.exo/` is a mounted worktree
- `commit_sidecar(repo, message=...)` — commit all pending governance changes
