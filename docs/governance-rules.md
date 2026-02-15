# Governance Rules Reference

A comprehensive guide to ExoProtocol governance rules for AI agents operating in governed repositories.

## Rule Sources

ExoProtocol governance is derived from three authoritative sources:

- **Constitution**: `.exo/CONSTITUTION.md` — human-editable policy document with YAML `exo-policy` blocks
- **Lock**: `.exo/governance.lock.json` — compiled, machine-parsed governance state
- **Config**: `.exo/config.yaml` — budgets, checks allowlist, scheduler settings, subsystem config

## Compilation & Verification

### Building Governance

```bash
exo build-governance
```

Compiles the constitution into the governance lock file. The lock hash is derived from the constitution content.

### Verifying Governance

```bash
exo audit
```

Verifies that the lock file matches the current constitution. Detects:
- Constitution changes not compiled into lock
- Lock tampering
- Governance drift

## Rule Types

### Filesystem Deny Rules

Pattern: `deny {actions} on {glob patterns}`

Actions: `read`, `write`, `delete`

**Default Rules:**

- **RULE-SEC-001**: `deny read, write on ~/.aws/**, ~/.ssh/**, **/.env*`
  - Protects secrets and credentials
- **RULE-GIT-001**: `deny read, write, delete on .git/**`
  - Prevents direct Git object manipulation

**Smart Init Rules:**

- **RULE-KRN-001**: `deny write, delete on exo/kernel/**`
  - Kernel freeze — core functions immutable without RFC
- **RULE-DEL-001**: `deny delete on src/**`
  - Delete protection for detected source directories

**Custom Rules:**

Add filesystem deny rules in `.exo/CONSTITUTION.md`:

```yaml
---
exo-policy: filesystem_deny
actions: [write, delete]
patterns:
  - "config/production/**"
  - "deployment/**"
---
```

### Require Lock (RULE-LOCK-001)

**Rule**: Any governed write operation requires an active ticket lock.

**Enforcement**: Session start acquires lock, session finish releases lock. Concurrent sessions on the same ticket are blocked.

**Override**: Use `--break-glass-reason` to bypass (recorded in audit trail).

### Require Checks (RULE-CHECK-001)

**Rule**: All checks must pass before session finish marks ticket as `done`.

**Checks Allowlist**: Defined in `.exo/config.yaml`:

```yaml
checks_allowlist:
  - npm test
  - npm run lint
  - pytest
  - python -m pytest
  - ruff check
```

**Enforcement**: Session finish runs checks from allowlist. Non-allowlisted checks are rejected.

**Override**: Use `--skip-check` with `--break-glass-reason` (recorded in audit trail).

### Evolution Gate (RULE-EVO-001)

**Rule**: Practice is mutable, governance requires explicit human approval.

**Practice**: Agent can modify application code, tests, docs with any approval mechanism.

**Governance**: Changes to `.exo/CONSTITUTION.md`, `.exo/config.yaml`, governance rules require human review and explicit approval.

### Patch First (RULE-EVO-002)

**Rule**: Governance changes follow proposal → patch → approval → audit trail workflow.

**Process**:
1. Create intent ticket describing governance change
2. Submit patch via session
3. Human reviews and approves
4. Audit session verifies compliance
5. Merge with full trail

## Budgets

### Ticket Budgets

Per-ticket limits on scope of change:

- `max_files_changed`: Maximum files modified in one ticket
- `max_loc_changed`: Maximum lines of code changed in one ticket

**Defaults** (from `.exo/config.yaml`):

```yaml
ticket_budgets:
  default:
    max_files_changed: 12
    max_loc_changed: 400
```

**Per-Ticket Override**:

```bash
exo ticket-create --title "..." --max-files 20 --max-loc 800
```

### Drift Score Formula

Session finish calculates drift score:

```
drift = (0.4 × scope_violations) +
        (0.3 × file_budget_ratio) +
        (0.2 × loc_budget_ratio) +
        (0.1 × boundary_violations)
```

- Drift > 0.7: High drift warning, may trigger follow-up ticket
- Drift recorded in session memento and index

## Scope Enforcement

### Ticket Scope

Tickets define allowed/denied file patterns:

```bash
exo ticket-create \
  --title "Fix auth" \
  --scope-allow "src/auth/**" \
  --scope-deny "src/auth/third_party/**"
```

**Enforcement**: Session finish checks modified files against scope patterns. Violations → scope drift.

### Feature Manifest Scope

Features with `allow_agent_edit: false` generate automatic deny patterns:

```yaml
# .exo/features.yaml
features:
  - id: FEAT-KERN-001
    name: Ticket Allocation
    files: ["exo/kernel/tickets.py"]
    allow_agent_edit: false
```

Result: `exo/kernel/tickets.py` added to session scope deny list.

## Feature Traceability

### Feature Lifecycle

Defined in `.exo/features.yaml`:

- `active`: Current production feature
- `experimental`: In development, unstable
- `deprecated`: Scheduled for removal
- `deleted`: No longer exists, code should be removed

### Code Tags

Mark feature boundaries in source code:

```python
# @feature:FEAT-AUTH-001
def authenticate_user(token):
    """Verify JWT token."""
    # implementation
# @endfeature
```

Single-point tags (no block):

```python
from auth import verify  # @feature:FEAT-AUTH-001
```

### Feature Tracing

```bash
exo trace [--glob "src/**"]
```

**Detects**:
- `invalid_tag`: Malformed or unknown feature ID (error)
- `deleted_usage`: Code tagged with deleted feature (error)
- `deprecated_usage`: Code tagged with deprecated feature (warning)
- `locked_edit`: Edits to `allow_agent_edit: false` feature (warning)
- `unbound_feature`: Feature in manifest with no code coverage (warning)
- `uncovered_code`: Files not covered by any feature (warning)

**Session Integration**: `exo trace` runs advisory at session finish, results in memento.

### Pruning

```bash
exo prune [--include-deprecated] [--dry-run]
```

Automatically removes code blocks tagged with `deleted` (or `deprecated` if flag set) features.

## Requirement Traceability

### Requirement Lifecycle

Defined in `.exo/requirements.yaml`:

- `active`: Current requirement
- `deprecated`: No longer enforced
- `deleted`: Removed from system

**Priorities**: `high`, `medium`, `low`

### Code Annotations

Mark requirement implementation:

```python
# @req:REQ-SEC-001
def hash_password(password):
    """Hash password using bcrypt."""
    # @implements:REQ-SEC-001
    return bcrypt.hashpw(password, bcrypt.gensalt())
```

Multi-requirement annotation:

```python
# @implements:REQ-AUTH-001,REQ-AUDIT-002
```

### Requirement Tracing

```bash
exo trace-reqs [--no-check-uncovered]
```

**Detects**:
- `orphan_ref`: Code references non-existent requirement (error)
- `deleted_ref`: Code references deleted requirement (error)
- `deprecated_ref`: Code references deprecated requirement (warning)
- `uncovered_req`: Active requirement with no code implementation (warning)

## Coherence Rules

### Co-Update Rules

File pairs that must change together (defined in `.exo/config.yaml`):

```yaml
coherence:
  enabled: true
  co_update_rules:
    - [".exo/features.yaml", "exo/stdlib/features.py"]
    - ["package.json", "package-lock.json"]
```

**Detection**: Checks if only one file in pair was modified in current branch vs base.

### Docstring Freshness

Detects functions modified without docstring updates:

```bash
exo coherence [--base main]
```

**Supported Languages**: Python (detects `def` functions and docstrings).

**Severity**: `warning` (advisory, won't block session finish).

## Chain Reaction Follow-Ups

Session finish automatically detects governance gaps and creates follow-up tickets:

**Detection Rules**:
- Uncovered code → `medium` severity follow-up
- Unbound feature → `medium` severity follow-up
- Drift > 0.7 → `high` severity follow-up
- Uncovered requirement → `medium` severity follow-up
- Tools created but not used → `low` severity follow-up

**Config**:

```yaml
follow_up:
  enabled: true
  max_per_session: 5
```

## Agent Compliance Checklist

### 1. Start Session

```bash
EXO_ACTOR=agent:claude python3 -m exo.cli session-start \
  --ticket-id TICKET-001 \
  --vendor anthropic \
  --model claude-sonnet-4-5 \
  --task "Implement user authentication"
```

Acquires ticket lock, records start time, git branch.

### 2. Read Bootstrap Prompt

Open `.exo/cache/sessions/agent-<name>.bootstrap.md` and follow directives:
- Governance rules reminder
- Sibling sessions awareness
- Start advisories (conflicts, stale branches)
- Operational learnings (reflections)
- Tool reuse protocol
- Current task description

### 3. Stay Within Scope

Only modify files matching ticket's `scope.allow` patterns, excluding `scope.deny` patterns.

### 4. Tag Code with Features

Wrap new code blocks:

```python
# @feature:FEAT-AUTH-002
def validate_token(token):
    # implementation
# @endfeature
```

### 5. Annotate with Requirements

Mark requirement implementations:

```python
# @implements:REQ-AUTH-001
```

### 6. Run Trace Before Finishing

```bash
exo trace
exo trace-reqs
```

Fix any error-severity violations before session finish.

### 7. Search Tools Before Building

```bash
exo tool-search "csv parsing"
```

If reusable tool exists, use it. If building new tool, register it:

```bash
exo tool-register lib/parsers.py parse_csv \
  --description "Parse CSV with header detection"
```

### 8. Record Learnings

When discovering gotchas, patterns, or operational insights:

```bash
exo reflect \
  --pattern "ruff SIM105 requires contextlib.suppress" \
  --insight "Use contextlib.suppress() instead of try-except-pass for cleaner code"
```

**Do NOT** write to private memory files without also creating a shared reflection.

### 9. Finish Session

```bash
EXO_ACTOR=agent:claude python3 -m exo.cli session-finish \
  --ticket-id TICKET-001 \
  --summary "Implemented JWT-based authentication with bcrypt password hashing" \
  --set-status review
```

Runs checks, calculates drift, generates memento, releases lock.

## Advanced: Audit Sessions

For adversarial review of governed work:

```bash
exo session-audit \
  --ticket-id TICKET-001 \
  --vendor anthropic \
  --model claude-opus-4-6 \
  --pr-base main \
  --pr-head feature-branch
```

**Audit Mode Features**:
- Adversarial persona (Red Team Auditor)
- Context isolation (denies `.exo/cache/**`, `.exo/memory/**`)
- PR governance report (ungoverned commits, scope violations, drift)
- Writing session model-mismatch detection
- Advisory warnings (no artifacts, same model as writer)

## Reference Commands

```bash
# Governance
exo build-governance        # Compile constitution → lock
exo audit                   # Verify lock matches constitution
exo drift                   # Composite health check

# Features
exo features                # List features
exo trace                   # Check feature traceability
exo prune                   # Remove deleted feature code

# Requirements
exo requirements            # List requirements
exo trace-reqs              # Check requirement traceability

# Tools
exo tools                   # List registered tools
exo tool-search <keywords>  # Search tool registry
exo tool-register <module> <function>

# Reflection
exo reflect --pattern "..." --insight "..."
exo reflections             # List active reflections

# Sessions
exo session-start           # Begin governed work
exo session-finish          # Complete and verify
exo session-audit           # Adversarial review
exo scan-sessions           # List active sessions

# Maintenance
exo gc                      # Clean old session artifacts
exo doctor                  # Health check
exo upgrade                 # Migrate to latest schema
```

---

**Remember**: Governance exists to ensure quality, traceability, and safe evolution. When in doubt, read the bootstrap prompt and follow the checklist.
