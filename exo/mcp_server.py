from __future__ import annotations

from pathlib import Path
from typing import Any

from exo.control.syscalls import KernelSyscalls
from exo.kernel.errors import ExoError
from exo.kernel.utils import default_topic_id
from exo.kernel.tickets import (
    allocate_intent_id,
    allocate_ticket_id,
    load_ticket,
    normalize_ticket,
    save_ticket,
    validate_intent_hierarchy,
)
from exo.orchestrator import AgentSessionManager, DistributedWorker, cleanup_sessions, scan_sessions
from exo.stdlib.adapters import generate_adapters
from exo.stdlib.engine import KernelEngine
from exo.stdlib.features import (
    load_features,
    features_to_list,
    generate_scope_deny,
    trace as run_trace,
    trace_to_dict,
    prune as run_prune,
    prune_to_dict,
)
from exo.stdlib.drift import drift as run_drift, drift_to_dict
from exo.stdlib.gc import gc as run_gc, gc_to_dict
from exo.stdlib.reflect import (
    reflect as do_reflect,
    load_reflections,
    dismiss_reflection,
    reflect_to_dict,
    reflections_to_list,
)
from exo.stdlib.requirements import (
    load_requirements,
    requirements_to_list,
    trace_requirements as run_trace_reqs,
    req_trace_to_dict,
)
from exo.stdlib.pr_check import pr_check, pr_check_to_dict
from exo.stdlib.timeline import build_intent_timeline

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # noqa: BLE001
    FastMCP = None  # type: ignore[assignment]


def _run(repo: str, method: str, **kwargs: Any) -> dict[str, Any]:
    engine = KernelEngine(repo=Path(repo).resolve(), actor="agent:mcp")
    try:
        fn = getattr(engine, method)
        return fn(**kwargs)
    except ExoError as err:
        return {
            "ok": False,
            "error": err.to_dict(),
            "events": engine.events,
            "blocked": err.blocked,
        }
    except Exception as exc:  # noqa: BLE001
        return {
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


def _run_syscall(repo: str, method: str, *, data_key: str = "value", **kwargs: Any) -> dict[str, Any]:
    api = KernelSyscalls(root=Path(repo).resolve(), actor="agent:mcp")
    try:
        fn = getattr(api, method)
        value = fn(**kwargs)
        if isinstance(value, dict):
            data: dict[str, Any] = value
        elif value is None:
            data = {}
        else:
            data = {data_key: value}
        return {
            "ok": True,
            "data": data,
            "events": [],
            "blocked": False,
        }
    except ExoError as err:
        return {
            "ok": False,
            "error": err.to_dict(),
            "events": [],
            "blocked": err.blocked,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": {
                "code": "UNHANDLED_EXCEPTION",
                "message": str(exc),
                "details": {},
                "blocked": False,
            },
            "events": [],
            "blocked": False,
        }


if FastMCP:
    mcp = FastMCP("exo-kernel")

    @mcp.tool()
    def exo_status(repo: str = ".") -> dict[str, Any]:
        return _run(repo, "status")

    @mcp.tool()
    def exo_plan(input: str, repo: str = ".") -> dict[str, Any]:
        return _run(repo, "plan", input_value=input)

    @mcp.tool()
    def exo_next(repo: str = ".", owner: str = "agent:mcp", role: str = "developer") -> dict[str, Any]:
        return _run(repo, "next", owner=owner, role=role)

    @mcp.tool()
    def exo_do(ticket_id: str | None = None, repo: str = ".") -> dict[str, Any]:
        return _run(repo, "do", ticket_id=ticket_id)

    @mcp.tool()
    def exo_check(ticket_id: str | None = None, repo: str = ".") -> dict[str, Any]:
        return _run(repo, "check", ticket_id=ticket_id)

    @mcp.tool()
    def exo_jot(content: str, repo: str = ".") -> dict[str, Any]:
        return _run(repo, "jot", line=content)

    @mcp.tool()
    def exo_thread(topic: str, repo: str = ".") -> dict[str, Any]:
        return _run(repo, "thread", topic=topic)

    @mcp.tool()
    def exo_promote(thread_id: str, repo: str = ".") -> dict[str, Any]:
        return _run(repo, "promote", thread_id=thread_id, to="ticket")

    @mcp.tool()
    def exo_recall(query: str, repo: str = ".") -> dict[str, Any]:
        return _run(repo, "recall", query=query)

    @mcp.tool()
    def exo_subscribe(
        repo: str = ".",
        topic_id: str | None = None,
        since_cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        resolved_topic = topic_id or default_topic_id(Path(repo))
        return _run_syscall(
            repo,
            "subscribe",
            topic_id=resolved_topic,
            since_cursor=since_cursor,
            limit=limit,
        )

    @mcp.tool()
    def exo_ack(ref_id: str, repo: str = ".", required: int = 1, actor_cap: str = "cap:ack") -> dict[str, Any]:
        return _run_syscall(repo, "ack", ref_id=ref_id, required=required, actor_cap=actor_cap)

    @mcp.tool()
    def exo_quorum(ref_id: str, repo: str = ".", required: int = 1) -> dict[str, Any]:
        return _run(repo, "quorum", ref_id=ref_id, required=required)

    @mcp.tool()
    def exo_head(topic_id: str, repo: str = ".") -> dict[str, Any]:
        response = _run_syscall(repo, "head", topic_id=topic_id, data_key="head")
        if response.get("ok"):
            data = response.get("data", {})
            if isinstance(data, dict):
                data["topic_id"] = topic_id
        return response

    @mcp.tool()
    def exo_cas_head(
        topic_id: str,
        control_cap: str,
        repo: str = ".",
        expected_ref: str | None = None,
        new_ref: str | None = None,
        max_attempts: int = 1,
    ) -> dict[str, Any]:
        response = _run_syscall(
            repo,
            "cas_head",
            topic_id=topic_id,
            control_cap=control_cap,
            expected_ref=expected_ref,
            new_ref=new_ref,
            max_attempts=max_attempts,
        )
        if response.get("ok"):
            data = response.get("data", {})
            if isinstance(data, dict):
                data["topic_id"] = topic_id
        return response

    @mcp.tool()
    def exo_decide_override(
        intent_id: str,
        override_cap: str,
        rationale_ref: str,
        repo: str = ".",
        outcome: str = "ALLOW",
    ) -> dict[str, Any]:
        response = _run_syscall(
            repo,
            "decide_override",
            intent_id=intent_id,
            override_cap=override_cap,
            rationale_ref=rationale_ref,
            outcome=outcome,
            data_key="decision_id",
        )
        if response.get("ok"):
            data = response.get("data", {})
            if isinstance(data, dict):
                data["intent_id"] = intent_id
        return response

    @mcp.tool()
    def exo_policy_set(
        policy_cap: str,
        repo: str = ".",
        policy_bundle: str | None = None,
        version: str | None = None,
    ) -> dict[str, Any]:
        return _run_syscall(
            repo,
            "policy_set",
            policy_cap=policy_cap,
            policy_bundle=policy_bundle,
            version=version,
            data_key="policy_version",
        )

    @mcp.tool()
    def exo_submit(
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
    ) -> dict[str, Any]:
        resolved_topic = topic_id or default_topic_id(Path(repo))
        envelope = {
            "intent_id": intent_id,
            "intent": intent,
            "topic": resolved_topic,
            "ttl_hours": ttl_hours,
            "scope": {
                "allow": scope_allow or ["**"],
                "deny": scope_deny or [],
            },
            "action": {
                "kind": action_kind,
                "target": target,
                "params": {},
            },
            "expected_ref": expected_ref,
            "max_attempts": max_attempts,
        }
        response = _run_syscall(repo, "submit", intent_envelope=envelope, data_key="intent_id")
        if response.get("ok"):
            data = response.get("data", {})
            if isinstance(data, dict):
                data["topic_id"] = resolved_topic
        return response

    @mcp.tool()
    def exo_check_intent(intent_id: str, repo: str = ".", context_refs: list[str] | None = None) -> dict[str, Any]:
        response = _run_syscall(
            repo, "check", intent_id=intent_id, context_refs=context_refs or [], data_key="decision_id"
        )
        if response.get("ok"):
            data = response.get("data", {})
            if isinstance(data, dict):
                data["intent_id"] = intent_id
        return response

    @mcp.tool()
    def exo_begin(decision_id: str, executor_ref: str, idem_key: str, repo: str = ".") -> dict[str, Any]:
        response = _run_syscall(
            repo,
            "begin",
            decision_id=decision_id,
            executor_ref=executor_ref,
            idem_key=idem_key,
            data_key="effect_id",
        )
        if response.get("ok"):
            data = response.get("data", {})
            if isinstance(data, dict):
                data["decision_id"] = decision_id
        return response

    @mcp.tool()
    def exo_commit(
        effect_id: str,
        status: str,
        repo: str = ".",
        artifact_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        response = _run_syscall(
            repo,
            "commit",
            effect_id=effect_id,
            status=status,
            artifact_refs=artifact_refs or [],
        )
        if response.get("ok"):
            response["data"] = {
                "effect_id": effect_id,
                "status": status,
                "artifact_refs": artifact_refs or [],
            }
        return response

    @mcp.tool()
    def exo_read(
        repo: str = ".",
        ref_id: str | None = None,
        type_filter: str | None = None,
        since_cursor: str | None = None,
        limit: int = 200,
        topic_id: str | None = None,
        intent_id: str | None = None,
    ) -> dict[str, Any]:
        response = _run_syscall(
            repo,
            "read",
            ref_id=ref_id,
            selector={
                "typeFilter": type_filter,
                "sinceCursor": since_cursor,
                "limit": limit,
                "topic_id": topic_id,
                "intent_id": intent_id,
            },
            data_key="records",
        )
        if response.get("ok"):
            data = response.get("data", {})
            if isinstance(data, dict):
                records = data.get("records")
                if isinstance(records, list):
                    data["count"] = len(records)
        return response

    @mcp.tool()
    def exo_escalate(intent_id: str, kind: str, repo: str = ".", ctx_refs: list[str] | None = None) -> dict[str, Any]:
        response = _run_syscall(repo, "escalate", intent_id=intent_id, kind=kind, ctx_refs=ctx_refs or [])
        if response.get("ok"):
            response["data"] = {"intent_id": intent_id, "kind": kind, "ctx_refs": ctx_refs or []}
        return response

    @mcp.tool()
    def exo_worker_poll(
        repo: str = ".",
        topic_id: str | None = None,
        since_cursor: str | None = None,
        limit: int = 100,
        cursor_file: str | None = None,
        use_cursor: bool = True,
        require_session: bool = False,
    ) -> dict[str, Any]:
        worker = DistributedWorker(
            Path(repo).resolve(),
            actor="agent:mcp",
            topic_id=topic_id,
            cursor_path=cursor_file,
            use_cursor=use_cursor,
            require_session=require_session,
        )
        try:
            data = worker.poll_once(
                since_cursor=since_cursor,
                limit=limit,
                persist_cursor=use_cursor,
            )
            return {
                "ok": True,
                "data": data,
                "events": [],
                "blocked": False,
            }
        except ExoError as err:
            return {
                "ok": False,
                "error": err.to_dict(),
                "events": [],
                "blocked": err.blocked,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {
                    "code": "UNHANDLED_EXCEPTION",
                    "message": str(exc),
                    "details": {},
                    "blocked": False,
                },
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_session_start(
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
    ) -> dict[str, Any]:
        manager = AgentSessionManager(Path(repo).resolve(), actor="agent:mcp")
        try:
            data = manager.start(
                ticket_id=ticket_id,
                vendor=vendor,
                model=model,
                context_window_tokens=context_window_tokens,
                role=role,
                task=task,
                topic_id=topic_id,
                acquire_lock=acquire_lock,
                distributed=distributed,
                remote=remote,
                duration_hours=duration_hours,
            )
            return {
                "ok": True,
                "data": data,
                "events": [],
                "blocked": False,
            }
        except ExoError as err:
            return {
                "ok": False,
                "error": err.to_dict(),
                "events": [],
                "blocked": err.blocked,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {
                    "code": "UNHANDLED_EXCEPTION",
                    "message": str(exc),
                    "details": {},
                    "blocked": False,
                },
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_session_audit(
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
    ) -> dict[str, Any]:
        """Start an isolated audit session with context isolation, adversarial persona, and model-mismatch detection.

        When pr_base and pr_head are provided, automatically runs pr-check and injects
        the governance report into the audit bootstrap prompt for PR review.
        """
        manager = AgentSessionManager(Path(repo).resolve(), actor="agent:mcp")
        try:
            data = manager.start(
                ticket_id=ticket_id,
                vendor=vendor,
                model=model,
                context_window_tokens=context_window_tokens,
                role=role,
                task=task,
                topic_id=topic_id,
                acquire_lock=acquire_lock,
                distributed=distributed,
                remote=remote,
                duration_hours=duration_hours,
                mode="audit",
                pr_base=pr_base,
                pr_head=pr_head,
            )
            return {"ok": True, "data": data, "events": [], "blocked": False}
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_session_suspend(
        reason: str,
        repo: str = ".",
        ticket_id: str | None = None,
        release_lock: bool = True,
        stash_changes: bool = False,
    ) -> dict[str, Any]:
        manager = AgentSessionManager(Path(repo).resolve(), actor="agent:mcp")
        try:
            data = manager.suspend(
                reason=reason,
                ticket_id=ticket_id,
                release_lock=release_lock,
                stash_changes=stash_changes,
            )
            return {
                "ok": True,
                "data": data,
                "events": [],
                "blocked": False,
            }
        except ExoError as err:
            return {
                "ok": False,
                "error": err.to_dict(),
                "events": [],
                "blocked": err.blocked,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {
                    "code": "UNHANDLED_EXCEPTION",
                    "message": str(exc),
                    "details": {},
                    "blocked": False,
                },
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_session_resume(
        repo: str = ".",
        ticket_id: str | None = None,
        acquire_lock: bool = True,
        pop_stash: bool = False,
        distributed: bool = False,
        remote: str = "origin",
        duration_hours: int = 2,
        role: str | None = None,
    ) -> dict[str, Any]:
        manager = AgentSessionManager(Path(repo).resolve(), actor="agent:mcp")
        try:
            data = manager.resume(
                ticket_id=ticket_id,
                acquire_lock=acquire_lock,
                pop_stash=pop_stash,
                distributed=distributed,
                remote=remote,
                duration_hours=duration_hours,
                role=role,
            )
            return {
                "ok": True,
                "data": data,
                "events": [],
                "blocked": False,
            }
        except ExoError as err:
            return {
                "ok": False,
                "error": err.to_dict(),
                "events": [],
                "blocked": err.blocked,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {
                    "code": "UNHANDLED_EXCEPTION",
                    "message": str(exc),
                    "details": {},
                    "blocked": False,
                },
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_session_scan(repo: str = ".", stale_hours: float = 48.0) -> dict[str, Any]:
        try:
            data = scan_sessions(Path(repo).resolve(), stale_hours=stale_hours)
            return {"ok": True, "data": data, "events": [], "blocked": False}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc), "details": {}, "blocked": False},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_session_cleanup(
        repo: str = ".",
        stale_hours: float = 48.0,
        force: bool = False,
        release_lock: bool = False,
    ) -> dict[str, Any]:
        try:
            data = cleanup_sessions(
                Path(repo).resolve(),
                stale_hours=stale_hours,
                force=force,
                release_lock=release_lock,
                actor="agent:mcp",
            )
            return {"ok": True, "data": data, "events": [], "blocked": False}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc), "details": {}, "blocked": False},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_session_finish(
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
    ) -> dict[str, Any]:
        manager = AgentSessionManager(Path(repo).resolve(), actor="agent:mcp")
        try:
            data = manager.finish(
                summary=summary,
                ticket_id=ticket_id,
                set_status=set_status,
                skip_check=skip_check,
                break_glass_reason=break_glass_reason,
                artifacts=artifacts or [],
                blockers=blockers or [],
                next_step=next_step,
                release_lock=release_lock,
                drift_threshold=drift_threshold,
                errors=errors,
            )
            return {
                "ok": True,
                "data": data,
                "events": [],
                "blocked": False,
            }
        except ExoError as err:
            return {
                "ok": False,
                "error": err.to_dict(),
                "events": [],
                "blocked": err.blocked,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {
                    "code": "UNHANDLED_EXCEPTION",
                    "message": str(exc),
                    "details": {},
                    "blocked": False,
                },
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_observe(
        ticket_id: str,
        tag: str,
        msg: str,
        repo: str = ".",
        triggers: list[str] | None = None,
        confidence: str = "high",
    ) -> dict[str, Any]:
        return _run(
            repo,
            "observe",
            ticket_id=ticket_id,
            tag=tag,
            msg=msg,
            triggers=triggers or [],
            confidence=confidence,
        )

    @mcp.tool()
    def exo_propose(
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
    ) -> dict[str, Any]:
        return _run(
            repo,
            "propose",
            ticket_id=ticket_id,
            kind=kind,
            symptom=symptom,
            root_cause=root_cause,
            summary=summary,
            expected_effect=expected_effect,
            risk_level=risk_level,
            blast_radius=blast_radius or [],
            rollback_type=rollback_type,
            rollback_path=rollback_path,
            proposed_change_type=proposed_change_type,
            proposed_change_path=proposed_change_path,
            evidence_observations=evidence_observations or [],
            evidence_audit_ranges=evidence_audit_ranges or [],
            notes=notes or [],
            requires_approvals=requires_approvals,
            human_required=human_required,
            patch_file=patch_file,
        )

    @mcp.tool()
    def exo_approve(proposal_id: str, repo: str = ".", decision: str = "approved", note: str = "") -> dict[str, Any]:
        return _run(repo, "approve", proposal_id=proposal_id, decision=decision, note=note)

    @mcp.tool()
    def exo_apply(proposal_id: str, repo: str = ".") -> dict[str, Any]:
        return _run(repo, "apply_proposal", proposal_id=proposal_id)

    @mcp.tool()
    def exo_distill(
        proposal_id: str, repo: str = ".", statement: str | None = None, confidence: float = 0.7
    ) -> dict[str, Any]:
        return _run(repo, "distill", proposal_id=proposal_id, statement=statement, confidence=confidence)

    @mcp.tool()
    def exo_intents(
        repo: str = ".",
        status: str | None = None,
        drift_above: float | None = None,
    ) -> dict[str, Any]:
        try:
            repo_path = Path(repo).resolve()
            timeline = build_intent_timeline(repo_path)
            if status:
                target = status.strip().lower()
                timeline["intents"] = [i for i in timeline["intents"] if i.get("status", "").strip().lower() == target]
            if drift_above is not None:
                timeline["intents"] = [
                    i for i in timeline["intents"] if i.get("drift_avg") is not None and i["drift_avg"] > drift_above
                ]
            return {"ok": True, "data": timeline, "events": [], "blocked": False}
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_validate_hierarchy(ticket_id: str, repo: str = ".") -> dict[str, Any]:
        try:
            repo_path = Path(repo).resolve()
            ticket = load_ticket(repo_path, ticket_id)
            reasons = validate_intent_hierarchy(repo_path, ticket)
            return {
                "ok": True,
                "data": {
                    "ticket_id": ticket_id,
                    "kind": str(ticket.get("kind", "task")),
                    "parent_id": ticket.get("parent_id"),
                    "valid": len(reasons) == 0,
                    "reasons": reasons,
                },
                "events": [],
                "blocked": False,
            }
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_intent_create(
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
    ) -> dict[str, Any]:
        try:
            repo_path = Path(repo).resolve()
            intent_id = allocate_intent_id(repo_path)
            ticket_data = {
                "id": intent_id,
                "title": title,
                "intent": title,
                "kind": "intent",
                "brain_dump": brain_dump,
                "boundary": boundary,
                "success_condition": success_condition,
                "risk": risk,
                "priority": priority,
                "labels": labels or [],
                "scope": {"allow": scope_allow or ["**"], "deny": scope_deny or []},
                "budgets": {"max_files_changed": max_files, "max_loc_changed": max_loc},
            }
            saved_path = save_ticket(repo_path, ticket_data)
            saved_ticket = normalize_ticket(ticket_data)
            return {
                "ok": True,
                "data": {
                    "intent_id": intent_id,
                    "path": str(saved_path.relative_to(repo_path)),
                    "ticket": saved_ticket,
                },
                "events": [],
                "blocked": False,
            }
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_ticket_create(
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
    ) -> dict[str, Any]:
        try:
            repo_path = Path(repo).resolve()
            parent = load_ticket(repo_path, parent_id)
            parent_kind = str(parent.get("kind", "task")).strip().lower()
            if kind == "task" and parent_kind not in ("intent", "epic"):
                raise ExoError(
                    code="INVALID_PARENT",
                    message=f"task parent must be intent or epic, got {parent_kind}",
                    details={
                        "requested_kind": kind,
                        "parent_id": parent_id,
                        "parent_kind": parent_kind,
                        "allowed_parent_kinds": ["intent", "epic"],
                        "hint": "Create an intent first with exo_intent_create, or use an existing epic as parent.",
                    },
                    blocked=True,
                )
            if kind == "epic" and parent_kind != "intent":
                raise ExoError(
                    code="INVALID_PARENT",
                    message=f"epic parent must be intent, got {parent_kind}",
                    details={
                        "requested_kind": kind,
                        "parent_id": parent_id,
                        "parent_kind": parent_kind,
                        "allowed_parent_kinds": ["intent"],
                        "hint": "Epics must be direct children of an intent. Create an intent first with exo_intent_create.",
                    },
                    blocked=True,
                )
            ticket_id = allocate_ticket_id(repo_path, kind=kind)
            ticket_data = {
                "id": ticket_id,
                "title": title,
                "intent": title,
                "kind": kind,
                "parent_id": parent_id,
                "boundary": boundary,
                "success_condition": success_condition,
                "priority": priority,
                "labels": labels or [],
                "scope": {"allow": scope_allow or ["**"], "deny": scope_deny or []},
                "budgets": {"max_files_changed": max_files, "max_loc_changed": max_loc},
            }
            saved_path = save_ticket(repo_path, ticket_data)
            saved_ticket = normalize_ticket(ticket_data)
            if ticket_id not in (parent.get("children") or []):
                children = list(parent.get("children") or [])
                children.append(ticket_id)
                parent["children"] = children
                save_ticket(repo_path, parent)
            return {
                "ok": True,
                "data": {
                    "ticket_id": ticket_id,
                    "parent_id": parent_id,
                    "path": str(saved_path.relative_to(repo_path)),
                    "ticket": saved_ticket,
                },
                "events": [],
                "blocked": False,
            }
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_pr_check(
        repo: str = ".",
        base_ref: str = "main",
        head_ref: str = "HEAD",
        drift_threshold: float = 0.7,
    ) -> dict[str, Any]:
        """Check governance compliance for all commits in a PR range.

        Matches commits to governed sessions by timestamp, checks scope
        coverage, drift scores, and governance integrity. Returns structured
        verdict: pass / warn / fail.
        """
        try:
            report = pr_check(
                Path(repo).resolve(),
                base_ref=base_ref,
                head_ref=head_ref,
                drift_threshold=drift_threshold,
            )
            return {"ok": True, "data": pr_check_to_dict(report), "events": [], "blocked": False}
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_adapter_generate(
        repo: str = ".",
        targets: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Generate repo-root agent config files (CLAUDE.md, .cursorrules, AGENTS.md) from governance state."""
        try:
            data = generate_adapters(Path(repo).resolve(), targets=targets, dry_run=dry_run)
            return {"ok": True, "data": data, "events": [], "blocked": False}
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_features(
        repo: str = ".",
        status: str | None = None,
    ) -> dict[str, Any]:
        """List feature definitions from .exo/features.yaml with optional status filter."""
        try:
            repo_path = Path(repo).resolve()
            features = load_features(repo_path)
            if status:
                target = status.strip().lower()
                features = [f for f in features if f.status == target]
            return {
                "ok": True,
                "data": {
                    "features": features_to_list(features),
                    "count": len(features),
                    "scope_deny": generate_scope_deny(features),
                },
                "events": [],
                "blocked": False,
            }
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_trace(
        repo: str = ".",
        globs: list[str] | None = None,
        check_unbound: bool = True,
    ) -> dict[str, Any]:
        """Run feature traceability linter: cross-reference @feature: tags against manifest.

        Checks for invalid tags, deprecated/deleted usage, locked edits,
        and unbound features. Returns structured report with pass/fail verdict.
        """
        try:
            report = run_trace(
                Path(repo).resolve(),
                globs=globs,
                check_unbound=check_unbound,
            )
            return {"ok": True, "data": trace_to_dict(report), "events": [], "blocked": False}
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_prune(
        repo: str = ".",
        include_deprecated: bool = False,
        globs: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Remove code blocks tagged with deleted (or deprecated) features.

        Scans for @feature: / @endfeature blocks referencing features with
        'deleted' status and removes them. Use include_deprecated=True to
        also remove deprecated feature code. Use dry_run=True to preview.
        """
        try:
            report = run_prune(
                Path(repo).resolve(),
                include_deprecated=include_deprecated,
                globs=globs,
                dry_run=dry_run,
            )
            return {"ok": True, "data": prune_to_dict(report), "events": [], "blocked": False}
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_requirements(
        repo: str = ".",
        status: str | None = None,
    ) -> dict[str, Any]:
        """List requirement definitions from .exo/requirements.yaml with optional status filter."""
        try:
            repo_path = Path(repo).resolve()
            reqs = load_requirements(repo_path)
            if status:
                target = status.strip().lower()
                reqs = [r for r in reqs if r.status == target]
            return {
                "ok": True,
                "data": {
                    "requirements": requirements_to_list(reqs),
                    "count": len(reqs),
                },
                "events": [],
                "blocked": False,
            }
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_trace_reqs(
        repo: str = ".",
        globs: list[str] | None = None,
        check_uncovered: bool = True,
    ) -> dict[str, Any]:
        """Run requirement traceability linter: cross-reference @req: annotations against manifest.

        Checks for orphan references, deprecated/deleted usage, and uncovered
        requirements. Returns structured report with pass/fail verdict.
        """
        try:
            report = run_trace_reqs(
                Path(repo).resolve(),
                globs=globs,
                check_uncovered=check_uncovered,
            )
            return {"ok": True, "data": req_trace_to_dict(report), "events": [], "blocked": False}
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_drift(
        repo: str = ".",
        stale_hours: float = 48.0,
        skip_adapters: bool = False,
        skip_features: bool = False,
        skip_requirements: bool = False,
        skip_sessions: bool = False,
        skip_coherence: bool = False,
    ) -> dict[str, Any]:
        """Run composite governance drift check across all subsystems.

        Checks governance integrity, adapter freshness, feature traceability,
        requirement traceability, session health, and coherence. Returns unified
        pass/fail verdict with per-subsystem details.
        """
        try:
            report = run_drift(
                Path(repo).resolve(),
                stale_hours=stale_hours,
                skip_adapters=skip_adapters,
                skip_features=skip_features,
                skip_requirements=skip_requirements,
                skip_sessions=skip_sessions,
                skip_coherence=skip_coherence,
            )
            return {"ok": True, "data": drift_to_dict(report), "events": [], "blocked": False}
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_coherence(
        repo: str = ".",
        base: str = "main",
        skip_co_updates: bool = False,
        skip_docstrings: bool = False,
    ) -> dict[str, Any]:
        """Check semantic coherence: co-update rules and docstring freshness.

        Detects when code changes without corresponding documentation updates.
        Co-update rules enforce that related files change together. Docstring
        freshness flags functions whose body changed but docstring didn't.
        """
        try:
            from exo.stdlib.coherence import check_coherence, coherence_to_dict

            report = check_coherence(
                Path(repo).resolve(),
                base=base,
                skip_co_updates=skip_co_updates,
                skip_docstrings=skip_docstrings,
            )
            return {"ok": True, "data": coherence_to_dict(report), "events": [], "blocked": False}
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_gc(
        repo: str = ".",
        max_age_days: float = 30.0,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Garbage-collect old mementos, cursors, and bootstraps.

        Scans .exo/memory/sessions/ for old memento files, .exo/cache/orchestrator/
        for orphaned cursors, and .exo/cache/sessions/ for leftover bootstraps.
        Also compacts the session index JSONL.
        """
        try:
            report = run_gc(
                Path(repo).resolve(),
                max_age_days=max_age_days,
                dry_run=dry_run,
            )
            return {"ok": True, "data": gc_to_dict(report), "events": [], "blocked": False}
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_gc_locks(
        repo: str = ".",
        remote: str = "origin",
        dry_run: bool = False,
        list_only: bool = False,
    ) -> dict[str, Any]:
        """Clean up expired distributed locks on a git remote.

        Scans refs/exoprotocol/locks/* on the remote, identifies expired
        leases, and deletes their refs. Use list_only=True to inspect
        without cleaning, or dry_run=True to preview cleanup.
        """
        from exo.stdlib.distributed_leases import GitDistributedLeaseManager

        try:
            manager = GitDistributedLeaseManager(Path(repo).resolve())
            if list_only:
                locks = manager.list_locks(remote=remote)
                return {
                    "ok": True,
                    "data": {"remote": remote, "locks": locks, "count": len(locks)},
                    "events": [],
                    "blocked": False,
                }
            data = manager.cleanup_locks(remote=remote, dry_run=dry_run)
            return {"ok": True, "data": data, "events": [], "blocked": False}
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_reflect(
        pattern: str,
        insight: str,
        repo: str = ".",
        severity: str = "medium",
        scope: str = "global",
        tags: list[str] | None = None,
        session_id: str = "",
    ) -> dict[str, Any]:
        """Record an operational learning from session experience.

        Agents call this when they encounter repeated failures or discover
        an insight worth persisting. The reflection is stored and automatically
        injected into future session bootstraps as 'Operational Learnings'.
        """
        try:
            repo_path = Path(repo).resolve()
            sid = session_id
            if not sid:
                manager = AgentSessionManager(repo_path, actor="agent:mcp")
                active = manager.get_active()
                if active:
                    sid = str(active.get("session_id", ""))
            reflection = do_reflect(
                repo_path,
                pattern=pattern,
                insight=insight,
                severity=severity,
                scope=scope,
                actor="agent:mcp",
                session_id=sid,
                tags=tags or [],
            )
            return {"ok": True, "data": reflect_to_dict(reflection), "events": [], "blocked": False}
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_reflections(
        repo: str = ".",
        status: str | None = None,
        scope: str | None = None,
        severity: str | None = None,
    ) -> dict[str, Any]:
        """List stored reflections with optional filters."""
        try:
            repo_path = Path(repo).resolve()
            refs = load_reflections(repo_path, status=status, scope=scope)
            if severity:
                target = severity.strip().lower()
                refs = [r for r in refs if r.severity == target]
            return {
                "ok": True,
                "data": {"reflections": reflections_to_list(refs), "count": len(refs)},
                "events": [],
                "blocked": False,
            }
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @mcp.tool()
    def exo_reflect_dismiss(
        reflection_id: str,
        repo: str = ".",
    ) -> dict[str, Any]:
        """Dismiss a reflection so it stops appearing in future bootstraps."""
        try:
            ref = dismiss_reflection(Path(repo).resolve(), reflection_id)
            return {"ok": True, "data": reflect_to_dict(ref), "events": [], "blocked": False}
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @server.tool(
        name="exo_scan",
        description="Scan repository and detect languages, sensitive files, build dirs, CI systems, and existing governance. Read-only preview of what 'exo init' would customize.",
    )
    def exo_scan(repo: str = ".") -> dict[str, Any]:
        try:
            from exo.stdlib.scan import scan_repo, scan_to_dict

            repo_path = Path(repo).resolve()
            report = scan_repo(repo_path)
            return {"ok": True, "data": scan_to_dict(report), "events": [], "blocked": False}
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @server.tool(
        name="exo_doctor",
        description="Run unified governance health check: scaffold, config validation, drift, and scan freshness.",
    )
    def exo_doctor(repo: str = ".", stale_hours: float = 48.0) -> dict[str, Any]:
        try:
            from exo.stdlib.doctor import doctor as run_doctor, doctor_to_dict

            repo_path = Path(repo).resolve()
            report = run_doctor(repo_path, stale_hours=stale_hours)
            return {"ok": True, "data": doctor_to_dict(report), "events": [], "blocked": False}
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @server.tool(
        name="exo_config_validate",
        description="Validate .exo/config.yaml structure, types, and value ranges.",
    )
    def exo_config_validate(repo: str = ".") -> dict[str, Any]:
        try:
            from exo.stdlib.config_schema import validate_config, validation_to_dict

            repo_path = Path(repo).resolve()
            result = validate_config(repo_path)
            return {"ok": True, "data": validation_to_dict(result), "events": [], "blocked": False}
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }

    @server.tool(
        name="exo_upgrade",
        description="Upgrade .exo/ directory to latest schema version. Backfills missing config keys, creates missing dirs, recompiles governance, regenerates adapters.",
    )
    def exo_upgrade(repo: str = ".", dry_run: bool = False) -> dict[str, Any]:
        try:
            from exo.stdlib.upgrade import upgrade as run_upgrade

            repo_path = Path(repo).resolve()
            data = run_upgrade(repo_path, dry_run=dry_run)
            return {"ok": True, "data": data, "events": [], "blocked": False}
        except ExoError as err:
            return {"ok": False, "error": err.to_dict(), "events": [], "blocked": err.blocked}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {"code": "UNHANDLED_EXCEPTION", "message": str(exc)},
                "events": [],
                "blocked": False,
            }


def main() -> int:
    if not FastMCP:
        raise SystemExit("MCP dependencies missing. Install with: pip install -e .[mcp]")

    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
