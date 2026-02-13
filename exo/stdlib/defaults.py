from __future__ import annotations

from typing import Any


DEFAULT_CONSTITUTION = """# ExoProtocol Constitution (Kernel v0.1)

This constitution is literate: human guidance plus machine-parsed `exo-policy` blocks.

## Article: Secrets
[RULE-SEC-001] Agents must never read or write host credential stores or dotenv secrets.

```yaml exo-policy
{
  "id": "RULE-SEC-001",
  "type": "filesystem_deny",
  "patterns": ["~/.aws/**", "~/.ssh/**", "**/.env*"],
  "actions": ["read", "write"],
  "message": "Blocked by RULE-SEC-001 (Secrets). Use secret injection."
}
```

## Article: Git internals
[RULE-GIT-001] Agents must not mutate `.git` internals.

```yaml exo-policy
{
  "id": "RULE-GIT-001",
  "type": "filesystem_deny",
  "patterns": [".git/**"],
  "actions": ["read", "write", "delete"],
  "message": "Blocked by RULE-GIT-001 (.git internals are protected)."
}
```

## Article: Kernel is out-of-band
[RULE-KRN-001] Governed flows must never mutate kernel sources.

```yaml exo-policy
{
  "id": "RULE-KRN-001",
  "type": "filesystem_deny",
  "patterns": ["exo/kernel/**"],
  "actions": ["write", "delete"],
  "message": "Blocked by RULE-KRN-001 (kernel updates are out-of-band and human-only)."
}
```

## Article: Protected deletions
[RULE-DEL-001] Source deletes are denied by default.

```yaml exo-policy
{
  "id": "RULE-DEL-001",
  "type": "filesystem_deny",
  "patterns": ["src/**"],
  "actions": ["delete"],
  "message": "Blocked by RULE-DEL-001 (src delete denied by default)."
}
```

## Article: Ticket lock required
[RULE-LOCK-001] Any governed write requires an active ticket lock.

```yaml exo-policy
{
  "id": "RULE-LOCK-001",
  "type": "require_lock",
  "message": "Blocked by RULE-LOCK-001 (acquire a ticket lock first)."
}
```

## Article: Checks before done
[RULE-CHECK-001] A ticket must pass checks before status can move to done.

```yaml exo-policy
{
  "id": "RULE-CHECK-001",
  "type": "require_checks",
  "message": "Blocked by RULE-CHECK-001 (checks must pass before done)."
}
```

## Article: Practice is mutable, governance is sacred
[RULE-EVO-001] Practice changes may use lightweight approval; governance changes require human approval.

```yaml exo-policy
{
  "id": "RULE-EVO-001",
  "type": "evolution_gate",
  "practice_requires": ["approval:any(human|trusted_agent)"],
  "governance_requires": ["approval:human"],
  "message": "Practice is mutable, governance requires explicit human approval."
}
```

## Article: Patch-first evolution
[RULE-EVO-002] No self-evolution applies without proposal + patch + approval + audit trail.

```yaml exo-policy
{
  "id": "RULE-EVO-002",
  "type": "patch_first",
  "requires": ["proposal_artifact", "patch_artifact", "review_artifact", "audit_trail"],
  "message": "Patch-first evolution required."
}
```
"""


DEFAULT_CONFIG = {
    "version": 1,
    "defaults": {
        "ticket_budgets": {
            "max_files_changed": 12,
            "max_loc_changed": 400,
        },
        "ticket_checks": [],
    },
    "checks_allowlist": [
        "npm test",
        "npm run lint",
        "pytest",
        "python -m pytest",
        "python3 -m pytest",
        "python3 -m compileall exo",
    ],
    "do_allowlist": [
        "npm run build",
        "python3 -m compileall exo",
    ],
    "recall_paths": [
        ".exo",
        "docs",
    ],
    "self_evolution": {
        "trusted_approvers": ["agent:trusted"],
        "governance_cooldown_hours": 24,
    },
    "scheduler": {
        "enabled": False,
        "global_concurrency_limit": None,
        "lanes": [],
    },
    "control_caps": {
        "decide_override": ["cap:override"],
        "policy_set": ["cap:policy-set"],
        "cas_head": ["cap:cas-head"],
    },
    "git_controls": {
        "enabled": True,
        "strict_diff_budgets": True,
        "enforce_lock_branch": True,
        "auto_create_lock_branch": True,
        "auto_switch_lock_branch": True,
        "require_clean_worktree_before_do": True,
        "stale_lock_drift_hours": 24,
        "base_branch_fallback": "main",
        "ignore_paths": [
            ".exo/logs/**",
            ".exo/cache/**",
            ".exo/locks/**",
            ".exo/tickets/**",
            "**/__pycache__/**",
            "**/*.pyc",
        ],
    },
    "privacy": {
        "commit_logs": False,
        "redact_local_paths": True,
    },
}


def seed_kernel_tickets(created_at: str) -> list[dict[str, Any]]:
    epic_id = "TICKET-000-EPIC"
    shared_scope = {
        "allow": ["exo/**", ".exo/**", "README.md", "pyproject.toml"],
        "deny": [".env*", "**/.ssh/**", "**/.aws/**", ".git/**"],
    }
    shared_budget = {
        "max_files_changed": 12,
        "max_loc_changed": 400,
    }

    return [
        {
            "id": epic_id,
            "type": "governance",
            "title": "EPIC-001: Repo scaffold & integrity",
            "status": "todo",
            "priority": 5,
            "parent_id": None,
            "spec_ref": ".exo/specs/SPEC-001.md",
            "scope": shared_scope,
            "budgets": shared_budget,
            "checks": ["python3 -m compileall exo"],
            "notes": ["Foundation epic for Kernel v0.1."],
            "blockers": [],
            "labels": ["kernel", "epic"],
            "created_at": created_at,
        },
        {
            "id": "TICKET-001",
            "type": "feature",
            "title": "T1: exo init creates .exo layout",
            "status": "todo",
            "priority": 5,
            "parent_id": epic_id,
            "spec_ref": ".exo/specs/SPEC-001.md",
            "scope": shared_scope,
            "budgets": shared_budget,
            "checks": ["python3 -m compileall exo"],
            "notes": ["Create canonical .exo scaffold."],
            "blockers": [],
            "labels": ["kernel", "bootstrap"],
            "created_at": created_at,
        },
        {
            "id": "TICKET-002",
            "type": "feature",
            "title": "T2: Constitution compiler + governance lock checksum",
            "status": "todo",
            "priority": 5,
            "parent_id": epic_id,
            "spec_ref": ".exo/specs/SPEC-001.md",
            "scope": shared_scope,
            "budgets": shared_budget,
            "checks": ["python3 -m compileall exo"],
            "notes": ["Compile exo-policy blocks into governance.lock.json."],
            "blockers": [],
            "labels": ["kernel", "governance"],
            "created_at": created_at,
        },
        {
            "id": "TICKET-003",
            "type": "feature",
            "title": "T3: Boot-time governance drift detection",
            "status": "todo",
            "priority": 5,
            "parent_id": epic_id,
            "spec_ref": ".exo/specs/SPEC-001.md",
            "scope": shared_scope,
            "budgets": shared_budget,
            "checks": ["python3 -m compileall exo"],
            "notes": ["Hard-fail critical commands on hash mismatch."],
            "blockers": ["TICKET-002"],
            "labels": ["kernel", "integrity"],
            "created_at": created_at,
        },
        {
            "id": "TICKET-004",
            "type": "feature",
            "title": "T4: Ticket schema + loader + validator",
            "status": "todo",
            "priority": 4,
            "parent_id": "TICKET-001",
            "spec_ref": ".exo/specs/SPEC-001.md",
            "scope": shared_scope,
            "budgets": shared_budget,
            "checks": ["python3 -m compileall exo"],
            "notes": ["Add robust defaults and validation for ticket files."],
            "blockers": ["TICKET-001"],
            "labels": ["kernel", "tickets"],
            "created_at": created_at,
        },
        {
            "id": "TICKET-005",
            "type": "feature",
            "title": "T5: Lock acquire/release/expire flow",
            "status": "todo",
            "priority": 4,
            "parent_id": "TICKET-004",
            "spec_ref": ".exo/specs/SPEC-001.md",
            "scope": shared_scope,
            "budgets": shared_budget,
            "checks": ["python3 -m compileall exo"],
            "notes": ["Single-writer lock under .exo/locks/ticket.lock.json."],
            "blockers": ["TICKET-004"],
            "labels": ["kernel", "locking"],
            "created_at": created_at,
        },
        {
            "id": "TICKET-006",
            "type": "feature",
            "title": "T6: Append-only JSONL audit logging",
            "status": "todo",
            "priority": 4,
            "parent_id": "TICKET-005",
            "spec_ref": ".exo/specs/SPEC-001.md",
            "scope": shared_scope,
            "budgets": shared_budget,
            "checks": ["python3 -m compileall exo"],
            "notes": ["Emit deterministic audit events for governed actions."],
            "blockers": ["TICKET-005"],
            "labels": ["kernel", "audit"],
            "created_at": created_at,
        },
    ]
