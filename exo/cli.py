from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from exo.control.syscalls import KernelSyscalls
from exo.kernel.errors import ExoError
from exo.stdlib.engine import KernelEngine


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="exo", description="ExoProtocol Kernel CLI")
    parser.add_argument("--format", choices=["human", "json"], default="human")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--no-llm", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    init_cmd = sub.add_parser("init", help="Create .exo scaffold")
    init_cmd.add_argument("--seed", action="store_true", help="Seed starter SPEC + first kernel tickets")

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

    renew_cmd = sub.add_parser("lease-renew", help="Renew active ticket lease and increment fencing token")
    renew_cmd.add_argument("--ticket-id")
    renew_cmd.add_argument("--owner")
    renew_cmd.add_argument("--role")
    renew_cmd.add_argument("--hours", type=int, default=2)

    heartbeat_cmd = sub.add_parser("lease-heartbeat", help="Heartbeat active ticket lease without changing fencing token")
    heartbeat_cmd.add_argument("--ticket-id")
    heartbeat_cmd.add_argument("--owner")
    heartbeat_cmd.add_argument("--hours", type=int, default=2)

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
    propose_cmd.add_argument("--kind", required=True, choices=["practice_change", "governance_change", "tooling_change"])
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

    return parser


def _render_human(response: dict[str, Any]) -> None:
    if response.get("ok"):
        print("OK")
        print(json.dumps(response.get("data", {}), indent=2, ensure_ascii=True))
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
            response = engine.init(seed=bool(args.seed))
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
            response = engine.next(owner=args.owner, role=args.role)
        elif cmd == "lease-renew":
            response = engine.lease_renew(
                args.ticket_id,
                owner=args.owner,
                role=args.role,
                duration_hours=args.hours,
            )
        elif cmd == "lease-heartbeat":
            response = engine.lease_heartbeat(
                args.ticket_id,
                owner=args.owner,
                duration_hours=args.hours,
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
            topic_id = args.topic_id or f"repo:{Path(args.repo).resolve().as_posix()}"
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
            topic_id = args.topic_id or f"repo:{Path(args.repo).resolve().as_posix()}"
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
        _render_human(response)

    if response.get("ok") and not response.get("blocked"):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
