from __future__ import annotations

from typing import Any

DEFAULT_CONSTITUTION = """# Project Constitution

This constitution is literate: human guidance plus machine-parsed `exo-policy` blocks.
Edit these rules to match your project's needs, then run `exo build-governance` to recompile.

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
    ],
    "global_checks": [],
    "do_allowlist": [
        "npm run build",
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
    "private_memory": {
        "watch_paths": [],
        "enabled": True,
    },
    "coherence": {
        "enabled": True,
        "co_update_rules": [],
        "docstring_languages": ["py"],
        "skip_patterns": [],
    },
}


def seed_kernel_tickets(created_at: str) -> list[dict[str, Any]]:
    """Seed a minimal example intent + task to demonstrate the ticket system.

    These are generic starter tickets — not exo's own development backlog.
    Users can delete them and create their own with `exo intent-create` and
    `exo ticket-create`.
    """
    from exo.kernel.utils import gen_timestamp_id

    intent_id = gen_timestamp_id("INT")
    task_id = gen_timestamp_id("TKT")
    return [
        {
            "id": intent_id,
            "kind": "intent",
            "type": "feature",
            "title": "Example: first governed feature",
            "intent": "Example intent — replace with your first real goal",
            "brain_dump": "This is a starter intent created by exo init. Replace it with your actual goal.",
            "boundary": "",
            "success_condition": "",
            "risk": "low",
            "status": "todo",
            "priority": 3,
            "parent_id": None,
            "scope": {"allow": ["**"], "deny": [".env*", "**/.ssh/**", "**/.aws/**", ".git/**"]},
            "budgets": {"max_files_changed": 12, "max_loc_changed": 400},
            "checks": [],
            "notes": ["Starter intent from exo init. Replace or delete."],
            "blockers": [],
            "labels": ["example"],
            "created_at": created_at,
        },
        {
            "id": task_id,
            "kind": "task",
            "type": "feature",
            "title": "Example: first task",
            "intent": "Example task — replace with your first real task",
            "status": "todo",
            "priority": 3,
            "parent_id": intent_id,
            "scope": {"allow": ["**"], "deny": [".env*", "**/.ssh/**", "**/.aws/**", ".git/**"]},
            "budgets": {"max_files_changed": 12, "max_loc_changed": 400},
            "checks": [],
            "notes": ["Starter task from exo init. Replace or delete."],
            "blockers": [],
            "labels": ["example"],
            "created_at": created_at,
        },
    ]
