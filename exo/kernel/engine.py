from __future__ import annotations

import json
import os
import platform
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import governance as governance_mod
from . import ledger
from . import tickets as tickets_mod
from .audit import append_audit_event, event_template
from .errors import ExoError
from .types import Action, Decision, Governance, Plan, Session, Ticket, to_dict
from .utils import any_pattern_matches, load_yaml, relative_posix, sha256_text


def _normalize_action(action: Action | dict[str, Any]) -> Action:
    if isinstance(action, Action):
        return action
    if isinstance(action, dict):
        return Action(
            kind=str(action.get("kind", "")).strip(),
            params=action.get("params") if isinstance(action.get("params"), dict) else {},
            target=str(action["target"]) if isinstance(action.get("target"), str) else None,
            mode=str(action.get("mode", "execute")),
        )
    raise ExoError(code="ACTION_INVALID", message="action must be Action|dict", blocked=True)


def _normalize_ticket(ticket: Ticket | dict[str, Any]) -> dict[str, Any]:
    if isinstance(ticket, Ticket):
        return to_dict(ticket)
    if isinstance(ticket, dict):
        return dict(ticket)
    raise ExoError(code="TICKET_INVALID", message="ticket must be Ticket|dict", blocked=True)


def _effective_policy_action(action: Action) -> str:
    lowered = action.kind.strip().lower()
    if "delete" in lowered:
        return "delete"
    if "write" in lowered or "create" in lowered or "update" in lowered:
        return "write"
    if "read" in lowered:
        return "read"
    return lowered or "write"


def _path_for_target(root: Path, target: str | None) -> Path | None:
    if not isinstance(target, str) or not target.strip():
        return None
    target_path = Path(target)
    if target_path.is_absolute():
        return target_path.resolve()
    return (root / target_path).resolve()


def _is_advisory_memory_path(root: Path, path: Path) -> bool:
    rel = relative_posix(path, root).replace("\\", "/")
    return rel == ".exo/memory" or rel.startswith(".exo/memory/")


def _scope_lists(ticket_data: dict[str, Any]) -> tuple[list[str], list[str]]:
    scope = ticket_data.get("scope") if isinstance(ticket_data.get("scope"), dict) else {}
    allow_raw = scope.get("allow") if isinstance(scope, dict) else []
    deny_raw = scope.get("deny") if isinstance(scope, dict) else []

    allow = [str(item) for item in allow_raw if isinstance(item, str) and item.strip()]
    deny = [str(item) for item in deny_raw if isinstance(item, str) and item.strip()]
    if not allow:
        allow = ["**"]
    return allow, deny


def _decision_outcome_for_ledger(decision: Decision) -> str:
    if decision.status == "REQUIRE_ATTESTATION":
        return "ESCALATE"
    if decision.status in {"ALLOW", "DENY"}:
        return decision.status
    return "ESCALATE"


def _decision_reasons_hash(decision: Decision) -> str:
    return sha256_text(json.dumps(decision.reasons, sort_keys=True, ensure_ascii=True))


def _finalize_decision(gov: Governance, decision: Decision, *, intent_id: str) -> Decision:
    policy_version = str(gov.lock_data.get("version", "0.1"))
    decision_id = f"DEC-{datetime.now().astimezone().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8].upper()}"
    try:
        ref = ledger.decision_recorded(
            gov.root,
            decision_id=decision_id,
            intent_id=intent_id,
            policy_version=policy_version,
            outcome=_decision_outcome_for_ledger(decision),
            reasons_hash=_decision_reasons_hash(decision),
            reasons=decision.reasons,
            constraints=decision.constraints,
        )
    except ExoError as err:
        return Decision(
            status="DENY",
            reasons=[*decision.reasons, f"decision log failure: {err.code}: {err.message}"],
            required_evidence=[],
            constraints={"rule": "LEDGER_APPEND_FAILED"},
        )
    except Exception as exc:  # noqa: BLE001
        return Decision(
            status="DENY",
            reasons=[*decision.reasons, f"decision log failure: {exc}"],
            required_evidence=[],
            constraints={"rule": "LEDGER_APPEND_FAILED"},
        )

    constraints = dict(decision.constraints)
    constraints["decision_id"] = decision_id
    constraints["decision_record"] = {
        "log_path": ref.log_path,
        "line": ref.line,
        "record_hash": ref.record_hash,
        "record_type": ref.record_type,
    }
    return Decision(
        status=decision.status,
        reasons=list(decision.reasons),
        required_evidence=list(decision.required_evidence),
        constraints=constraints,
    )


def open_session(root: Path | str, actor: str) -> Session:
    repo = Path(root).resolve()
    now = datetime.now().astimezone()
    session_id = f"SES-{now.strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8].upper()}"
    return Session(
        id=session_id,
        root=repo.as_posix(),
        actor=actor,
        opened_at=now.isoformat(timespec="seconds"),
        env_fingerprint={
            "cwd": repo.as_posix(),
            "pid": os.getpid(),
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
    )


def check_action(
    gov: Governance,
    session: Session,
    ticket: Ticket | dict[str, Any],
    action: Action | dict[str, Any],
) -> Decision:
    _ = session
    try:
        ticket_hint = _normalize_ticket(ticket)
    except ExoError:
        ticket_hint = {}
    hint_intent_id = str(ticket_hint.get("id", "UNKNOWN")) if isinstance(ticket_hint, dict) else "UNKNOWN"

    report = governance_mod.verify_governance(gov)
    if not report.valid:
        return _finalize_decision(
            gov,
            Decision(
                status="DENY",
                reasons=["governance verification failed", *report.reasons],
                required_evidence=[],
                constraints={
                    "expected_hash": report.expected_hash,
                    "actual_hash": report.actual_hash,
                },
            ),
            intent_id=hint_intent_id,
        )

    ticket_status = tickets_mod.validate_ticket(gov, ticket)
    if ticket_status.status != "VALID":
        return _finalize_decision(
            gov,
            Decision(
                status="DENY",
                reasons=["ticket validation failed", *ticket_status.reasons],
                required_evidence=[],
                constraints={},
            ),
            intent_id=hint_intent_id,
        )

    try:
        normalized_action = _normalize_action(action)
    except ExoError as err:
        return _finalize_decision(
            gov,
            Decision(
                status="DENY",
                reasons=[err.message],
                required_evidence=[],
                constraints={},
            ),
            intent_id=hint_intent_id,
        )

    repo = Path(gov.root)
    target_path = _path_for_target(repo, normalized_action.target)
    policy_action = _effective_policy_action(normalized_action)

    if target_path and policy_action in {"write", "delete"} and _is_advisory_memory_path(repo, target_path):
        return _finalize_decision(
            gov,
            Decision(
                status="DENY",
                reasons=[
                    (f"Layer-4 memory is advisory and read-only during governed execution: {normalized_action.target}")
                ],
                required_evidence=[],
                constraints={"rule": "MEMORY_READ_ONLY_EXECUTION"},
            ),
            intent_id=hint_intent_id,
        )

    constraints: dict[str, Any] = {}
    ticket_data = ticket_status.normalized_ticket
    intent_id = str(ticket_data.get("id", hint_intent_id))
    budgets = ticket_data.get("budgets")
    if isinstance(budgets, dict):
        constraints["budgets"] = budgets

    if policy_action in {"write", "delete"} and governance_mod.has_rule(gov.lock_data, "require_lock"):
        ticket_id = ticket_data.get("id")
        scoped_ticket = str(ticket_id) if isinstance(ticket_id, str) and ticket_id.startswith("TICKET-") else None
        try:
            tickets_mod.ensure_lock(repo, scoped_ticket)
        except ExoError as err:
            return _finalize_decision(
                gov,
                Decision(
                    status="DENY",
                    reasons=[f"lock required: {err.message}"],
                    required_evidence=[],
                    constraints={"rule": "require_lock"},
                ),
                intent_id=intent_id,
            )

    allow_patterns, deny_patterns = _scope_lists(ticket_data)
    if (
        policy_action in {"write", "delete"}
        and target_path
        and not any_pattern_matches(target_path, allow_patterns, repo)
    ):
        return _finalize_decision(
            gov,
            Decision(
                status="DENY",
                reasons=[f"target outside ticket scope allowlist: {normalized_action.target}"],
                required_evidence=[],
                constraints={"allow": allow_patterns},
            ),
            intent_id=intent_id,
        )

    if target_path and deny_patterns and any_pattern_matches(target_path, deny_patterns, repo):
        return _finalize_decision(
            gov,
            Decision(
                status="DENY",
                reasons=[f"target matches ticket scope denylist: {normalized_action.target}"],
                required_evidence=[],
                constraints={"deny": deny_patterns},
            ),
            intent_id=intent_id,
        )

    if target_path:
        denied = governance_mod.evaluate_filesystem_rules(gov.lock_data, policy_action, target_path, repo)
        if denied:
            return _finalize_decision(
                gov,
                Decision(
                    status="DENY",
                    reasons=[str(denied.get("message") or "blocked by governance rule")],
                    required_evidence=[],
                    constraints={"rule": denied},
                ),
                intent_id=intent_id,
            )

    if policy_action == "delete":
        return _finalize_decision(
            gov,
            Decision(
                status="REQUIRE_ATTESTATION",
                reasons=["delete actions require attestation before execution"],
                required_evidence=["attestation:delete"],
                constraints=constraints,
            ),
            intent_id=intent_id,
        )

    return _finalize_decision(
        gov,
        Decision(
            status="ALLOW",
            reasons=["policy checks passed"],
            required_evidence=[],
            constraints=constraints,
        ),
        intent_id=intent_id,
    )


def resolve_requirements(decision: Decision, evidence: dict[str, Any] | list[str] | None) -> Decision:
    if decision.status != "REQUIRE_ATTESTATION":
        return decision

    provided: set[str] = set()
    if isinstance(evidence, dict):
        for key, value in evidence.items():
            if value:
                provided.add(str(key))
    elif isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, str) and item.strip():
                provided.add(item.strip())

    missing = [item for item in decision.required_evidence if item not in provided]
    if missing:
        return Decision(
            status="DENY",
            reasons=[*decision.reasons, f"missing evidence: {', '.join(missing)}"],
            required_evidence=missing,
            constraints=decision.constraints,
        )

    return Decision(
        status="ALLOW",
        reasons=[*decision.reasons, "required evidence satisfied"],
        required_evidence=[],
        constraints=decision.constraints,
    )


def commit_plan(
    session: Session,
    ticket: Ticket | dict[str, Any],
    action: Action | dict[str, Any],
) -> Plan:
    normalized_ticket = _normalize_ticket(ticket)
    normalized_action = _normalize_action(action)

    constraints: dict[str, Any] = {}
    scope = normalized_ticket.get("scope")
    if isinstance(scope, dict):
        constraints["scope"] = scope
    budgets = normalized_ticket.get("budgets")
    if isinstance(budgets, dict):
        constraints["budgets"] = budgets

    steps = [
        {
            "index": 1,
            "op": "authorize",
            "primitive": "check_action",
            "action_kind": normalized_action.kind,
            "target": normalized_action.target,
        },
        {
            "index": 2,
            "op": "execute",
            "primitive": "apply_effect",
            "action_kind": normalized_action.kind,
            "target": normalized_action.target,
            "params": normalized_action.params,
        },
        {
            "index": 3,
            "op": "audit",
            "primitive": "append_audit",
            "event": "action_executed",
            "session_id": session.id,
        },
        {
            "index": 4,
            "op": "seal",
            "primitive": "seal_result",
            "session_id": session.id,
        },
    ]

    return Plan(
        steps=steps,
        constraints=constraints,
        required_audits=["action_authorized", "action_executed", "action_result_sealed"],
    )


class KernelEngine:
    """Compatibility wrapper around the enforcement API."""

    def __init__(self, repo: Path | str = ".", *, actor: str = "human") -> None:
        self.repo = Path(repo).resolve()
        self.actor = actor
        self.events: list[dict[str, Any]] = []

    @property
    def exo_dir(self) -> Path:
        return self.repo / ".exo"

    def begin(self) -> None:
        self.events = []

    def _audit(
        self,
        action: str,
        result: str,
        *,
        ticket: str | None = None,
        path: Path | None = None,
        rule: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        rel_path = relative_posix(path, self.repo) if path else None
        event = event_template(
            actor=self.actor,
            action=action,
            result=result,
            ticket=ticket,
            path=rel_path,
            rule=rule,
            details=details,
        )
        append_audit_event(self.repo, event)
        self.events.append(event)

    def _require_scaffold(self) -> None:
        if not self.exo_dir.exists():
            raise ExoError(code="EXO_NOT_INITIALIZED", message="Missing .exo/. Run: exo init")

    def load_config(self) -> dict[str, Any]:
        config_path = self.exo_dir / "config.yaml"
        if not config_path.exists():
            return {}
        data = load_yaml(config_path)
        return data if isinstance(data, dict) else {}

    def verify_integrity(self) -> dict[str, Any]:
        self._require_scaffold()
        return governance_mod.verify_integrity(self.repo)

    def require_lock(self, ticket_id: str | None = None) -> dict[str, Any]:
        self._require_scaffold()
        return tickets_mod.ensure_lock(self.repo, ticket_id)

    def resolve_ticket(self, ticket_id: str | None = None) -> dict[str, Any]:
        lock = self.require_lock(ticket_id)
        resolved_id = ticket_id or str(lock.get("ticket_id", "")).strip()
        if not resolved_id:
            raise ExoError(
                code="LOCK_INVALID",
                message="Active lock missing ticket_id",
                details=lock,
                blocked=True,
            )
        ticket = tickets_mod.load_ticket(self.repo, resolved_id)
        ticket["id"] = resolved_id
        return ticket

    def enforce_scope(self, ticket: dict[str, Any], action: str, path: Path) -> None:
        scope = ticket.get("scope") or {}
        allow_patterns = [str(item) for item in scope.get("allow", ["**"]) if isinstance(item, str)]
        deny_patterns = [str(item) for item in scope.get("deny", []) if isinstance(item, str)]

        is_write_like = action in {"write", "delete"}
        if is_write_like and not any_pattern_matches(path, allow_patterns, self.repo):
            raise ExoError(
                code="SCOPE_DENY",
                message=f"Path is outside ticket scope allowlist: {path}",
                details={"ticket": ticket.get("id"), "action": action, "path": str(path), "allow": allow_patterns},
                blocked=True,
            )

        if deny_patterns and any_pattern_matches(path, deny_patterns, self.repo):
            raise ExoError(
                code="SCOPE_DENY",
                message=f"Path matches ticket scope denylist: {path}",
                details={"ticket": ticket.get("id"), "action": action, "path": str(path), "deny": deny_patterns},
                blocked=True,
            )

    def enforce_governance(self, action: str, path: Path, lock_data: dict[str, Any] | None = None) -> None:
        lock = lock_data or self.verify_integrity()
        denied = governance_mod.evaluate_filesystem_rules(lock, action, path, self.repo)
        if denied:
            message = str(denied.get("message") or "Blocked by governance rule")
            raise ExoError(
                code="GOVERNANCE_DENY",
                message=message,
                details={"rule": denied, "action": action, "path": str(path)},
                blocked=True,
            )

    def authorize_filesystem_action(
        self,
        action: str,
        path: Path | str,
        *,
        ticket_id: str | None = None,
    ) -> dict[str, Any]:
        full_path = Path(path)
        if not full_path.is_absolute():
            full_path = (self.repo / full_path).resolve()

        ticket: dict[str, Any] | None = None
        lock_data: dict[str, Any] | None = None

        try:
            lock_data = self.verify_integrity()
            ticket = self.resolve_ticket(ticket_id)
            self.enforce_scope(ticket, action, full_path)
            self.enforce_governance(action, full_path, lock_data)
        except ExoError as err:
            self._audit(
                action=f"authorize_{action}",
                result="blocked",
                ticket=(ticket or {}).get("id") if ticket else ticket_id,
                path=full_path,
                rule=err.code,
                details=err.details,
            )
            raise

        resolved_ticket_id = str(ticket.get("id"))
        self._audit(
            action=f"authorize_{action}",
            result="ok",
            ticket=resolved_ticket_id,
            path=full_path,
        )
        return {
            "ticket_id": resolved_ticket_id,
            "path": relative_posix(full_path, self.repo),
            "action": action,
            "lock": lock_data,
        }
