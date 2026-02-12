# ExoProtocol Kernel (MVP)

Repo-native governance kernel for multi-agent development. Enforces ticket locks, policy compilation, session lifecycle, and audit trails — all stored in your git repo, no external services needed.

## Install

```bash
pip install -e .          # or: pipx install .
```

## 5-minute quickstart

### 1. Initialize a governed repo

```bash
exo init
```

Scans your repo to detect language, sensitive files, build directories, and CI systems. Generates a project-aware constitution, config, and agent adapter files (CLAUDE.md, .cursorrules, AGENTS.md) so governance adds value from day one.

Use `exo init --no-scan` for generic defaults, or `exo init --seed` to also create starter tickets.

### 2. Check health

```bash
exo doctor
```

Runs scaffold check, config validation, governance drift detection, and scan freshness in one pass.

### 3. Pick up a ticket

```bash
exo next --owner your-name
```

Dispatches the highest-priority todo ticket with resolved blockers, acquires a lock, and prints what to work on.

### 4. Start a governed session

```bash
EXO_ACTOR=agent:claude exo session-start \
  --ticket-id TICKET-001 \
  --vendor anthropic --model claude-code \
  --context-window 200000 \
  --task "implement the feature described in the ticket"
```

Creates a bootstrap context file with governance rules, scope constraints, and operational learnings from prior sessions.

### 5. Do the work, then finish

```bash
# ... agent does work within ticket scope ...

EXO_ACTOR=agent:claude exo session-finish \
  --summary "implemented feature, added tests" \
  --set-status review
```

Session finish runs drift detection, feature tracing, and verification. Writes a closeout memento and releases the lock.

### 6. Suspend and resume (if context runs out)

```bash
EXO_ACTOR=agent:claude exo session-suspend --reason "context window exhausted"
EXO_ACTOR=agent:claude exo session-resume
```

### 7. Recovery after crashes

```bash
exo session-scan --stale-hours 24
exo session-cleanup --stale-hours 24 --release-lock
```

Dead-PID sessions are auto-detected and flagged as stale.

## CLI verbs

### Core lifecycle

| Command | Description |
|---|---|
| `exo init [--seed] [--no-scan]` | Create .exo scaffold (scans repo by default) |
| `exo status` | Show ticket counts, active lock, dispatch candidate |
| `exo next [--owner] [--distributed]` | Dispatch next ticket and acquire lock |
| `exo do [TICKET-ID]` | Run controlled execution pipeline |
| `exo check` | Run allowlisted checks |
| `exo plan <input>` | Generate SPEC + tickets from input |

### Session lifecycle

| Command | Description |
|---|---|
| `exo session-start [--ticket-id] [--vendor] [--model] [--task]` | Start governed session with bootstrap |
| `exo session-finish --summary "..." [--set-status] [--error "tool:msg"]` | Finish session, write memento |
| `exo session-suspend --reason "..."` | Suspend session, release lock |
| `exo session-resume` | Resume suspended session |
| `exo session-audit [--ticket-id] [--pr-base] [--pr-head]` | Start audit session (adversarial review) |
| `exo session-scan [--stale-hours N]` | Scan for active/stale/orphaned sessions |
| `exo session-cleanup [--stale-hours N] [--release-lock]` | Clean up stale sessions |

### Governance and integrity

| Command | Description |
|---|---|
| `exo build-governance` | Compile constitution into governance lock |
| `exo audit` | Run integrity/rule/lock audit |
| `exo doctor [--stale-hours N]` | Unified health check (scaffold + config + drift + scan) |
| `exo config-validate` | Validate .exo/config.yaml structure and values |
| `exo drift [--skip-adapters] [--skip-features] ...` | Composite governance drift check |
| `exo pr-check [--base] [--head] [--drift-threshold]` | PR governance check (commit-to-session coverage) |
| `exo upgrade [--dry-run]` | Upgrade .exo/ to latest schema (backfill config, create dirs) |

### Intent accountability

| Command | Description |
|---|---|
| `exo intent-create --brain-dump "..." [--boundary] [--success-condition]` | Create intent ticket |
| `exo ticket-create --title "..." [--kind task\|epic] [--parent]` | Create task/epic ticket |
| `exo intents [--status] [--drift-above N]` | List intents with timeline |
| `exo validate-hierarchy <ticket-id>` | Validate intent hierarchy |

### Feature and requirement traceability

| Command | Description |
|---|---|
| `exo features [--status active\|deprecated\|...]` | List features from manifest |
| `exo trace [--glob "..."]` | Scan code for @feature tags, report violations |
| `exo prune [--include-deprecated] [--dry-run]` | Remove deleted/deprecated feature code blocks |
| `exo requirements [--status]` | List requirements from manifest |
| `exo trace-reqs [--glob "..."]` | Scan code for @req/@implements tags |

### Adapter generation

| Command | Description |
|---|---|
| `exo adapter-generate [--target claude\|cursor\|agents\|ci] [--dry-run]` | Generate agent config files from governance |
| `exo scan` | Preview what init would detect (read-only) |

### Error reflection and learning

| Command | Description |
|---|---|
| `exo reflect --pattern "..." --insight "..."` | Record operational learning |
| `exo reflections [--status] [--scope] [--severity]` | List stored reflections |
| `exo reflect-dismiss <REF-ID>` | Dismiss a reflection |

### Infrastructure

| Command | Description |
|---|---|
| `exo gc [--max-age-days N] [--dry-run]` | Garbage collect old mementos and caches |
| `exo gc-locks [--remote origin] [--dry-run] [--list]` | Clean up expired distributed leases |

### Lease management

| Command | Description |
|---|---|
| `exo lease-renew [--ticket-id] [--hours N] [--distributed]` | Renew active ticket lease |
| `exo lease-heartbeat [--ticket-id] [--hours N] [--distributed]` | Heartbeat lease without token change |
| `exo lease-release [--ticket-id] [--distributed]` | Release active lease |

### Scratchpad and recall

| Command | Description |
|---|---|
| `exo jot "..."` | Append to scratchpad inbox |
| `exo thread "topic"` | Create scratchpad thread |
| `exo promote <thread-id> --to ticket` | Promote thread to ticket |
| `exo recall "query"` | Search local memory paths |

### Self-evolution

| Command | Description |
|---|---|
| `exo observe --ticket <id> --tag <tag> --msg "..."` | Record observation |
| `exo propose --ticket <id> --kind <kind> --symptom "..."` | Propose change |
| `exo approve <PROP-ID>` | Approve proposal |
| `exo apply <PROP-ID>` | Apply approved proposal |
| `exo distill <PROP-ID>` | Distill learnings to memory |

### Ledger and control plane

| Command | Description |
|---|---|
| `exo subscribe [--topic] [--since]` | Subscribe to ledger events |
| `exo read-ledger [ref-id] [--type] [--topic]` | Read ledger records |
| `exo head --topic <id>` | Inspect topic head pointer |
| `exo cas-head --topic <id> --cap <cap>` | Compare-and-swap topic head |
| `exo submit-intent --intent "..." [--topic]` | Submit intent to ledger |
| `exo check-intent <intent-id>` | Check intent decision |
| `exo begin-effect <decision-id> --executor-ref --idem-key` | Claim execution |
| `exo commit-effect <effect-id> --status OK\|FAIL` | Commit execution result |
| `exo ack <ref-id>` | Acknowledge a ledger ref |
| `exo quorum <ref-id> [--required N]` | Check quorum status |
| `exo decide-override <intent-id> --override-cap <cap>` | Override decision (cap-gated) |
| `exo policy-set --policy-cap <cap>` | Install policy bundle (cap-gated) |

All commands support `--format json`.

## Key concepts

### Smart Init (brownfield on-ramp)

`exo init` scans the repo and generates project-aware governance:
- **Language detection**: Python, Node, Go, Rust, Java, Ruby — sets checks, budgets, and ignore paths
- **Sensitive files**: Detects .pem, .key, credentials — adds RULE-SEC-002 deny rules
- **Build dirs**: Detects node_modules, target, dist — adds to git ignore paths
- **Source dirs**: Detects src/, lib/, app/ — customizes delete protection rules
- **Adapters**: Auto-generates CLAUDE.md, .cursorrules, AGENTS.md with governance rules
- **CI**: Detects GitHub Actions, GitLab CI, Jenkins

### Operational learnings (.exo/LEARNINGS.md)

Accumulated knowledge from governed sessions lives in `.exo/LEARNINGS.md` — a vendor-neutral, portable file that any agent can read. It's auto-generated from:
- Active reflections (patterns + insights from `exo reflect`)
- Failure modes from the memory index

All adapter files (CLAUDE.md, .cursorrules, AGENTS.md) reference it. Refreshed on `exo adapter-generate` and `exo upgrade`.

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

Note: the kernel lock is single-active-ticket per `.exo/` instance. Each git worktree (via `exo sidecar-init`) has its own `.exo/` with independent locks, enabling parallel work. For cross-clone coordination, use distributed leases (`--distributed` flag).

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

## Architecture boundary

- `exo/kernel/` — Frozen 10-function enforcement core (governance, tickets/locks, audit, errors, rule checks). Do not expand without RFC.
- `exo/control/` — Transport-neutral control-plane wrappers (12-syscall surface).
- `exo/stdlib/` — Orchestration and governance subsystems:
  - `engine.py` — CLI workflow engine (init, plan, do, check, etc.)
  - `adapters.py` — Agent config generation (CLAUDE.md, .cursorrules, AGENTS.md, CI)
  - `scan.py` — Brownfield repo scanner
  - `drift.py` — Composite governance drift check
  - `pr_check.py` — PR governance check
  - `features.py` — Feature manifest and traceability linter
  - `requirements.py` — Requirement registry and traceability
  - `reconcile.py` — Intent drift detection
  - `timeline.py` — Intent timeline builder
  - `reflect.py` — Error reflection and LEARNINGS.md generation
  - `doctor.py` — Unified health check
  - `config_schema.py` — Config validation
  - `upgrade.py` — Schema migration
  - `gc.py` — Garbage collection
  - `distributed_leases.py` — Git-ref-based distributed locks
  - `evolution.py` — Self-evolution protocol
  - `dispatch.py` — Lane-aware scheduling
  - `recall.py` — Memory search
- `exo/orchestrator/` — Layer-3 session/worker lifecycle, bootstrap/closeout.

### Frozen Kernel API

Public kernel surface is intentionally fixed to 10 functions:

- `load_governance(root)`, `verify_governance(gov)`
- `open_session(root, actor)`, `mint_ticket(session, intent, scope, ttl)`
- `validate_ticket(gov, ticket)`, `check_action(gov, session, ticket, action)`
- `resolve_requirements(decision, evidence)`, `commit_plan(session, ticket, action)`
- `append_audit(root, event)`, `seal_result(session, ticket, action, result, audit_refs)`

Import from `exo.kernel` to use this contract. Evolution guardrail in `KERNEL_EVOLUTION_POLICY.md`.

## Dual timeline sidecar workflow

Use `exo sidecar-init` to mount `.exo/` as a dedicated governance worktree:

```bash
exo sidecar-init --branch exo-governance --sidecar .exo
```

This gives you parallel git histories: app code on `main`, governance state on `exo-governance`.

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
