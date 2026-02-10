from __future__ import annotations

from pathlib import Path
from typing import Any

from exo.control.syscalls import KernelSyscalls
from exo.kernel.errors import ExoError
from exo.orchestrator.worker import DistributedWorker
from exo.stdlib.engine import KernelEngine

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
        resolved_topic = topic_id or f"repo:{Path(repo).resolve().as_posix()}"
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
        resolved_topic = topic_id or f"repo:{Path(repo).resolve().as_posix()}"
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
        response = _run_syscall(repo, "check", intent_id=intent_id, context_refs=context_refs or [], data_key="decision_id")
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
    ) -> dict[str, Any]:
        worker = DistributedWorker(
            Path(repo).resolve(),
            actor="agent:mcp",
            topic_id=topic_id,
            cursor_path=cursor_file,
            use_cursor=use_cursor,
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
    def exo_distill(proposal_id: str, repo: str = ".", statement: str | None = None, confidence: float = 0.7) -> dict[str, Any]:
        return _run(repo, "distill", proposal_id=proposal_id, statement=statement, confidence=confidence)


def main() -> int:
    if not FastMCP:
        raise SystemExit("MCP dependencies missing. Install with: pip install -e .[mcp]")

    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
