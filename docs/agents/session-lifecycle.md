# Session Lifecycle

Complete reference for ExoProtocol session management — the enforcement layer for governed work.

## State Machine

Sessions progress through these states:

```
(none) → active → finished
(none) → active → suspended → active (resume) → finished
active → handoff → (none) [receiving agent starts new session]
active → stale (via timeout or dead PID)
```

- **none**: No session exists for the ticket
- **active**: Agent is working, lock held, bootstrap valid
- **suspended**: Work paused, lock released, snapshot saved
- **finished**: Work complete, memento written, lock released
- **handoff**: Agent A finished and wrote handoff record for Agent B
- **stale**: Active session exceeded 48h threshold or process died

## session-start

Create a new work session or resume an existing one.

### Required Parameters

- `--ticket-id`: Ticket ID (e.g., `TICKET-001`, `INTENT-042`)
- `--vendor`: AI vendor (e.g., `anthropic`, `openai`)
- `--model`: Model ID (e.g., `claude-sonnet-4-5`, `gpt-4`)

### Optional Parameters

- `--task`: One-line task summary (recorded in session payload)
- `--context-window`: Token limit for context planning (advisory)
- `--acquire-lock`: Acquire distributed lock (default: true)

### Internal Execution Flow

When `session-start` is called:

1. **Session Reuse Check**: If an active session exists for the same ticket and actor, reuse it (return existing bootstrap)
2. **Stale Session Eviction**: Scan for stale sessions (>48h or dead PID), auto-finish them with cleanup status
3. **Ticket Loading**: Load ticket from `.exo/memory/tickets/`, extract scope, budget, success condition
4. **Lock Verification**: Check if ticket lock is held (fail if `acquire_lock=true` but lock missing)
5. **Sibling Scan**: Run `scan_sessions()` to find other active agents, detect scope conflicts and unmerged work
6. **Start Advisories**: Run conflict detection (scope overlap, ticket contention, stale branch, unmerged work)
7. **Bootstrap Generation**: Compile governance rules, inject scope/budget/checks, format directives
8. **Reflection Injection**: Load active reflections for ticket scope, append "Operational Learnings" section
9. **Tool Registry Injection**: Load `.exo/tools.yaml`, append "Tool Reuse Protocol" section
10. **Branch + PID Recording**: Capture `git branch --show-current` and `os.getpid()` in session payload
11. **Session Persistence**: Write session to `.exo/cache/orchestrator/sessions/<TICKET>/<ACTOR>.session.json`
12. **Index Append**: Append session start event to `.exo/cache/orchestrator/index.jsonl`
13. **Bootstrap Write**: Write bootstrap prompt to `.exo/cache/sessions/<ACTOR>.bootstrap.md`

### CLI Usage

```bash
EXO_ACTOR=agent:claude python3 -m exo.cli session-start \
  --ticket-id TICKET-001 \
  --vendor anthropic \
  --model claude-sonnet-4-5 \
  --task "Implement user authentication"
```

### MCP Usage

```python
exo_session_start(
    repo="/path/to/repo",
    ticket_id="TICKET-001",
    vendor="anthropic",
    model="claude-sonnet-4-5",
    task="Implement user authentication"
)
```

### Return Value

Returns dict with:

- `session_id`: Unique session identifier
- `ticket_id`: Associated ticket
- `bootstrap_path`: Absolute path to `.bootstrap.md` file
- `exo_banner`: Box-drawn governance strip for agent display
- `scope`: Scope rules (allow/deny globs)
- `budget`: File and LOC limits
- `sibling_sessions`: List of other active agents
- `advisories`: Start warnings (scope conflicts, stale branch, etc.)

## Bootstrap Prompt Anatomy

The bootstrap prompt (`.exo/cache/sessions/<ACTOR>.bootstrap.md`) contains:

### 1. Exo Mode Banner

```
╔═══════════════════════════════════════════════════════════════════╗
║  ⚡ EXO GOVERNED SESSION — SESSION-001 — TICKET-001 — main       ║
╚═══════════════════════════════════════════════════════════════════╝
```

Box-drawn governance strip shown first to establish context.

### 2. Session Metadata

- Session ID, actor, ticket ID, lock holder
- Vendor, model, context window, session status
- Started timestamp

### 3. Ticket Scope

- **Allowed globs**: Files agent can read/write (e.g., `src/**`, `tests/**`)
- **Denied globs**: Files agent must never touch (e.g., `.git/**`, `.env*`)
- Budget limits: max files changed, max LOC changed

### 4. Governed Checks

Allowlisted commands from `.exo/config.yaml`:

```
Approved checks (must pass before session-finish):
- npm test
- npm run lint
- python3 -m pytest
```

### 5. Machine Context (Advisory)

- CPU count, RAM available, disk space
- Informational only — not enforced

### 6. Sibling Sessions

Other active agents working in the repository:

```
## Sibling Sessions

Active sessions from other agents:

- agent:cursor — TICKET-002 — feature/auth — 14 minutes ago
- agent:copilot — INTENT-007 — main — 3 hours ago
```

### 7. Start Advisories

Warnings detected during session start:

```
## Start Advisories

⚠ SCOPE CONFLICT (warning)
Your scope overlaps with agent:cursor on TICKET-002.
Files: src/auth/**, tests/auth/**

⚠ STALE BRANCH (info)
Your branch 'feature/login' is 12 commits behind 'main'.
Consider rebasing before making changes.
```

### 8. Prior Session Memento

If resuming work on the same ticket, the last session's summary is injected:

```
## Prior Session

Session SESSION-042 by agent:claude finished 2 hours ago.

Summary: Implemented login endpoint, wrote tests, all checks passed.
Status: review
Drift: 0.12 (low)
```

### 9. Operational Learnings

Active reflections from `.exo/memory/reflections/`:

```
## Operational Learnings

These patterns have been learned from prior sessions. Avoid repeating them.

🔴 CRITICAL — REF-003 (hit 7 times)
Pattern: Session finish blocked by pre-commit hook failure
Insight: Always run `ruff check` and `ruff format` before session-finish.
CI enforces both. Use contextlib.suppress() instead of try-except-pass (SIM105).

⚠ MEDIUM — REF-012 (hit 3 times)
Pattern: Hardcoded config values causing test failures
Insight: Never copy values from config files into source code literals.
Load from manifest at runtime. Tests must vary config input to prove wiring.
```

### 10. Tool Reuse Protocol

If `.exo/tools.yaml` exists:

```
## Tool Reuse Protocol

Before writing new utility functions, SEARCH the tool registry:
  `exo tool-search "<keywords>"`

After building a reusable utility, REGISTER it:
  `exo tool-register <module> <function> --description "..."`

Mark a tool as used when you import/call it:
  `exo tool-use <tool_id>`

Registered tools in this repository:

- lib.parsers.csv_utils:parse_csv — Parse CSV with custom dialect support
- lib.validators.email:validate_email — RFC 5322 email validation
```

### 11. Feature Governance Protocol

If `.exo/features.yaml` exists:

```
## Feature Governance Protocol

Before writing code:
1. Check which feature your work belongs to: `exo features`
2. Add `@feature:<feature-id>` / `@endfeature` tags around new code blocks

After finishing:
- Run `exo trace` to verify no uncovered code
- If you built a new subsystem, add it to `.exo/features.yaml` first
```

### 12. Current Task

The task summary from ticket or session-start:

```
## Current Task

TICKET-001: Implement user authentication

Implement login/logout endpoints with JWT tokens.
Must support email/password and OAuth2 flows.
```

### 13. Lifecycle Commands

How to finish or suspend the session:

```
## Lifecycle Commands

When done:
  exo session-finish --ticket-id TICKET-001 --summary "..." --set-status review

To pause work:
  exo session-suspend --ticket-id TICKET-001 --reason "..."

To resume later:
  exo session-resume --ticket-id TICKET-001
```

## session-finish

Complete the current work session, run checks, write memento.

### Required Parameters

- `--summary`: One-paragraph summary of work done

### Optional Parameters

- `--set-status`: Transition ticket to new status (default: `keep`)
  - `keep`: Leave ticket status unchanged
  - `review`: Mark ticket for review
  - `done`: Mark ticket complete
- `--error`: Record error encountered (format: `tool:message`)
- `--skip-check`: Skip governed checks (requires `--break-glass-reason`)
- `--break-glass-reason`: Justification for skipping checks

### Internal Execution Flow

When `session-finish` is called:

1. **Governed Checks**: Run all commands from `checks_allowlist` (unless `--skip-check` with reason)
2. **Drift Detection**: Run `detect_intent_drift()` for intent tickets (advisory, won't block)
3. **Feature Trace**: Run `trace()` if `.exo/features.yaml` exists (advisory)
4. **Coherence Check**: Run `check_coherence()` for co-update + docstring freshness (advisory)
5. **Memory Leak Scan**: Run `detect_memory_leaks()` for private memory writes (advisory)
6. **Tool Tracking**: Run `tools_session_summary()` to record tools created/used
7. **Follow-Up Detection**: Run `create_follow_ups()` for governance gaps (advisory)
8. **Branch Drift Check**: Compare start branch vs current branch
9. **Memento Write**: Write session summary to `.exo/memory/sessions/<TICKET>/<SESSION_ID>.memento.yaml`
10. **Index Append**: Append finish event to `.exo/cache/orchestrator/index.jsonl`
11. **Ticket Status Update**: If `--set-status` provided, update ticket YAML
12. **Lock Release**: Release ticket lock if held

### CLI Usage

```bash
EXO_ACTOR=agent:claude python3 -m exo.cli session-finish \
  --ticket-id TICKET-001 \
  --summary "Implemented login endpoint with JWT tokens. All tests pass." \
  --set-status review
```

### MCP Usage

```python
exo_session_finish(
    repo="/path/to/repo",
    ticket_id="TICKET-001",
    summary="Implemented login endpoint with JWT tokens. All tests pass.",
    set_status="review"
)
```

### Return Value

Returns dict with:

- `session_id`: Session that was finished
- `exo_banner`: Completion banner for display
- `memento_path`: Path to written memento
- `drift_score`: Intent drift score (if applicable)
- `trace_report`: Feature traceability results (if applicable)
- `coherence_violations`: Co-update/docstring warnings (if applicable)
- `memory_leak_warnings`: Private memory writes detected (if applicable)
- `tools_created`: Tools registered during session
- `tools_used`: Tools marked as used during session
- `follow_ups`: Follow-up tickets created (if applicable)
- `branch_drifted`: Boolean flag if branch changed during session

## session-suspend

Pause work and release the lock for another agent.

### Required Parameters

- `--reason`: Why work is being paused (recorded in snapshot)

### Internal Execution Flow

1. **Git Stash**: Run `git stash push -u` to save uncommitted changes
2. **Snapshot Write**: Write session state to `.exo/cache/orchestrator/sessions/<TICKET>/<ACTOR>.snapshot.json`
3. **Status Update**: Change session status from `active` to `suspended`
4. **Lock Release**: Release ticket lock
5. **Index Append**: Record suspend event in index.jsonl

### CLI Usage

```bash
EXO_ACTOR=agent:claude python3 -m exo.cli session-suspend \
  --ticket-id TICKET-001 \
  --reason "Blocked on code review"
```

### MCP Usage

```python
exo_session_suspend(
    repo="/path/to/repo",
    ticket_id="TICKET-001",
    reason="Blocked on code review"
)
```

## session-resume

Continue suspended work, reacquire lock, restore changes.

### Optional Parameters

- `--acquire-lock`: Reacquire ticket lock (default: true)

### Internal Execution Flow

1. **Load Snapshot**: Read suspended session state
2. **Lock Acquisition**: Acquire ticket lock (if `--acquire-lock=true`)
3. **Git Stash Pop**: Restore uncommitted changes with `git stash pop`
4. **Fresh Bootstrap**: Generate new bootstrap prompt (same flow as session-start)
5. **Status Update**: Change session status from `suspended` to `active`
6. **Index Append**: Record resume event in index.jsonl

### CLI Usage

```bash
EXO_ACTOR=agent:claude python3 -m exo.cli session-resume \
  --ticket-id TICKET-001
```

### MCP Usage

```python
exo_session_resume(
    repo="/path/to/repo",
    ticket_id="TICKET-001"
)
```

## Audit Sessions

Adversarial review sessions with context isolation.

### Purpose

- **Lazy Auditor Defense**: Dedicated audit persona reviews governed work
- **Context Isolation**: No access to `.exo/cache/**`, `.exo/memory/**`
- **Writing Session Lookup**: Auto-detects last writing session for model comparison
- **PR Governance**: Optional PR check integration for commit-to-session traceability

### CLI Usage

```bash
EXO_ACTOR=agent:claude python3 -m exo.cli session-audit \
  --ticket-id TICKET-001 \
  --vendor anthropic \
  --model claude-opus-4-6 \
  --pr-base main \
  --pr-head HEAD
```

### MCP Usage

```python
exo_session_audit(
    repo="/path/to/repo",
    ticket_id="TICKET-001",
    vendor="anthropic",
    model="claude-opus-4-6",
    pr_base="main",
    pr_head="HEAD"
)
```

### Bootstrap Differences

Audit mode bootstrap includes:

1. **Audit Banner**: `AUDIT SESSION` instead of `GOVERNED SESSION`
2. **Adversarial Persona**: Red Team Auditor directives from `.exo/audit_persona.md` or built-in defaults
3. **Writing Session Context**: Last non-audit session details (vendor, model, summary, drift)
4. **PR Governance Report**: If `--pr-base` and `--pr-head` provided:
   - Verdict (pass/warn/fail)
   - Ungoverned commits
   - Scope violations
   - Drift scores
   - Review directives

**Skipped sections**: Operational Learnings, Tool Reuse Protocol (auditor should not learn from or build tools)

### Finish Warnings

Advisory warnings in audit session finish:

- **No artifacts produced**: Audit session made file changes (suspicious)
- **Same model as writer**: Audit using same model as writing session (weak independence)

These are advisory only — never block session-finish.

## session-handoff

Governed transfer of work from one agent to another on the same ticket.

### Required Parameters

- `--to`: Target actor identifier (e.g., `agent:claude-sonnet`)
- `--ticket-id`: Ticket being handed off
- `--summary`: Summary of work done by the handing-off agent

### Optional Parameters

- `--reason`: Why the handoff is needed
- `--next-step`: What the receiving agent should do
- `--keep-lock`: Don't release the ticket lock (default: release)

### Internal Execution Flow

1. **Verify Active Session**: Check that the current agent has an active session on the ticket
2. **Finish Session**: Close the current session with memento (uses `break_glass_reason="handoff"`)
3. **Write Handoff Record**: Create `.exo/cache/sessions/handoff-{ticket_id}.json` with from_actor, to_actor, summary, reason, next_steps, scope, branch
4. **Release Lock**: Release ticket lock (unless `--keep-lock`)

When the receiving agent calls `session-start` on the same ticket:

1. **Detect Handoff**: Load handoff record if present
2. **Inject Context**: Add "Handoff Context" section to bootstrap with summary, reason, next steps, and source branch
3. **Consume Record**: Delete handoff file (one-shot — prevents stale context)

### CLI Usage

```bash
# Agent A hands off
EXO_ACTOR=agent:claude-opus exo session-handoff \
  --to agent:claude-sonnet \
  --ticket-id TICKET-001 \
  --summary "Built API endpoints, tests not yet written" \
  --reason "Needs testing expertise" \
  --next-step "Write integration tests for /api/users"

# Agent B picks up — handoff context auto-injected
EXO_ACTOR=agent:claude-sonnet exo session-start \
  --ticket-id TICKET-001 --vendor anthropic --model claude-sonnet-4-5
```

### MCP Usage

```python
exo_session_handoff(
    to_actor="agent:claude-sonnet",
    ticket_id="TICKET-001",
    summary="Built API endpoints, tests not yet written",
    reason="Needs testing expertise",
    next_steps="Write integration tests for /api/users"
)
```

### Design Decisions

| Decision | Rationale |
|----------|-----------|
| Handoff = finish + record (not suspend) | Clean memento, no dangling suspended state |
| Record consumed on start | One-shot — prevents stale context |
| Lock released by default | Receiving agent acquires fresh lock |
| `to_actor` is advisory | Any agent can pick up if needed |

## Crash Recovery

Detect and clean up orphaned sessions from agent crashes.

### session-scan

List all sessions, highlight stale/dead ones.

```bash
EXO_ACTOR=agent:claude python3 -m exo.cli session-scan
```

Returns:

- `active_sessions`: Sessions with status=active
- `stale_sessions`: Sessions >48h old or with dead PID
- `suspended_sessions`: Sessions with status=suspended

Each session includes:

- `session_id`, `actor`, `ticket_id`, `status`
- `started_at`, `age_hours`
- `pid`, `pid_alive` (process liveness)
- `git_branch`

### session-cleanup

Auto-finish stale sessions.

```bash
EXO_ACTOR=agent:claude python3 -m exo.cli session-cleanup
```

For each stale session:

1. Write emergency memento with `status: cleanup`
2. Release lock
3. Update session status to `finished`
4. Append cleanup event to index.jsonl

### Dead PID Detection

Sessions track `os.getpid()` at start. `scan_sessions()` checks process liveness with `os.kill(pid, 0)`.

If PID check fails → session flagged as stale → auto-evicted on next `session-start`.

## Non-Negotiables

Rules for all agents operating in governed sessions:

- **No work without session-start**: Always call `session-start` before making changes
- **Bootstrap is source of truth**: Read `.bootstrap.md` file path from session-start response
- **Respect scope**: Only modify files matching `scope.allow` globs, never touch `scope.deny` paths
- **Run checks before finish**: All `checks_allowlist` commands must pass (or use `--break-glass-reason`)
- **Always call session-finish**: Never leave work without writing a memento
- **Ticket status transitions**: Use `--set-status` to signal review/completion
- **Lock discipline**: If lock held, you own the ticket; if lock released, stop working
- **Sibling awareness**: Check `sibling_sessions` in bootstrap to avoid conflicts
- **Heed advisories**: Start warnings are there for a reason (scope conflicts, stale branches)
- **Test-driven config**: Never hardcode values from config files — load at runtime, test the wiring
- **Search before build**: Check tool registry (`exo tool-search`) before writing new utilities
- **Feature tagging**: Mark code with `@feature:` tags if `.exo/features.yaml` exists
- **Reflect learnings**: Use `exo reflect` for operational insights — not private memory files
- **Error reporting**: If blocked, use `--error` param on session-finish to record the issue
- **Branch stability**: If branch drifts during work, document why in summary
- **No skip-check abuse**: `--skip-check` requires justification — only use for emergencies

## Related Commands

- `exo intents` — List all intent tickets, check drift scores
- `exo drift` — Run composite governance health check
- `exo trace` — Feature traceability lint
- `exo coherence` — Co-update + docstring freshness check
- `exo tools` — List registered tools
- `exo reflections` — List operational learnings
- `exo follow-ups` — Preview governance gap tickets
- `exo gc` — Clean up old mementos/bootstraps/cursors
