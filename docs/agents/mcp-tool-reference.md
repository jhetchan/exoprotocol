# MCP Tool Reference

Signatures and parameters for all 68 ExoProtocol MCP tools.

## Conventions

All tools accept `repo: str = "."` as the first or second parameter (repository root path).

**Return format on success:**
```json
{
  "ok": true,
  "data": {...},
  "events": [],
  "blocked": false
}
```

**Return format on error:**
```json
{
  "ok": false,
  "error": {
    "code": "ERROR_CODE",
    "message": "Human-readable error message",
    "details": {...},
    "blocked": true
  },
  "events": [],
  "blocked": true
}
```

---

## Core lifecycle

### exo_status
```python
exo_status(repo: str = ".") -> dict[str, Any]
```
Show ticket counts, active lock status, and dispatch candidate for the next work item.

### exo_plan
```python
exo_plan(input: str, repo: str = ".") -> dict[str, Any]
```
Break down a natural language task into work tickets (intent/epic/task).
- `input`: Natural language description of work to decompose

### exo_next
```python
exo_next(repo: str = ".", owner: str = "agent:mcp", role: str = "developer") -> dict[str, Any]
```
Get the next dispatch candidate ticket for a given owner and role.
- `owner`: Actor identifier (e.g., "agent:mcp", "human:alice")
- `role`: Worker role for lane-aware dispatch (e.g., "developer", "reviewer")

### exo_do
```python
exo_do(ticket_id: str | None = None, repo: str = ".") -> dict[str, Any]
```
Mark a ticket as in-progress. If no ticket_id provided, uses dispatch candidate.

### exo_check
```python
exo_check(ticket_id: str | None = None, repo: str = ".") -> dict[str, Any]
```
Run governance checks for a ticket. If no ticket_id provided, uses active ticket.

---

## Session lifecycle

### exo_session_start
```python
exo_session_start(
    repo: str = ".",
    ticket_id: str | None = None,
    vendor: str = "unknown",
    model: str = "unknown",
    context_window_tokens: int | None = None,
    role: str | None = None,
    task: str | None = None,
    topic_id: str | None = None,
    acquire_lock: bool = False,
    distributed: bool = False,
    remote: str = "origin",
    duration_hours: int = 2,
) -> dict[str, Any]
```
Start an agent session with bootstrap prompt generation.
- `ticket_id`: Ticket to work on (optional if using dispatch)
- `vendor`: Model vendor (e.g., "anthropic", "openai")
- `model`: Model name (e.g., "claude-opus-4-6", "gpt-4")
- `context_window_tokens`: Model context limit for bootstrap sizing
- `role`: Worker role (e.g., "developer", "reviewer")
- `task`: Custom task description (overrides ticket intent)
- `topic_id`: Topic for ledger operations (defaults to repo:default)
- `acquire_lock`: Whether to acquire distributed lock
- `distributed`: Whether to use git-based distributed locking
- `remote`: Git remote for distributed locks
- `duration_hours`: Lock lease duration

Returns: `bootstrap_path`, `bootstrap_prompt`, `session_id`, `ticket_id`, `exo_banner`

### exo_session_audit
```python
exo_session_audit(
    ticket_id: str,
    repo: str = ".",
    vendor: str = "unknown",
    model: str = "unknown",
    context_window_tokens: int | None = None,
    role: str = "auditor",
    task: str | None = None,
    topic_id: str | None = None,
    acquire_lock: bool = False,
    distributed: bool = False,
    remote: str = "origin",
    duration_hours: int = 2,
    pr_base: str | None = None,
    pr_head: str | None = None,
) -> dict[str, Any]
```
Start an isolated audit session with context isolation, adversarial persona, and model-mismatch detection. When pr_base and pr_head are provided, automatically runs pr-check and injects the governance report into the audit bootstrap prompt for PR review.
- `ticket_id`: Required (ticket to audit)
- `pr_base`: Git ref for PR base (e.g., "main")
- `pr_head`: Git ref for PR head (e.g., "HEAD")

### exo_session_suspend
```python
exo_session_suspend(
    reason: str,
    repo: str = ".",
    ticket_id: str | None = None,
    release_lock: bool = True,
    stash_changes: bool = False,
) -> dict[str, Any]
```
Suspend a running session with snapshot.
- `reason`: Why the session is being suspended
- `release_lock`: Whether to release distributed lock
- `stash_changes`: Whether to stash uncommitted git changes

### exo_session_resume
```python
exo_session_resume(
    repo: str = ".",
    ticket_id: str | None = None,
    acquire_lock: bool = True,
    pop_stash: bool = False,
    distributed: bool = False,
    remote: str = "origin",
    duration_hours: int = 2,
    role: str | None = None,
) -> dict[str, Any]
```
Resume a suspended session and restore snapshot.
- `acquire_lock`: Whether to reacquire distributed lock
- `pop_stash`: Whether to pop git stash from suspension

### exo_session_finish
```python
exo_session_finish(
    summary: str,
    repo: str = ".",
    ticket_id: str | None = None,
    set_status: str = "review",
    artifacts: list[str] | None = None,
    blockers: list[str] | None = None,
    next_step: str | None = None,
    skip_check: bool = False,
    break_glass_reason: str | None = None,
    release_lock: bool | None = None,
    drift_threshold: float | None = None,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]
```
Finish a session and write memento.
- `summary`: Work summary for memento
- `set_status`: Ticket status to set (e.g., "review", "done", "blocked")
- `artifacts`: List of file paths created/modified
- `blockers`: List of blocker descriptions
- `next_step`: Recommended next action
- `skip_check`: Skip governance checks (requires break_glass_reason)
- `break_glass_reason`: Justification for skipping checks
- `drift_threshold`: Custom drift threshold (default from config)
- `errors`: List of error dicts with "tool" and "message" keys for reflection

### exo_session_handoff
```python
exo_session_handoff(
    to_actor: str,
    ticket_id: str,
    summary: str,
    repo: str = ".",
    reason: str = "",
    next_steps: str = "",
    release_lock: bool = True,
) -> dict[str, Any]
```
Hand off active session to another agent with context transfer. Finishes the current session, writes a handoff record, and (by default) releases the lock so the target agent can pick up.
- `to_actor`: Target actor identifier (e.g., "agent:claude-sonnet")
- `ticket_id`: Ticket being handed off
- `summary`: Summary of work done
- `reason`: Why the handoff is needed
- `next_steps`: What the receiving agent should do next
- `release_lock`: Whether to release ticket lock (default: true)

Returns: `from_session_id`, `to_actor`, `handoff_path`, `released_lock`

### exo_session_scan
```python
exo_session_scan(repo: str = ".", stale_hours: float = 48.0) -> dict[str, Any]
```
Scan for active and stale sessions.
- `stale_hours`: Hours before session is considered stale

Returns: `active_sessions`, `stale_sessions`, `orphan_sessions`

### exo_session_cleanup
```python
exo_session_cleanup(
    repo: str = ".",
    stale_hours: float = 48.0,
    force: bool = False,
    release_lock: bool = False,
) -> dict[str, Any]
```
Clean up stale sessions and optionally release locks.
- `force`: Force cleanup even if session appears live
- `release_lock`: Release distributed locks for cleaned sessions

---

## Governance and integrity

### exo_drift
```python
exo_drift(
    repo: str = ".",
    stale_hours: float = 48.0,
    skip_adapters: bool = False,
    skip_features: bool = False,
    skip_requirements: bool = False,
    skip_sessions: bool = False,
    skip_coherence: bool = False,
) -> dict[str, Any]
```
Run composite governance drift check across all subsystems. Checks governance integrity, adapter freshness, feature traceability, requirement traceability, session health, and coherence. Returns unified pass/fail verdict with per-subsystem details.

### exo_coherence
```python
exo_coherence(
    repo: str = ".",
    base: str = "main",
    skip_co_updates: bool = False,
    skip_docstrings: bool = False,
) -> dict[str, Any]
```
Check semantic coherence: co-update rules and docstring freshness. Detects when code changes without corresponding documentation updates. Co-update rules enforce that related files change together. Docstring freshness flags functions whose body changed but docstring didn't.

### exo_pr_check
```python
exo_pr_check(
    repo: str = ".",
    base_ref: str = "main",
    head_ref: str = "HEAD",
    drift_threshold: float = 0.7,
) -> dict[str, Any]
```
Check governance compliance for all commits in a PR range. Matches commits to governed sessions by timestamp, checks scope coverage, drift scores, and governance integrity. Returns structured verdict: pass / warn / fail.

### exo_doctor
```python
exo_doctor(repo: str = ".", stale_hours: float = 48.0) -> dict[str, Any]
```
Run unified governance health check: scaffold, config validation, drift, and scan freshness.

### exo_config_validate
```python
exo_config_validate(repo: str = ".") -> dict[str, Any]
```
Validate .exo/config.yaml structure, types, and value ranges.

### exo_upgrade
```python
exo_upgrade(repo: str = ".", dry_run: bool = False) -> dict[str, Any]
```
Upgrade .exo/ directory to latest schema version. Backfills missing config keys, creates missing dirs, recompiles governance, regenerates adapters.

### exo_adapter_generate
```python
exo_adapter_generate(
    repo: str = ".",
    targets: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]
```
Generate repo-root agent config files (CLAUDE.md, .cursorrules, AGENTS.md) from governance state.
- `targets`: List of adapter targets (e.g., ["claude", "cursor", "agents", "ci"])

### exo_scan
```python
exo_scan(repo: str = ".") -> dict[str, Any]
```
Scan repository and detect languages, sensitive files, build dirs, CI systems, and existing governance. Read-only preview of what 'exo init' would customize.

---

## Intent accountability

### exo_intent_create
```python
exo_intent_create(
    title: str,
    brain_dump: str,
    repo: str = ".",
    boundary: str = "",
    success_condition: str = "",
    risk: str = "medium",
    scope_allow: list[str] | None = None,
    scope_deny: list[str] | None = None,
    max_files: int = 12,
    max_loc: int = 400,
    priority: int = 3,
    labels: list[str] | None = None,
) -> dict[str, Any]
```
Create a new intent ticket (root of intent hierarchy).
- `title`: Short intent description
- `brain_dump`: Detailed intent reasoning and context
- `boundary`: What is out of scope
- `success_condition`: How to verify success
- `risk`: Risk level (low/medium/high/critical)
- `scope_allow`: File glob patterns allowed for modification
- `scope_deny`: File glob patterns denied for modification
- `max_files`: Budget for files changed
- `max_loc`: Budget for lines of code changed

### exo_ticket_create
```python
exo_ticket_create(
    title: str,
    parent_id: str,
    repo: str = ".",
    kind: str = "task",
    scope_allow: list[str] | None = None,
    scope_deny: list[str] | None = None,
    max_files: int = 12,
    max_loc: int = 400,
    priority: int = 3,
    labels: list[str] | None = None,
    boundary: str = "",
    success_condition: str = "",
) -> dict[str, Any]
```
Create a new epic or task ticket under a parent intent/epic.
- `kind`: Ticket kind ("task" or "epic")
- `parent_id`: Parent intent or epic ID
- Task parent must be intent or epic
- Epic parent must be intent

### exo_intents
```python
exo_intents(
    repo: str = ".",
    status: str | None = None,
    drift_above: float | None = None,
) -> dict[str, Any]
```
List intents with timeline and drift metrics.
- `status`: Filter by ticket status (e.g., "open", "review", "done")
- `drift_above`: Filter to intents with drift score above threshold

### exo_validate_hierarchy
```python
exo_validate_hierarchy(ticket_id: str, repo: str = ".") -> dict[str, Any]
```
Validate intent hierarchy rules for a ticket.

---

## Feature and requirement traceability

### exo_features
```python
exo_features(
    repo: str = ".",
    status: str | None = None,
) -> dict[str, Any]
```
List feature definitions from .exo/features.yaml with optional status filter.
- `status`: Filter by feature status ("active", "experimental", "deprecated", "deleted")

### exo_trace
```python
exo_trace(
    repo: str = ".",
    globs: list[str] | None = None,
    check_unbound: bool = True,
) -> dict[str, Any]
```
Run feature traceability linter: cross-reference @feature: tags against manifest. Checks for invalid tags, deprecated/deleted usage, locked edits, and unbound features. Returns structured report with pass/fail verdict.
- `globs`: File glob patterns to scan (default: all source files)
- `check_unbound`: Whether to flag features without any code coverage

### exo_prune
```python
exo_prune(
    repo: str = ".",
    include_deprecated: bool = False,
    globs: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]
```
Remove code blocks tagged with deleted (or deprecated) features. Scans for @feature: / @endfeature blocks referencing features with 'deleted' status and removes them. Use include_deprecated=True to also remove deprecated feature code. Use dry_run=True to preview.

### exo_requirements
```python
exo_requirements(
    repo: str = ".",
    status: str | None = None,
) -> dict[str, Any]
```
List requirement definitions from .exo/requirements.yaml with optional status filter.
- `status`: Filter by requirement status ("active", "deprecated", "deleted")

### exo_trace_reqs
```python
exo_trace_reqs(
    repo: str = ".",
    globs: list[str] | None = None,
    check_uncovered: bool = True,
) -> dict[str, Any]
```
Run requirement traceability linter: cross-reference @req: annotations against manifest. Checks for orphan references, deprecated/deleted usage, and uncovered requirements. Returns structured report with pass/fail verdict.

---

## Tool awareness

### exo_tools
```python
exo_tools(
    repo: str = ".",
    tag: str | None = None,
) -> dict[str, Any]
```
List registered tools from .exo/tools.yaml with optional tag filter.

### exo_tool_register
```python
exo_tool_register(
    module: str,
    function: str,
    description: str,
    repo: str = ".",
    signature: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any]
```
Register a reusable tool in the .exo/tools.yaml registry.
- `module`: Python module path (e.g., "lib/parsers/csv_utils.py")
- `function`: Function name (e.g., "parse_csv")
- `description`: Human-readable description of what the tool does
- `signature`: Function signature (optional)
- `tags`: Categorization tags

### exo_tool_search
```python
exo_tool_search(
    query: str,
    repo: str = ".",
) -> dict[str, Any]
```
Search registered tools by keyword matching on description, tags, module, function.

### exo_tool_remove
```python
exo_tool_remove(
    tool_id: str,
    repo: str = ".",
) -> dict[str, Any]
```
Remove a tool from the .exo/tools.yaml registry.
- `tool_id`: Tool identifier (e.g., "lib.parsers.csv_utils:parse_csv")

### exo_tool_use
```python
exo_tool_use(
    tool_id: str,
    repo: str = ".",
    session_id: str = "",
) -> dict[str, Any]
```
Record that a tool was used in a session. Updates usage tracking.

### exo_tool_suggest
```python
exo_tool_suggest(repo: str = ".") -> dict[str, Any]
```
Detect duplication patterns across sessions and suggest tool registration. Finds underused tools, sessions without tool awareness, and summaries mentioning utility patterns without registration.

---

## Chain reaction

### exo_follow_ups
```python
exo_follow_ups(repo: str = ".", ticket_id: str = "") -> dict[str, Any]
```
Detect governance gaps that warrant follow-up tickets. Analyzes feature trace, requirement trace, drift scores, and tool usage to find gaps. Dry-run mode — does not create tickets.

---

## Error reflection

### exo_reflect
```python
exo_reflect(
    pattern: str,
    insight: str,
    repo: str = ".",
    severity: str = "medium",
    scope: str = "global",
    tags: list[str] | None = None,
    session_id: str = "",
) -> dict[str, Any]
```
Record an operational learning from session experience. Agents call this when they encounter repeated failures or discover an insight worth persisting. The reflection is stored and automatically injected into future session bootstraps as 'Operational Learnings'.
- `pattern`: What keeps happening (trigger condition)
- `insight`: What was learned (solution/workaround)
- `severity`: Severity level (low/medium/high/critical)
- `scope`: Reflection scope (global or ticket-specific)
- `tags`: Categorization tags

### exo_reflections
```python
exo_reflections(
    repo: str = ".",
    status: str | None = None,
    scope: str | None = None,
    severity: str | None = None,
) -> dict[str, Any]
```
List stored reflections with optional filters.
- `status`: Filter by status ("active", "superseded", "dismissed")
- `scope`: Filter by scope ("global", "ticket")
- `severity`: Filter by severity ("low", "medium", "high", "critical")

### exo_reflect_dismiss
```python
exo_reflect_dismiss(
    reflection_id: str,
    repo: str = ".",
) -> dict[str, Any]
```
Dismiss a reflection so it stops appearing in future bootstraps.

---

## Infrastructure

### exo_gc
```python
exo_gc(
    repo: str = ".",
    max_age_days: float = 30.0,
    dry_run: bool = False,
) -> dict[str, Any]
```
Garbage-collect old mementos, cursors, and bootstraps. Scans .exo/memory/sessions/ for old memento files, .exo/cache/orchestrator/ for orphaned cursors, and .exo/cache/sessions/ for leftover bootstraps. Also compacts the session index JSONL.

### exo_gc_locks
```python
exo_gc_locks(
    repo: str = ".",
    remote: str = "origin",
    dry_run: bool = False,
    list_only: bool = False,
) -> dict[str, Any]
```
Clean up expired distributed locks on a git remote. Scans refs/exoprotocol/locks/* on the remote, identifies expired leases, and deletes their refs. Use list_only=True to inspect without cleaning, or dry_run=True to preview cleanup.

---

## Observability

### exo_metrics
```python
exo_metrics(repo: str = ".") -> dict[str, Any]
```
Compute governance metrics for dashboards. Returns aggregate statistics from session history including verification rates, drift distribution, ticket throughput, and actor breakdown.

Returns: `session_count`, `verify_pass_rate`, `verify_passed`, `verify_failed`, `verify_bypassed`, `avg_drift_score`, `max_drift_score`, `drift_distribution`, `tickets_touched`, `actor_count`, `actors`, `mode_counts`, `computed_at`

### exo_fleet_drift
```python
exo_fleet_drift(
    repo: str = ".",
    stale_hours: float = 48.0,
    include_finished: int = 10,
) -> dict[str, Any]
```
Aggregate drift across active, suspended, and recent finished sessions. Provides a fleet-level view of governance drift for multi-agent teams.
- `stale_hours`: Threshold for stale session detection
- `include_finished`: Number of recent finished sessions to include

Returns: `agents` (per-agent records), `agent_count`, `active_count`, `suspended_count`, `finished_count`, `stale_count`, `avg_drift`, `max_drift`, `high_drift_count`

### exo_export_traces
```python
exo_export_traces(
    repo: str = ".",
    since: str = "",
    write: bool = True,
) -> dict[str, Any]
```
Export governance events as OTel-compatible JSONL traces. Reads session index and converts each session into an OpenTelemetry span with attributes, events, and status.
- `since`: Only export sessions started after this ISO timestamp
- `write`: Whether to write output to `.exo/logs/traces.jsonl` (default: true)

Returns: `span_count`, `spans`, `output_path`, `since`

---

## Scratchpad and recall

### exo_jot
```python
exo_jot(content: str, repo: str = ".") -> dict[str, Any]
```
Append a line to the scratchpad (quick note-taking).

### exo_thread
```python
exo_thread(topic: str, repo: str = ".") -> dict[str, Any]
```
Read scratchpad entries for a topic.

### exo_promote
```python
exo_promote(thread_id: str, repo: str = ".") -> dict[str, Any]
```
Promote a scratchpad thread to a ticket.

### exo_recall
```python
exo_recall(query: str, repo: str = ".") -> dict[str, Any]
```
Search memory index by query string.

---

## Self-evolution

### exo_observe
```python
exo_observe(
    ticket_id: str,
    tag: str,
    msg: str,
    repo: str = ".",
    triggers: list[str] | None = None,
    confidence: str = "high",
) -> dict[str, Any]
```
Record a practice observation during evolution sessions.
- `tag`: Observation category (e.g., "failure_mode", "design_pattern")
- `msg`: Observation message
- `triggers`: List of trigger conditions
- `confidence`: Confidence level (high/medium/low)

### exo_propose
```python
exo_propose(
    ticket_id: str,
    kind: str,
    symptom: str | list[str],
    root_cause: str,
    repo: str = ".",
    summary: str | None = None,
    expected_effect: list[str] | str | None = None,
    risk_level: str = "medium",
    blast_radius: list[str] | None = None,
    rollback_type: str = "delete_file",
    rollback_path: str | None = None,
    proposed_change_type: str = "patch_file",
    proposed_change_path: str | None = None,
    evidence_observations: list[str] | None = None,
    evidence_audit_ranges: list[str] | None = None,
    notes: list[str] | None = None,
    requires_approvals: int = 1,
    human_required: bool | None = None,
    patch_file: str | None = None,
) -> dict[str, Any]
```
Propose a governance or practice evolution change.
- `kind`: Proposal kind (e.g., "governance_patch", "practice_update")
- `symptom`: Observed symptoms triggering the proposal
- `root_cause`: Root cause analysis
- `expected_effect`: Expected outcomes
- `risk_level`: Risk assessment (low/medium/high/critical)
- `blast_radius`: Affected subsystems
- `rollback_type`: How to rollback (e.g., "delete_file", "git_revert")
- `proposed_change_type`: Type of change (e.g., "patch_file", "new_file")
- `requires_approvals`: Number of approvals needed
- `human_required`: Whether human approval is mandatory

### exo_approve
```python
exo_approve(
    proposal_id: str,
    repo: str = ".",
    decision: str = "approved",
    note: str = "",
) -> dict[str, Any]
```
Approve or reject an evolution proposal.
- `decision`: Decision ("approved", "rejected", "deferred")
- `note`: Approval note

### exo_apply
```python
exo_apply(proposal_id: str, repo: str = ".") -> dict[str, Any]
```
Apply an approved evolution proposal.

### exo_distill
```python
exo_distill(
    proposal_id: str,
    repo: str = ".",
    statement: str | None = None,
    confidence: float = 0.7,
) -> dict[str, Any]
```
Distill an evolution proposal into a practice statement.
- `statement`: Practice statement to distill
- `confidence`: Confidence score (0.0 - 1.0)

---

## Ledger and control plane

### exo_submit
```python
exo_submit(
    intent: str,
    repo: str = ".",
    topic_id: str | None = None,
    intent_id: str | None = None,
    ttl_hours: int = 1,
    action_kind: str = "read_file",
    target: str | None = None,
    scope_allow: list[str] | None = None,
    scope_deny: list[str] | None = None,
    expected_ref: str | None = None,
    max_attempts: int = 1,
) -> dict[str, Any]
```
Submit an intent to the control ledger.
- `intent`: Human-readable intent description
- `topic_id`: Topic identifier (defaults to repo:default)
- `intent_id`: Custom intent ID (auto-generated if omitted)
- `ttl_hours`: Time-to-live for the intent
- `action_kind`: Action type (e.g., "read_file", "write_file", "shell_exec")
- `target`: Action target (e.g., file path for read_file)
- `scope_allow`: File glob patterns allowed
- `scope_deny`: File glob patterns denied
- `expected_ref`: Expected ledger head ref for CAS
- `max_attempts`: Max retry attempts for CAS conflicts

### exo_check_intent
```python
exo_check_intent(
    intent_id: str,
    repo: str = ".",
    context_refs: list[str] | None = None,
) -> dict[str, Any]
```
Check if an intent is allowed by governance policy.
- `context_refs`: Additional ledger references for context

### exo_begin
```python
exo_begin(
    decision_id: str,
    executor_ref: str,
    idem_key: str,
    repo: str = ".",
) -> dict[str, Any]
```
Begin execution of an approved intent.
- `executor_ref`: Reference to executor record
- `idem_key`: Idempotency key for deduplication

### exo_commit
```python
exo_commit(
    effect_id: str,
    status: str,
    repo: str = ".",
    artifact_refs: list[str] | None = None,
) -> dict[str, Any]
```
Commit an effect record (success/failure of execution).
- `status`: Execution status (e.g., "success", "failure", "timeout")
- `artifact_refs`: References to artifacts produced

### exo_read
```python
exo_read(
    repo: str = ".",
    ref_id: str | None = None,
    type_filter: str | None = None,
    since_cursor: str | None = None,
    limit: int = 200,
    topic_id: str | None = None,
    intent_id: str | None = None,
) -> dict[str, Any]
```
Read records from the control ledger.
- `ref_id`: Specific record ID to read
- `type_filter`: Filter by record type (e.g., "intent", "decision", "effect")
- `since_cursor`: Cursor for pagination
- `limit`: Max records to return
- `topic_id`: Filter by topic
- `intent_id`: Filter by intent

### exo_subscribe
```python
exo_subscribe(
    repo: str = ".",
    topic_id: str | None = None,
    since_cursor: str | None = None,
    limit: int = 100,
) -> dict[str, Any]
```
Subscribe to ledger events for a topic.

### exo_ack
```python
exo_ack(
    ref_id: str,
    repo: str = ".",
    required: int = 1,
    actor_cap: str = "cap:ack",
) -> dict[str, Any]
```
Acknowledge a ledger record.
- `required`: Required number of acks for quorum
- `actor_cap`: Capability token for ack authority

### exo_quorum
```python
exo_quorum(
    ref_id: str,
    repo: str = ".",
    required: int = 1,
) -> dict[str, Any]
```
Check if a ledger record has reached quorum.

### exo_head
```python
exo_head(topic_id: str, repo: str = ".") -> dict[str, Any]
```
Get the current head ref for a topic.

### exo_cas_head
```python
exo_cas_head(
    topic_id: str,
    control_cap: str,
    repo: str = ".",
    expected_ref: str | None = None,
    new_ref: str | None = None,
    max_attempts: int = 1,
) -> dict[str, Any]
```
Compare-and-swap the head ref for a topic.
- `control_cap`: Capability token for CAS authority
- `expected_ref`: Expected current head
- `new_ref`: New head to set
- `max_attempts`: Max CAS retry attempts

### exo_decide_override
```python
exo_decide_override(
    intent_id: str,
    override_cap: str,
    rationale_ref: str,
    repo: str = ".",
    outcome: str = "ALLOW",
) -> dict[str, Any]
```
Override a policy decision for an intent.
- `override_cap`: Capability token for override authority
- `rationale_ref`: Reference to rationale record
- `outcome`: Override decision ("ALLOW" or "DENY")

### exo_policy_set
```python
exo_policy_set(
    policy_cap: str,
    repo: str = ".",
    policy_bundle: str | None = None,
    version: str | None = None,
) -> dict[str, Any]
```
Set a new policy bundle for governance.
- `policy_cap`: Capability token for policy authority
- `policy_bundle`: Policy bundle identifier
- `version`: Policy version

### exo_worker_poll
```python
exo_worker_poll(
    repo: str = ".",
    topic_id: str | None = None,
    since_cursor: str | None = None,
    limit: int = 100,
    cursor_file: str | None = None,
    use_cursor: bool = True,
    require_session: bool = False,
) -> dict[str, Any]
```
Poll for pending work items (distributed worker mode).
- `cursor_file`: Path to cursor persistence file
- `use_cursor`: Whether to use cursor for incremental polling
- `require_session`: Whether to require an active session

### exo_escalate
```python
exo_escalate(
    intent_id: str,
    kind: str,
    repo: str = ".",
    ctx_refs: list[str] | None = None,
) -> dict[str, Any]
```
Escalate an intent to human review.
- `kind`: Escalation kind (e.g., "policy_conflict", "ambiguity", "risk")
- `ctx_refs`: Additional context references
