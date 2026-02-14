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
| `doctor.py` | Unified health check |
| `config_schema.py` | Config validation |
| `upgrade.py` | Schema migration |
| `gc.py` | Garbage collection |
| `distributed_leases.py` | Git-ref-based distributed locks |
| `evolution.py` | Self-evolution protocol |
| `dispatch.py` | Lane-aware scheduling |
| `recall.py` | Memory search |
| `scratchpad.py` | Scratch notes and threads |
| `defaults.py` | Default config and constitution templates |
| `coherence.py` | Coherence checks |
| `sidecar.py` | Dual-timeline sidecar worktree management |

### Orchestrator (`exo/orchestrator/`)

Layer-3 session/worker lifecycle. Handles bootstrap prompt generation, closeout mementos, suspend/resume, audit sessions, and crash recovery.

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
