from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from exo.control.syscalls import KernelSyscalls
from exo.kernel.errors import ExoError
from exo.kernel.tickets import (
    allocate_intent_id,
    allocate_ticket_id,
    load_ticket,
    normalize_ticket,
    save_ticket,
    validate_intent_hierarchy,
)
from exo.kernel.utils import default_topic_id
from exo.orchestrator import AgentSessionManager, DistributedWorker, cleanup_sessions, scan_sessions
from exo.stdlib.adapters import ADAPTER_TARGETS, generate_adapters
from exo.stdlib.drift import drift as run_drift
from exo.stdlib.drift import drift_to_dict, format_drift_human
from exo.stdlib.engine import KernelEngine
from exo.stdlib.features import (
    features_to_list,
    format_prune_human,
    format_trace_human,
    generate_scope_deny,
    load_features,
    prune,
    prune_to_dict,
    trace,
    trace_to_dict,
)
from exo.stdlib.gc import format_gc_human, gc_to_dict
from exo.stdlib.gc import gc as run_gc
from exo.stdlib.pr_check import format_pr_check_human, pr_check, pr_check_to_dict
from exo.stdlib.reflect import (
    dismiss_reflection,
    format_reflections_human,
    load_reflections,
    reflect_to_dict,
    reflections_to_list,
)
from exo.stdlib.reflect import (
    reflect as do_reflect,
)
from exo.stdlib.requirements import (
    format_req_trace_human,
    load_requirements,
    req_trace_to_dict,
    requirements_to_list,
    trace_requirements,
)
from exo.stdlib.timeline import build_intent_timeline, format_timeline_human


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="exo", description="ExoProtocol Kernel CLI")
    parser.add_argument("--format", choices=["human", "json"], default="human")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--no-llm", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    init_cmd = sub.add_parser("init", help="Create .exo scaffold")
    init_cmd.add_argument("--seed", action="store_true", help="Seed starter SPEC + first kernel tickets")
    init_cmd.add_argument("--no-scan", action="store_true", help="Skip repo scanning (use generic defaults)")

    sidecar_cmd = sub.add_parser(
        "sidecar-init",
        help="Mount governance sidecar as dedicated git worktree (.exo -> exo-governance)",
    )
    sidecar_cmd.add_argument("--branch", default="exo-governance")
    sidecar_cmd.add_argument("--sidecar", default=".exo")
    sidecar_cmd.add_argument("--remote", default="origin")
    sidecar_cmd.add_argument("--default-branch", default="main")
    sidecar_cmd.add_argument("--no-init-git", action="store_true")
    sidecar_cmd.add_argument("--no-fetch-remote", action="store_true")
    sidecar_cmd.add_argument("--no-commit-migration", action="store_true")

    sub.add_parser("build-governance", help="Compile constitution into governance lock")
    sub.add_parser("audit", help="Run integrity/rule/lock audit")

    plan_cmd = sub.add_parser("plan", help="Generate SPEC + tickets")
    plan_cmd.add_argument("input", help="Inline text or file path")

    next_cmd = sub.add_parser("next", help="Dispatch next ticket and acquire lock")
    next_cmd.add_argument("--owner", default="human")
    next_cmd.add_argument("--role", default="developer")
    next_cmd.add_argument("--hours", type=int, default=2)
    next_cmd.add_argument("--distributed", action="store_true")
    next_cmd.add_argument("--remote", default="origin")

    renew_cmd = sub.add_parser("lease-renew", help="Renew active ticket lease and increment fencing token")
    renew_cmd.add_argument("--ticket-id")
    renew_cmd.add_argument("--owner")
    renew_cmd.add_argument("--role")
    renew_cmd.add_argument("--hours", type=int, default=2)
    renew_cmd.add_argument("--distributed", action="store_true")
    renew_cmd.add_argument("--remote", default="origin")

    heartbeat_cmd = sub.add_parser(
        "lease-heartbeat", help="Heartbeat active ticket lease without changing fencing token"
    )
    heartbeat_cmd.add_argument("--ticket-id")
    heartbeat_cmd.add_argument("--owner")
    heartbeat_cmd.add_argument("--hours", type=int, default=2)
    heartbeat_cmd.add_argument("--distributed", action="store_true")
    heartbeat_cmd.add_argument("--remote", default="origin")

    release_cmd = sub.add_parser("lease-release", help="Release active ticket lease")
    release_cmd.add_argument("--ticket-id")
    release_cmd.add_argument("--owner")
    release_cmd.add_argument("--distributed", action="store_true")
    release_cmd.add_argument("--remote", default="origin")
    release_cmd.add_argument("--ignore-missing", action="store_true")

    do_cmd = sub.add_parser("do", help="Run controlled execution pipeline")
    do_cmd.add_argument("ticket_id", nargs="?")
    do_cmd.add_argument("--patch", dest="patch_file", help="Patch spec (yaml/json) with patches[]")
    do_cmd.add_argument("--no-mark-done", action="store_true")

    check_cmd = sub.add_parser("check", help="Run allowlisted checks")
    check_cmd.add_argument("ticket_id", nargs="?")

    sub.add_parser("status", help="Show kernel status")

    jot_cmd = sub.add_parser("jot", help="Append to scratchpad inbox")
    jot_cmd.add_argument("line")

    thread_cmd = sub.add_parser("thread", help="Create scratchpad thread")
    thread_cmd.add_argument("topic")

    promote_cmd = sub.add_parser("promote", help="Promote thread to ticket")
    promote_cmd.add_argument("thread_id")
    promote_cmd.add_argument("--to", default="ticket")

    recall_cmd = sub.add_parser("recall", help="Search local memory paths")
    recall_cmd.add_argument("query")

    subscribe_cmd = sub.add_parser("subscribe", help="Read ledger events with cursor semantics")
    subscribe_cmd.add_argument("--topic", dest="topic_id")
    subscribe_cmd.add_argument("--since", dest="since_cursor")
    subscribe_cmd.add_argument("--limit", type=int, default=100)

    ack_cmd = sub.add_parser("ack", help="Append acknowledgement for a ledger reference")
    ack_cmd.add_argument("ref_id")
    ack_cmd.add_argument("--required", type=int, default=1)
    ack_cmd.add_argument("--actor-cap", default="cap:ack")

    quorum_cmd = sub.add_parser("quorum", help="Evaluate acknowledgement quorum for a ledger reference")
    quorum_cmd.add_argument("ref_id")
    quorum_cmd.add_argument("--required", type=int, default=1)

    head_cmd = sub.add_parser("head", help="Read current head pointer for a topic")
    head_cmd.add_argument("--topic", required=True, dest="topic_id")

    cas_cmd = sub.add_parser("cas-head", help="Compare-and-swap a topic head with retry semantics")
    cas_cmd.add_argument("--topic", required=True, dest="topic_id")
    cas_cmd.add_argument("--expected", dest="expected_ref")
    cas_cmd.add_argument("--new", dest="new_ref")
    cas_cmd.add_argument("--max-attempts", type=int, default=1)
    cas_cmd.add_argument("--cap", required=True, dest="control_cap")

    override_cmd = sub.add_parser("decide-override", help="Record privileged override decision for an intent")
    override_cmd.add_argument("intent_id")
    override_cmd.add_argument("--override-cap", required=True)
    override_cmd.add_argument("--rationale-ref", required=True)
    override_cmd.add_argument("--outcome", default="ALLOW", choices=["ALLOW", "DENY", "ESCALATE", "SANDBOX"])

    policy_cmd = sub.add_parser("policy-set", help="Compile and install governance policy bundle")
    policy_cmd.add_argument("--policy-cap", required=True)
    policy_cmd.add_argument("--bundle", dest="policy_bundle")
    policy_cmd.add_argument("--version")

    submit_intent_cmd = sub.add_parser("submit-intent", help="Submit low-level intent via kernel syscall surface")
    submit_intent_cmd.add_argument("--intent", required=True)
    submit_intent_cmd.add_argument("--topic", dest="topic_id")
    submit_intent_cmd.add_argument("--intent-id")
    submit_intent_cmd.add_argument("--ttl-hours", type=int, default=1)
    submit_intent_cmd.add_argument("--scope-allow", action="append", default=[])
    submit_intent_cmd.add_argument("--scope-deny", action="append", default=[])
    submit_intent_cmd.add_argument("--action-kind", default="read_file")
    submit_intent_cmd.add_argument("--target")
    submit_intent_cmd.add_argument("--expected-ref")
    submit_intent_cmd.add_argument("--max-attempts", type=int, default=1)

    check_intent_cmd = sub.add_parser("check-intent", help="Evaluate intent policy and produce DecisionRecorded")
    check_intent_cmd.add_argument("intent_id")

    begin_effect_cmd = sub.add_parser("begin-effect", help="Begin executable effect for a decision")
    begin_effect_cmd.add_argument("decision_id")
    begin_effect_cmd.add_argument("--executor-ref", required=True)
    begin_effect_cmd.add_argument("--idem-key", required=True)

    commit_effect_cmd = sub.add_parser("commit-effect", help="Commit effect result (write-once)")
    commit_effect_cmd.add_argument("effect_id")
    commit_effect_cmd.add_argument("--status", required=True, choices=["OK", "FAIL", "RETRYABLE_FAIL", "CANCELED"])
    commit_effect_cmd.add_argument("--artifact-ref", action="append", default=[])

    read_ledger_cmd = sub.add_parser("read-ledger", help="Read ledger records via syscall surface")
    read_ledger_cmd.add_argument("ref_id", nargs="?")
    read_ledger_cmd.add_argument("--type", dest="type_filter")
    read_ledger_cmd.add_argument("--since", dest="since_cursor")
    read_ledger_cmd.add_argument("--limit", type=int, default=200)
    read_ledger_cmd.add_argument("--topic", dest="topic_id")
    read_ledger_cmd.add_argument("--intent", dest="intent_id")

    escalate_cmd = sub.add_parser("escalate-intent", help="Record escalation for an intent")
    escalate_cmd.add_argument("intent_id")
    escalate_cmd.add_argument("--kind", required=True)
    escalate_cmd.add_argument("--ctx-ref", action="append", default=[])

    observe_cmd = sub.add_parser("observe", help="Record a governed observation artifact")
    observe_cmd.add_argument("--ticket", required=True)
    observe_cmd.add_argument("--tag", required=True)
    observe_cmd.add_argument("--msg", required=True)
    observe_cmd.add_argument("--trigger", action="append", default=[])
    observe_cmd.add_argument("--confidence", choices=["low", "medium", "high"], default="high")

    propose_cmd = sub.add_parser("propose", help="Create proposal + patch artifact")
    propose_cmd.add_argument("--ticket", required=True)
    propose_cmd.add_argument(
        "--kind", required=True, choices=["practice_change", "governance_change", "tooling_change"]
    )
    propose_cmd.add_argument("--summary")
    propose_cmd.add_argument("--symptom", action="append", required=True)
    propose_cmd.add_argument("--root-cause", required=True, dest="root_cause")
    propose_cmd.add_argument("--expected-effect", action="append", default=[], dest="expected_effect")
    propose_cmd.add_argument("--risk-level", default="medium", dest="risk_level")
    propose_cmd.add_argument("--blast-radius", action="append", default=[])
    propose_cmd.add_argument("--rollback-type", default="delete_file")
    propose_cmd.add_argument("--rollback-path")
    propose_cmd.add_argument("--change-type", default="patch_file")
    propose_cmd.add_argument("--change-path")
    propose_cmd.add_argument("--evidence-observation", action="append", default=[])
    propose_cmd.add_argument("--evidence-audit", action="append", default=[])
    propose_cmd.add_argument("--note", action="append", default=[])
    propose_cmd.add_argument("--requires-approvals", type=int, default=1)
    propose_cmd.add_argument("--human-required", action="store_true")
    propose_cmd.add_argument("--patch", dest="patch_file")

    approve_cmd = sub.add_parser("approve", help="Approve or reject proposal and write review artifact")
    approve_cmd.add_argument("proposal_id")
    approve_cmd.add_argument("--decision", default="approved", choices=["approved", "rejected"])
    approve_cmd.add_argument("--note", default="")

    apply_cmd = sub.add_parser("apply", help="Apply approved proposal patch (guarded)")
    apply_cmd.add_argument("proposal_id")

    distill_cmd = sub.add_parser("distill", help="Distill applied proposal into memory index")
    distill_cmd.add_argument("proposal_id")
    distill_cmd.add_argument("--statement")
    distill_cmd.add_argument("--confidence", type=float, default=0.7)

    session_start_cmd = sub.add_parser("session-start", help="Bootstrap an agent session with bounded context metadata")
    session_start_cmd.add_argument("--ticket-id")
    session_start_cmd.add_argument("--vendor", default="unknown")
    session_start_cmd.add_argument("--model", default="unknown")
    session_start_cmd.add_argument("--context-window", type=int)
    session_start_cmd.add_argument("--role")
    session_start_cmd.add_argument("--task")
    session_start_cmd.add_argument("--topic", dest="topic_id")
    session_start_cmd.add_argument("--acquire-lock", action="store_true")
    session_start_cmd.add_argument("--distributed", action="store_true")
    session_start_cmd.add_argument("--remote", default="origin")
    session_start_cmd.add_argument("--hours", type=int, default=2)

    suspend_cmd = sub.add_parser(
        "session-suspend", help="Suspend active agent session, release lock, and snapshot context"
    )
    suspend_cmd.add_argument("--ticket-id")
    suspend_cmd.add_argument("--reason", required=True)
    suspend_cmd.add_argument("--no-release-lock", action="store_true")
    suspend_cmd.add_argument("--stash", action="store_true", help="Git-stash uncommitted changes")

    resume_cmd = sub.add_parser("session-resume", help="Resume a suspended agent session and reacquire lock")
    resume_cmd.add_argument("--ticket-id")
    resume_cmd.add_argument("--no-acquire-lock", action="store_true")
    resume_cmd.add_argument("--pop-stash", action="store_true", help="Pop git stash on resume")
    resume_cmd.add_argument("--distributed", action="store_true")
    resume_cmd.add_argument("--remote", default="origin")
    resume_cmd.add_argument("--hours", type=int, default=2)
    resume_cmd.add_argument("--role")

    scan_cmd = sub.add_parser("session-scan", help="List all active and suspended sessions, flag stale ones")
    scan_cmd.add_argument("--stale-hours", type=float, default=48.0)

    cleanup_cmd = sub.add_parser("session-cleanup", help="Remove stale/orphaned sessions and optionally release locks")
    cleanup_cmd.add_argument("--stale-hours", type=float, default=48.0)
    cleanup_cmd.add_argument("--force", action="store_true", help="Remove ALL sessions, not just stale ones")
    cleanup_cmd.add_argument("--release-lock", action="store_true", help="Also release orphaned lock")

    session_finish_cmd = sub.add_parser("session-finish", help="Close active agent session and write memento")
    session_finish_cmd.add_argument("--ticket-id")
    session_finish_cmd.add_argument("--summary", required=True)
    session_finish_cmd.add_argument("--set-status", default="review", choices=["keep", "review", "done"])
    session_finish_cmd.add_argument("--artifact", action="append", default=[])
    session_finish_cmd.add_argument("--blocker", action="append", default=[])
    session_finish_cmd.add_argument("--next-step")
    session_finish_cmd.add_argument("--skip-check", action="store_true")
    session_finish_cmd.add_argument("--break-glass-reason")
    session_finish_cmd.add_argument("--release-lock", action="store_true")
    session_finish_cmd.add_argument("--no-release-lock", action="store_true")
    session_finish_cmd.add_argument("--drift-threshold", type=float)
    session_finish_cmd.add_argument(
        "--error",
        action="append",
        default=[],
        dest="errors",
        help="Error encountered during session (format: 'tool:message' or just 'message')",
    )

    audit_session_cmd = sub.add_parser(
        "session-audit", help="Start an isolated audit session with context isolation and adversarial persona"
    )
    audit_session_cmd.add_argument("--ticket-id", required=True)
    audit_session_cmd.add_argument("--vendor", default="unknown")
    audit_session_cmd.add_argument("--model", default="unknown")
    audit_session_cmd.add_argument("--context-window", type=int)
    audit_session_cmd.add_argument("--role", default="auditor")
    audit_session_cmd.add_argument("--task")
    audit_session_cmd.add_argument("--topic", dest="topic_id")
    audit_session_cmd.add_argument("--acquire-lock", action="store_true")
    audit_session_cmd.add_argument("--distributed", action="store_true")
    audit_session_cmd.add_argument("--remote", default="origin")
    audit_session_cmd.add_argument("--hours", type=int, default=2)
    audit_session_cmd.add_argument("--pr-base", default=None, help="Base branch/ref for PR governance check")
    audit_session_cmd.add_argument("--pr-head", default=None, help="Head ref for PR governance check")

    intents_cmd = sub.add_parser("intents", help="Show intent timeline with drift tracking")
    intents_cmd.add_argument("--status", help="Filter by status (e.g. active, done, todo)")
    intents_cmd.add_argument("--drift-above", type=float, help="Show only intents with drift above threshold")

    validate_hierarchy_cmd = sub.add_parser("validate-hierarchy", help="Validate intent hierarchy for a ticket")
    validate_hierarchy_cmd.add_argument("ticket_id")

    intent_create_cmd = sub.add_parser("intent-create", help="Create an intent ticket from a brain dump")
    intent_create_cmd.add_argument("title", help="Short intent title")
    intent_create_cmd.add_argument(
        "--brain-dump", required=True, dest="brain_dump", help="Original user input / brain dump"
    )
    intent_create_cmd.add_argument("--boundary", default="", help="Scope boundary description (what NOT to touch)")
    intent_create_cmd.add_argument(
        "--success-condition", default="", dest="success_condition", help="What 'done' looks like"
    )
    intent_create_cmd.add_argument("--risk", default="medium", choices=["low", "medium", "high"])
    intent_create_cmd.add_argument("--scope-allow", action="append", default=[], help="Allowed file globs")
    intent_create_cmd.add_argument("--scope-deny", action="append", default=[], help="Denied file globs")
    intent_create_cmd.add_argument("--max-files", type=int, default=12, help="File budget")
    intent_create_cmd.add_argument("--max-loc", type=int, default=400, help="LOC budget")
    intent_create_cmd.add_argument("--priority", type=int, default=3)
    intent_create_cmd.add_argument("--label", action="append", default=[])

    ticket_create_cmd = sub.add_parser("ticket-create", help="Create a task or epic ticket linked to an intent")
    ticket_create_cmd.add_argument("title", help="Short ticket title")
    ticket_create_cmd.add_argument("--kind", default="task", choices=["task", "epic"])
    ticket_create_cmd.add_argument(
        "--parent", required=True, dest="parent_id", help="Parent ticket ID (intent or epic)"
    )
    ticket_create_cmd.add_argument("--scope-allow", action="append", default=[], help="Allowed file globs")
    ticket_create_cmd.add_argument("--scope-deny", action="append", default=[], help="Denied file globs")
    ticket_create_cmd.add_argument("--max-files", type=int, default=12, help="File budget")
    ticket_create_cmd.add_argument("--max-loc", type=int, default=400, help="LOC budget")
    ticket_create_cmd.add_argument("--priority", type=int, default=3)
    ticket_create_cmd.add_argument("--label", action="append", default=[])
    ticket_create_cmd.add_argument("--boundary", default="")
    ticket_create_cmd.add_argument("--success-condition", default="", dest="success_condition")

    pr_check_cmd = sub.add_parser("pr-check", help="Check governance compliance for all commits in a PR range")
    pr_check_cmd.add_argument("--base", default="main", help="Base branch/ref (default: main)")
    pr_check_cmd.add_argument("--head", default="HEAD", help="Head ref (default: HEAD)")
    pr_check_cmd.add_argument("--drift-threshold", type=float, default=0.7, help="Drift score threshold for warnings")

    adapter_cmd = sub.add_parser(
        "adapter-generate",
        help="Generate repo-root agent config files (CLAUDE.md, .cursorrules, AGENTS.md) from governance state",
    )
    adapter_cmd.add_argument(
        "--target",
        action="append",
        default=[],
        help=f"Target adapter(s): {', '.join(sorted(ADAPTER_TARGETS))}. Omit for all.",
    )
    adapter_cmd.add_argument("--dry-run", action="store_true", help="Preview output without writing files")

    features_cmd = sub.add_parser("features", help="List feature definitions from .exo/features.yaml")
    features_cmd.add_argument("--status", help="Filter by status (active, deprecated, deleted, experimental)")

    trace_cmd = sub.add_parser(
        "trace", help="Run feature traceability linter: cross-reference @feature: tags against manifest"
    )
    trace_cmd.add_argument(
        "--glob", action="append", default=[], help="File globs to scan (default: common source extensions)"
    )
    trace_cmd.add_argument(
        "--no-check-unbound", action="store_true", help="Skip checking for features with no code tags"
    )

    prune_cmd = sub.add_parser("prune", help="Remove code blocks tagged with deleted (or deprecated) features")
    prune_cmd.add_argument(
        "--include-deprecated", action="store_true", help="Also prune deprecated features (default: only deleted)"
    )
    prune_cmd.add_argument("--glob", action="append", default=[], help="File globs to scan")
    prune_cmd.add_argument("--dry-run", action="store_true", help="Preview removals without modifying files")

    requirements_cmd = sub.add_parser("requirements", help="List requirement definitions from .exo/requirements.yaml")
    requirements_cmd.add_argument("--status", help="Filter by status (active, deprecated, deleted)")

    trace_reqs_cmd = sub.add_parser(
        "trace-reqs", help="Run requirement traceability linter: cross-reference @req: annotations against manifest"
    )
    trace_reqs_cmd.add_argument(
        "--glob", action="append", default=[], help="File globs to scan (default: common source extensions)"
    )
    trace_reqs_cmd.add_argument(
        "--no-check-uncovered", action="store_true", help="Skip checking for requirements with no code refs"
    )

    drift_cmd = sub.add_parser("drift", help="Run composite governance drift check across all subsystems")
    drift_cmd.add_argument(
        "--stale-hours", type=float, default=48.0, help="Threshold for flagging stale sessions (default: 48)"
    )
    drift_cmd.add_argument("--skip-adapters", action="store_true", help="Skip adapter freshness check")
    drift_cmd.add_argument("--skip-features", action="store_true", help="Skip feature traceability check")
    drift_cmd.add_argument("--skip-requirements", action="store_true", help="Skip requirement traceability check")
    drift_cmd.add_argument("--skip-sessions", action="store_true", help="Skip session health check")
    drift_cmd.add_argument("--skip-coherence", action="store_true", help="Skip coherence check")

    coherence_cmd = sub.add_parser(
        "coherence", help="Check semantic coherence: co-update rules and docstring freshness"
    )
    coherence_cmd.add_argument("--skip-co-updates", action="store_true", help="Skip co-update rule checks")
    coherence_cmd.add_argument("--skip-docstrings", action="store_true", help="Skip docstring freshness checks")
    coherence_cmd.add_argument("--base", default="main", help="Git base ref (default: main)")

    gc_cmd = sub.add_parser("gc", help="Garbage-collect old mementos, cursors, and bootstraps")
    gc_cmd.add_argument("--max-age-days", type=float, default=30.0, help="Age threshold in days (default: 30)")
    gc_cmd.add_argument("--dry-run", action="store_true", help="Preview what would be removed")

    gc_locks_cmd = sub.add_parser("gc-locks", help="Clean up expired distributed locks on remote")
    gc_locks_cmd.add_argument("--remote", default="origin", help="Git remote name (default: origin)")
    gc_locks_cmd.add_argument("--dry-run", action="store_true", help="Preview what would be cleaned up")
    gc_locks_cmd.add_argument(
        "--list", action="store_true", dest="list_only", help="List all remote locks without cleaning"
    )

    reflect_cmd = sub.add_parser("reflect", help="Record an operational learning from session experience")
    reflect_cmd.add_argument("--pattern", required=True, help="What keeps happening (the recurring failure/error)")
    reflect_cmd.add_argument("--insight", required=True, help="What was learned (the fix/workaround)")
    reflect_cmd.add_argument("--severity", default="medium", choices=["low", "medium", "high", "critical"])
    reflect_cmd.add_argument("--scope", default="global", help="'global' or a ticket ID")
    reflect_cmd.add_argument("--tag", action="append", default=[], help="Categorization tags")
    reflect_cmd.add_argument("--session-id", default="", help="Session ID (auto-detected if in active session)")

    reflections_cmd = sub.add_parser("reflections", help="List stored reflections with optional filters")
    reflections_cmd.add_argument("--status", help="Filter by status (active, superseded, dismissed)")
    reflections_cmd.add_argument("--scope", help="Filter by scope (global or ticket ID)")
    reflections_cmd.add_argument("--severity", help="Filter by severity")

    dismiss_cmd = sub.add_parser("reflect-dismiss", help="Dismiss a reflection so it stops appearing in bootstraps")
    dismiss_cmd.add_argument("reflection_id", help="Reflection ID (e.g., REF-001)")

    sub.add_parser("scan", help="Scan repo and preview what exo init would detect")

    doctor_cmd = sub.add_parser("doctor", help="Run unified governance health check")
    doctor_cmd.add_argument(
        "--stale-hours", type=float, default=48.0, help="Hours before a session is considered stale"
    )

    sub.add_parser("config-validate", help="Validate .exo/config.yaml structure and values")

    upgrade_cmd = sub.add_parser("upgrade", help="Upgrade .exo/ to latest schema version")
    upgrade_cmd.add_argument("--dry-run", action="store_true", help="Preview changes without applying")

    worker_poll_cmd = sub.add_parser("worker-poll", help="Poll ledger topic once and execute pending intents")
    worker_poll_cmd.add_argument("--topic", dest="topic_id")
    worker_poll_cmd.add_argument("--since", dest="since_cursor")
    worker_poll_cmd.add_argument("--limit", type=int, default=100)
    worker_poll_cmd.add_argument("--cursor-file")
    worker_poll_cmd.add_argument("--no-cursor", action="store_true")
    worker_poll_cmd.add_argument("--require-session", action="store_true")

    worker_loop_cmd = sub.add_parser("worker-loop", help="Run repeated ledger polling loop")
    worker_loop_cmd.add_argument("--topic", dest="topic_id")
    worker_loop_cmd.add_argument("--since", dest="since_cursor")
    worker_loop_cmd.add_argument("--limit", type=int, default=100)
    worker_loop_cmd.add_argument("--iterations", type=int, default=1)
    worker_loop_cmd.add_argument("--sleep-seconds", type=float, default=1.0)
    worker_loop_cmd.add_argument("--stop-when-idle", action="store_true")
    worker_loop_cmd.add_argument("--cursor-file")
    worker_loop_cmd.add_argument("--no-cursor", action="store_true")
    worker_loop_cmd.add_argument("--require-session", action="store_true")

    return parser


_SESSION_COMMANDS = frozenset(
    {
        "session-start",
        "session-finish",
        "session-audit",
        "session-resume",
    }
)


def _render_human(response: dict[str, Any], *, command: str = "") -> None:
    if response.get("ok"):
        # Special rendering for intents command
        if command == "intents":
            data = response.get("data", {})
            print(format_timeline_human(data))
            return

        # Special rendering for pr-check, trace, and prune commands
        if command in (
            "pr-check",
            "trace",
            "prune",
            "trace-reqs",
            "drift",
            "coherence",
            "gc",
            "reflections",
            "scan",
            "doctor",
            "config-validate",
            "upgrade",
        ):
            data = response.get("data", {})
            human_summary = data.pop("_human_summary", "")
            if human_summary:
                print(human_summary)
            else:
                print("OK")
                print(json.dumps(data, indent=2, ensure_ascii=True))
            return

        data = response.get("data", {})

        # Print exo governance banner for session lifecycle commands
        if command in _SESSION_COMMANDS:
            banner = data.get("exo_banner") or ""
            if not banner:
                session = data.get("session") or {}
                banner = session.get("exo_banner") or ""
            if banner:
                print(banner)
                print()

        print("OK")
        print(json.dumps(data, indent=2, ensure_ascii=True))
        if response.get("blocked"):
            print("BLOCKED")
        return

    print("ERROR")
    print(json.dumps(response.get("error", {}), indent=2, ensure_ascii=True))


def _render_json(response: dict[str, Any]) -> None:
    print(json.dumps(response, indent=2, ensure_ascii=True, sort_keys=False))


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "data": data,
        "events": [],
        "blocked": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    actor = os.getenv("EXO_ACTOR", "human")
    engine = KernelEngine(repo=args.repo, actor=actor, no_llm=bool(args.no_llm))
    syscalls = KernelSyscalls(root=args.repo, actor=actor)

    try:
        cmd = args.command
        if cmd == "init":
            response = engine.init(seed=bool(args.seed), scan=not bool(args.no_scan))
        elif cmd == "sidecar-init":
            response = engine.sidecar_init(
                branch=args.branch,
                sidecar=args.sidecar,
                remote=args.remote,
                init_git=not bool(args.no_init_git),
                default_branch=args.default_branch,
                fetch_remote=not bool(args.no_fetch_remote),
                commit_migration=not bool(args.no_commit_migration),
            )
        elif cmd == "build-governance":
            response = engine.build_governance()
        elif cmd == "audit":
            response = engine.audit()
        elif cmd == "plan":
            response = engine.plan(args.input)
        elif cmd == "next":
            response = engine.next(
                owner=args.owner,
                role=args.role,
                distributed=bool(args.distributed),
                remote=args.remote,
                duration_hours=args.hours,
            )
        elif cmd == "lease-renew":
            response = engine.lease_renew(
                args.ticket_id,
                owner=args.owner,
                role=args.role,
                duration_hours=args.hours,
                distributed=bool(args.distributed),
                remote=args.remote,
            )
        elif cmd == "lease-heartbeat":
            response = engine.lease_heartbeat(
                args.ticket_id,
                owner=args.owner,
                duration_hours=args.hours,
                distributed=bool(args.distributed),
                remote=args.remote,
            )
        elif cmd == "lease-release":
            response = engine.lease_release(
                args.ticket_id,
                owner=args.owner,
                distributed=bool(args.distributed),
                remote=args.remote,
                ignore_missing=bool(args.ignore_missing),
            )
        elif cmd == "do":
            response = engine.do(args.ticket_id, patch_file=args.patch_file, mark_done=not bool(args.no_mark_done))
        elif cmd == "check":
            response = engine.check(args.ticket_id)
        elif cmd == "status":
            response = engine.status()
        elif cmd == "jot":
            response = engine.jot(args.line)
        elif cmd == "thread":
            response = engine.thread(args.topic)
        elif cmd == "promote":
            response = engine.promote(args.thread_id, to=args.to)
        elif cmd == "recall":
            response = engine.recall(args.query)
        elif cmd == "subscribe":
            topic_id = args.topic_id or default_topic_id(Path(args.repo))
            data = syscalls.subscribe(topic_id=topic_id, since_cursor=args.since_cursor, limit=args.limit)
            response = _ok(data)
        elif cmd == "ack":
            data = syscalls.ack(args.ref_id, actor_cap=args.actor_cap, required=args.required)
            response = _ok(data)
        elif cmd == "quorum":
            response = engine.quorum(args.ref_id, required=args.required)
        elif cmd == "head":
            response = _ok({"topic_id": args.topic_id, "head": syscalls.head(args.topic_id)})
        elif cmd == "cas-head":
            result = syscalls.cas_head(
                topic_id=args.topic_id,
                expected_ref=args.expected_ref,
                new_ref=args.new_ref,
                max_attempts=args.max_attempts,
                control_cap=args.control_cap,
            )
            response = _ok(
                {
                    "topic_id": args.topic_id,
                    "head": result.get("head"),
                    "attempts": result.get("attempts"),
                    "history": result.get("history", []),
                }
            )
        elif cmd == "decide-override":
            decision_id = syscalls.decide_override(
                intent_id=args.intent_id,
                override_cap=args.override_cap,
                rationale_ref=args.rationale_ref,
                outcome=args.outcome,
            )
            response = _ok({"intent_id": args.intent_id, "decision_id": decision_id})
        elif cmd == "policy-set":
            version = syscalls.policy_set(
                policy_cap=args.policy_cap,
                policy_bundle=args.policy_bundle,
                version=args.version,
            )
            response = _ok({"policy_version": version})
        elif cmd == "submit-intent":
            topic_id = args.topic_id or default_topic_id(Path(args.repo))
            intent_id = syscalls.submit(
                {
                    "intent_id": args.intent_id,
                    "intent": args.intent,
                    "topic": topic_id,
                    "ttl_hours": args.ttl_hours,
                    "scope": {
                        "allow": args.scope_allow or ["**"],
                        "deny": args.scope_deny or [],
                    },
                    "action": {
                        "kind": args.action_kind,
                        "target": args.target,
                        "params": {},
                    },
                    "expected_ref": args.expected_ref,
                    "max_attempts": args.max_attempts,
                }
            )
            response = _ok({"intent_id": intent_id, "topic_id": topic_id})
        elif cmd == "check-intent":
            decision_id = syscalls.check(args.intent_id, context_refs=[])
            response = _ok({"intent_id": args.intent_id, "decision_id": decision_id})
        elif cmd == "begin-effect":
            effect_id = syscalls.begin(args.decision_id, executor_ref=args.executor_ref, idem_key=args.idem_key)
            response = _ok({"decision_id": args.decision_id, "effect_id": effect_id})
        elif cmd == "commit-effect":
            syscalls.commit(args.effect_id, status=args.status, artifact_refs=args.artifact_ref)
            response = _ok({"effect_id": args.effect_id, "status": args.status, "artifact_refs": args.artifact_ref})
        elif cmd == "read-ledger":
            records = syscalls.read(
                args.ref_id,
                {
                    "typeFilter": args.type_filter,
                    "sinceCursor": args.since_cursor,
                    "limit": args.limit,
                    "topic_id": args.topic_id,
                    "intent_id": args.intent_id,
                },
            )
            response = _ok({"records": records, "count": len(records)})
        elif cmd == "escalate-intent":
            syscalls.escalate(args.intent_id, args.kind, ctx_refs=args.ctx_ref)
            response = _ok({"intent_id": args.intent_id, "kind": args.kind, "ctx_refs": args.ctx_ref})
        elif cmd == "observe":
            response = engine.observe(
                args.ticket,
                args.tag,
                args.msg,
                triggers=args.trigger,
                confidence=args.confidence,
            )
        elif cmd == "propose":
            response = engine.propose(
                args.ticket,
                args.kind,
                args.symptom,
                args.root_cause,
                summary=args.summary,
                expected_effect=args.expected_effect,
                risk_level=args.risk_level,
                blast_radius=args.blast_radius,
                rollback_type=args.rollback_type,
                rollback_path=args.rollback_path,
                proposed_change_type=args.change_type,
                proposed_change_path=args.change_path,
                evidence_observations=args.evidence_observation,
                evidence_audit_ranges=args.evidence_audit,
                notes=args.note,
                requires_approvals=args.requires_approvals,
                human_required=(True if args.human_required else None),
                patch_file=args.patch_file,
            )
        elif cmd == "approve":
            response = engine.approve(args.proposal_id, decision=args.decision, note=args.note)
        elif cmd == "apply":
            response = engine.apply_proposal(args.proposal_id)
        elif cmd == "distill":
            response = engine.distill(args.proposal_id, statement=args.statement, confidence=args.confidence)
        elif cmd == "session-start":
            manager = AgentSessionManager(args.repo, actor=actor)
            data = manager.start(
                ticket_id=args.ticket_id,
                vendor=args.vendor,
                model=args.model,
                context_window_tokens=args.context_window,
                role=args.role,
                task=args.task,
                topic_id=args.topic_id,
                acquire_lock=bool(args.acquire_lock),
                distributed=bool(args.distributed),
                remote=args.remote,
                duration_hours=args.hours,
            )
            response = _ok(data)
        elif cmd == "session-suspend":
            manager = AgentSessionManager(args.repo, actor=actor)
            data = manager.suspend(
                reason=args.reason,
                ticket_id=args.ticket_id,
                release_lock=not bool(args.no_release_lock),
                stash_changes=bool(args.stash),
            )
            response = _ok(data)
        elif cmd == "session-resume":
            manager = AgentSessionManager(args.repo, actor=actor)
            data = manager.resume(
                ticket_id=args.ticket_id,
                acquire_lock=not bool(args.no_acquire_lock),
                pop_stash=bool(args.pop_stash),
                distributed=bool(args.distributed),
                remote=args.remote,
                duration_hours=args.hours,
                role=args.role,
            )
            response = _ok(data)
        elif cmd == "session-scan":
            data = scan_sessions(args.repo, stale_hours=args.stale_hours)
            response = _ok(data)
        elif cmd == "session-cleanup":
            data = cleanup_sessions(
                args.repo,
                stale_hours=args.stale_hours,
                force=bool(args.force),
                release_lock=bool(args.release_lock),
                actor=actor,
            )
            response = _ok(data)
        elif cmd == "session-finish":
            if bool(args.release_lock) and bool(args.no_release_lock):
                raise ExoError(
                    code="SESSION_RELEASE_FLAGS_CONFLICT",
                    message="Cannot pass both --release-lock and --no-release-lock",
                    blocked=True,
                )
            release_lock: bool | None
            if bool(args.release_lock):
                release_lock = True
            elif bool(args.no_release_lock):
                release_lock = False
            else:
                release_lock = None

            parsed_errors: list[dict[str, Any]] = []
            for err_str in args.errors or []:
                if ":" in err_str:
                    tool, msg = err_str.split(":", 1)
                    parsed_errors.append({"tool": tool.strip(), "message": msg.strip(), "count": 1})
                else:
                    parsed_errors.append({"tool": "unknown", "message": err_str.strip(), "count": 1})

            manager = AgentSessionManager(args.repo, actor=actor)
            data = manager.finish(
                ticket_id=args.ticket_id,
                summary=args.summary,
                set_status=args.set_status,
                skip_check=bool(args.skip_check),
                break_glass_reason=args.break_glass_reason,
                artifacts=args.artifact,
                blockers=args.blocker,
                next_step=args.next_step,
                release_lock=release_lock,
                drift_threshold=args.drift_threshold,
                errors=parsed_errors if parsed_errors else None,
            )
            response = _ok(data)
        elif cmd == "session-audit":
            manager = AgentSessionManager(args.repo, actor=actor)
            data = manager.start(
                ticket_id=args.ticket_id,
                vendor=args.vendor,
                model=args.model,
                context_window_tokens=args.context_window,
                role=args.role,
                task=args.task,
                topic_id=args.topic_id,
                acquire_lock=bool(args.acquire_lock),
                distributed=bool(args.distributed),
                remote=args.remote,
                duration_hours=args.hours,
                mode="audit",
                pr_base=args.pr_base,
                pr_head=args.pr_head,
            )
            response = _ok(data)
        elif cmd == "intents":
            repo_path = Path(args.repo).resolve()
            timeline = build_intent_timeline(repo_path)

            # Apply filters
            if args.status:
                target_status = args.status.strip().lower()
                timeline["intents"] = [
                    i for i in timeline["intents"] if i.get("status", "").strip().lower() == target_status
                ]
            if args.drift_above is not None:
                threshold = float(args.drift_above)
                timeline["intents"] = [
                    i for i in timeline["intents"] if i.get("drift_avg") is not None and i["drift_avg"] > threshold
                ]

            response = _ok(timeline)
        elif cmd == "validate-hierarchy":
            repo_path = Path(args.repo).resolve()
            ticket = load_ticket(repo_path, args.ticket_id)
            reasons = validate_intent_hierarchy(repo_path, ticket)
            response = _ok(
                {
                    "ticket_id": args.ticket_id,
                    "kind": str(ticket.get("kind", "task")),
                    "parent_id": ticket.get("parent_id"),
                    "valid": len(reasons) == 0,
                    "reasons": reasons,
                }
            )
        elif cmd == "intent-create":
            repo_path = Path(args.repo).resolve()
            intent_id = allocate_intent_id(repo_path)
            ticket_data = {
                "id": intent_id,
                "title": args.title,
                "intent": args.title,
                "kind": "intent",
                "brain_dump": args.brain_dump,
                "boundary": args.boundary,
                "success_condition": args.success_condition,
                "risk": args.risk,
                "priority": args.priority,
                "labels": args.label,
                "scope": {
                    "allow": args.scope_allow or ["**"],
                    "deny": args.scope_deny or [],
                },
                "budgets": {
                    "max_files_changed": args.max_files,
                    "max_loc_changed": args.max_loc,
                },
            }
            saved_path = save_ticket(repo_path, ticket_data)
            saved_ticket = normalize_ticket(ticket_data)
            response = _ok(
                {
                    "intent_id": intent_id,
                    "path": str(saved_path.relative_to(repo_path)),
                    "ticket": saved_ticket,
                }
            )
        elif cmd == "ticket-create":
            repo_path = Path(args.repo).resolve()
            # Validate parent exists
            parent = load_ticket(repo_path, args.parent_id)
            parent_kind = str(parent.get("kind", "task")).strip().lower()
            kind = args.kind
            if kind == "task" and parent_kind not in ("intent", "epic"):
                raise ExoError(
                    code="INVALID_PARENT",
                    message=f"task parent must be an intent or epic, got kind={parent_kind}",
                    details={
                        "requested_kind": kind,
                        "parent_id": args.parent_id,
                        "parent_kind": parent_kind,
                        "allowed_parent_kinds": ["intent", "epic"],
                        "hint": "Create an intent first with `exo intent-create`, or use an existing epic as parent.",
                    },
                    blocked=True,
                )
            if kind == "epic" and parent_kind != "intent":
                raise ExoError(
                    code="INVALID_PARENT",
                    message=f"epic parent must be an intent, got kind={parent_kind}",
                    details={
                        "requested_kind": kind,
                        "parent_id": args.parent_id,
                        "parent_kind": parent_kind,
                        "allowed_parent_kinds": ["intent"],
                        "hint": "Epics must be direct children of an intent. Create an intent first with `exo intent-create`.",
                    },
                    blocked=True,
                )
            ticket_id = allocate_ticket_id(repo_path, kind=kind)
            ticket_data = {
                "id": ticket_id,
                "title": args.title,
                "intent": args.title,
                "kind": kind,
                "parent_id": args.parent_id,
                "boundary": args.boundary,
                "success_condition": args.success_condition,
                "priority": args.priority,
                "labels": args.label,
                "scope": {
                    "allow": args.scope_allow or ["**"],
                    "deny": args.scope_deny or [],
                },
                "budgets": {
                    "max_files_changed": args.max_files,
                    "max_loc_changed": args.max_loc,
                },
            }
            saved_path = save_ticket(repo_path, ticket_data)
            saved_ticket = normalize_ticket(ticket_data)
            # Wire child into parent's children list
            if ticket_id not in (parent.get("children") or []):
                children = list(parent.get("children") or [])
                children.append(ticket_id)
                parent["children"] = children
                save_ticket(repo_path, parent)
            response = _ok(
                {
                    "ticket_id": ticket_id,
                    "parent_id": args.parent_id,
                    "path": str(saved_path.relative_to(repo_path)),
                    "ticket": saved_ticket,
                }
            )
        elif cmd == "pr-check":
            repo_path = Path(args.repo).resolve()
            report = pr_check(
                repo_path,
                base_ref=args.base,
                head_ref=args.head,
                drift_threshold=args.drift_threshold,
            )
            data = pr_check_to_dict(report)
            data["_human_summary"] = format_pr_check_human(report)
            response = _ok(data)
        elif cmd == "adapter-generate":
            repo_path = Path(args.repo).resolve()
            targets = args.target if args.target else None
            data = generate_adapters(repo_path, targets=targets, dry_run=bool(args.dry_run))
            response = _ok(data)
        elif cmd == "features":
            repo_path = Path(args.repo).resolve()
            features = load_features(repo_path)
            if args.status:
                target_status = args.status.strip().lower()
                features = [f for f in features if f.status == target_status]
            data: dict[str, Any] = {
                "features": features_to_list(features),
                "count": len(features),
                "scope_deny": generate_scope_deny(features),
            }
            response = _ok(data)
        elif cmd == "trace":
            repo_path = Path(args.repo).resolve()
            globs = args.glob if args.glob else None
            report = trace(
                repo_path,
                globs=globs,
                check_unbound=not bool(args.no_check_unbound),
            )
            data = trace_to_dict(report)
            data["_human_summary"] = format_trace_human(report)
            response = _ok(data)
        elif cmd == "prune":
            repo_path = Path(args.repo).resolve()
            globs = args.glob if args.glob else None
            report = prune(
                repo_path,
                include_deprecated=bool(args.include_deprecated),
                globs=globs,
                dry_run=bool(args.dry_run),
            )
            data = prune_to_dict(report)
            data["_human_summary"] = format_prune_human(report)
            response = _ok(data)
        elif cmd == "requirements":
            repo_path = Path(args.repo).resolve()
            reqs = load_requirements(repo_path)
            if args.status:
                target_status = args.status.strip().lower()
                reqs = [r for r in reqs if r.status == target_status]
            data = {
                "requirements": requirements_to_list(reqs),
                "count": len(reqs),
            }
            response = _ok(data)
        elif cmd == "trace-reqs":
            repo_path = Path(args.repo).resolve()
            globs = args.glob if args.glob else None
            report = trace_requirements(
                repo_path,
                globs=globs,
                check_uncovered=not bool(args.no_check_uncovered),
            )
            data = req_trace_to_dict(report)
            data["_human_summary"] = format_req_trace_human(report)
            response = _ok(data)
        elif cmd == "drift":
            repo_path = Path(args.repo).resolve()
            report = run_drift(
                repo_path,
                stale_hours=float(args.stale_hours),
                skip_adapters=bool(args.skip_adapters),
                skip_features=bool(args.skip_features),
                skip_requirements=bool(args.skip_requirements),
                skip_sessions=bool(args.skip_sessions),
                skip_coherence=bool(args.skip_coherence),
            )
            data = drift_to_dict(report)
            data["_human_summary"] = format_drift_human(report)
            response = _ok(data)
        elif cmd == "coherence":
            from exo.stdlib.coherence import check_coherence, coherence_to_dict, format_coherence_human

            repo_path = Path(args.repo).resolve()
            report = check_coherence(
                repo_path,
                base=args.base,
                skip_co_updates=bool(args.skip_co_updates),
                skip_docstrings=bool(args.skip_docstrings),
            )
            data = coherence_to_dict(report)
            data["_human_summary"] = format_coherence_human(report)
            response = _ok(data)
        elif cmd == "gc":
            repo_path = Path(args.repo).resolve()
            report = run_gc(
                repo_path,
                max_age_days=float(args.max_age_days),
                dry_run=bool(args.dry_run),
            )
            data = gc_to_dict(report)
            data["_human_summary"] = format_gc_human(report)
            response = _ok(data)
        elif cmd == "gc-locks":
            from exo.stdlib.distributed_leases import GitDistributedLeaseManager

            repo_path = Path(args.repo).resolve()
            manager = GitDistributedLeaseManager(repo_path)
            if bool(args.list_only):
                locks = manager.list_locks(remote=args.remote)
                response = _ok(
                    {
                        "remote": args.remote,
                        "locks": locks,
                        "count": len(locks),
                    }
                )
            else:
                data = manager.cleanup_locks(
                    remote=args.remote,
                    dry_run=bool(args.dry_run),
                )
                response = _ok(data)
        elif cmd == "reflect":
            repo_path = Path(args.repo).resolve()
            session_id = args.session_id
            if not session_id:
                mgr = AgentSessionManager(repo_path, actor=actor)
                active = mgr.get_active()
                if active:
                    session_id = str(active.get("session_id", ""))
            reflection = do_reflect(
                repo_path,
                pattern=args.pattern,
                insight=args.insight,
                severity=args.severity,
                scope=args.scope,
                actor=actor,
                session_id=session_id,
                tags=args.tag,
            )
            response = _ok(reflect_to_dict(reflection))
        elif cmd == "reflections":
            repo_path = Path(args.repo).resolve()
            refs = load_reflections(
                repo_path,
                status=args.status,
                scope=args.scope,
            )
            if args.severity:
                target_sev = args.severity.strip().lower()
                refs = [r for r in refs if r.severity == target_sev]
            data: dict[str, Any] = {
                "reflections": reflections_to_list(refs),
                "count": len(refs),
            }
            data["_human_summary"] = format_reflections_human(refs)
            response = _ok(data)
        elif cmd == "reflect-dismiss":
            repo_path = Path(args.repo).resolve()
            ref = dismiss_reflection(repo_path, args.reflection_id)
            response = _ok(reflect_to_dict(ref))
        elif cmd == "scan":
            repo_path = Path(args.repo).resolve()
            from exo.stdlib.scan import format_scan_human as fmt_scan
            from exo.stdlib.scan import scan_repo, scan_to_dict

            report = scan_repo(repo_path)
            data = scan_to_dict(report)
            data["_human_summary"] = fmt_scan(report)
            response = _ok(data)
        elif cmd == "doctor":
            repo_path = Path(args.repo).resolve()
            from exo.stdlib.doctor import doctor as run_doctor
            from exo.stdlib.doctor import doctor_to_dict, format_doctor_human

            report = run_doctor(repo_path, stale_hours=float(args.stale_hours))
            data = doctor_to_dict(report)
            data["_human_summary"] = format_doctor_human(report)
            response = _ok(data)
        elif cmd == "config-validate":
            repo_path = Path(args.repo).resolve()
            from exo.stdlib.config_schema import format_validation_human, validate_config, validation_to_dict

            result = validate_config(repo_path)
            data = validation_to_dict(result)
            data["_human_summary"] = format_validation_human(result)
            response = _ok(data)
        elif cmd == "upgrade":
            repo_path = Path(args.repo).resolve()
            from exo.stdlib.upgrade import format_upgrade_human
            from exo.stdlib.upgrade import upgrade as run_upgrade

            data = run_upgrade(repo_path, dry_run=bool(args.dry_run))
            data["_human_summary"] = format_upgrade_human(data)
            response = _ok(data)
        elif cmd == "worker-poll":
            worker = DistributedWorker(
                args.repo,
                actor=actor,
                topic_id=args.topic_id,
                cursor_path=args.cursor_file,
                use_cursor=not bool(args.no_cursor),
                require_session=bool(args.require_session),
            )
            data = worker.poll_once(
                since_cursor=args.since_cursor,
                limit=args.limit,
                persist_cursor=not bool(args.no_cursor),
            )
            response = _ok(data)
        elif cmd == "worker-loop":
            worker = DistributedWorker(
                args.repo,
                actor=actor,
                topic_id=args.topic_id,
                cursor_path=args.cursor_file,
                use_cursor=not bool(args.no_cursor),
                require_session=bool(args.require_session),
            )
            loop_iterations = args.iterations if args.iterations > 0 else None
            data = worker.run_loop(
                iterations=loop_iterations,
                sleep_seconds=float(args.sleep_seconds),
                since_cursor=args.since_cursor,
                limit=args.limit,
                persist_cursor=not bool(args.no_cursor),
                stop_when_idle=bool(args.stop_when_idle),
            )
            response = _ok(data)
        else:
            raise ExoError(code="CMD_UNKNOWN", message=f"Unknown command: {cmd}")

    except ExoError as err:
        response = {
            "ok": False,
            "error": err.to_dict(),
            "events": engine.events,
            "blocked": err.blocked,
        }
    except Exception as exc:  # noqa: BLE001
        response = {
            "ok": False,
            "error": {
                "code": "UNHANDLED_EXCEPTION",
                "message": str(exc),
                "details": {},
                "blocked": False,
            },
            "events": engine.events,
            "blocked": False,
        }

    if args.format == "json":
        _render_json(response)
    else:
        _render_human(response, command=cmd)

    if response.get("ok") and not response.get("blocked"):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
