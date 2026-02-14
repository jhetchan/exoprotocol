from __future__ import annotations

import contextlib
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from exo.kernel import governance, ledger, tickets
from exo.kernel.engine import check_action, open_session
from exo.kernel.errors import ExoError
from exo.kernel.utils import default_topic_id, load_yaml

try:
    import fcntl as _fcntl
except Exception:  # pragma: no cover - platform-specific
    _fcntl = None

DEFAULT_CONTROL_CAPS: dict[str, list[str]] = {
    "decide_override": ["cap:override"],
    "policy_set": ["cap:policy-set"],
    "cas_head": ["cap:cas-head"],
}
DECISION_LOCK_PATH = Path(".exo/logs/ledger.decision.lock")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class KernelSyscalls:
    """Transport-neutral 12-syscall control-plane wrapper.

    This interface is additive and does not alter the frozen 10-function kernel API.
    """

    def __init__(self, root: Path | str, actor: str) -> None:
        repo = Path(root).resolve()
        self.root = repo
        self.actor = actor
        self._session = open_session(repo, actor)

    @contextlib.contextmanager
    def _decision_lock(self):
        lock_path = self.root / DECISION_LOCK_PATH
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
            yield
        finally:
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
            handle.close()

    def _find_latest_decision_id(self, intent_id: str) -> str | None:
        rows = ledger.read_records(
            self.root,
            record_type="DecisionRecorded",
            intent_id=intent_id,
            limit=200,
        )
        if not rows:
            return None
        decision_id = rows[-1].get("decision_id")
        if isinstance(decision_id, str) and decision_id.strip():
            return decision_id.strip()
        return None

    def _load_control_caps(self) -> dict[str, list[str]]:
        config_path = self.root / ".exo" / "config.yaml"
        if not config_path.exists():
            return dict(DEFAULT_CONTROL_CAPS)

        config = load_yaml(config_path)
        control_caps = config.get("control_caps") if isinstance(config, dict) else None
        if not isinstance(control_caps, dict):
            return dict(DEFAULT_CONTROL_CAPS)

        merged: dict[str, list[str]] = dict(DEFAULT_CONTROL_CAPS)
        for key, value in control_caps.items():
            if not isinstance(key, str):
                continue
            if isinstance(value, list):
                merged[key] = [str(cap) for cap in value if isinstance(cap, str) and cap.strip()]
        return merged

    def _require_control_cap(self, action: str, provided_cap: str) -> str:
        token = provided_cap.strip()
        if not token:
            raise ExoError(code="CAPABILITY_DENIED", message=f"{action} requires a capability token", blocked=True)

        allowed = self._load_control_caps().get(action, [])
        if allowed and token not in allowed:
            raise ExoError(
                code="CAPABILITY_DENIED",
                message=f"Capability {token} is not allowed for {action}",
                details={"action": action, "provided": token, "allowed": allowed},
                blocked=True,
            )
        return token

    def _resolve_intent_record(self, intent_id: str) -> dict[str, Any]:
        rows = ledger.read_records(self.root, record_type="IntentSubmitted", intent_id=intent_id, limit=1)
        if not rows:
            raise ExoError(
                code="INTENT_NOT_FOUND",
                message=f"Intent not found in ledger: {intent_id}",
                details={"intent_id": intent_id},
                blocked=True,
            )
        return rows[-1]

    def submit(self, intent_envelope: dict[str, Any]) -> str:
        envelope = dict(intent_envelope)
        intent_id = str(envelope.get("intent_id") or f"INT-{uuid.uuid4().hex[:12].upper()}")
        topic_id = str(envelope.get("topic") or default_topic_id(self.root))
        parents_raw = envelope.get("parents")
        parents = (
            [str(item) for item in parents_raw if isinstance(item, str) and item.strip()]
            if isinstance(parents_raw, list)
            else None
        )

        payload_hash_value = envelope.get("payload_hash")
        if not isinstance(payload_hash_value, str) or not payload_hash_value.strip():
            payload_hash_value = ledger.payload_hash(
                {
                    "payload": envelope.get("payload"),
                    "payload_ref": envelope.get("payload_ref"),
                    "metadata": envelope.get("metadata"),
                }
            )

        now = _now_iso()
        ttl_hours_raw = envelope.get("ttl_hours", 1)
        try:
            ttl_hours = max(int(ttl_hours_raw), 1)
        except (TypeError, ValueError):
            ttl_hours = 1
        expires_at = (datetime.now().astimezone() + timedelta(hours=ttl_hours)).isoformat(timespec="seconds")

        scope_raw = envelope.get("scope") if isinstance(envelope.get("scope"), dict) else {}
        allow_raw = scope_raw.get("allow") if isinstance(scope_raw.get("allow"), list) else ["**"]
        deny_raw = scope_raw.get("deny") if isinstance(scope_raw.get("deny"), list) else []
        scope = {
            "allow": [str(item) for item in allow_raw if isinstance(item, str) and item.strip()] or ["**"],
            "deny": [str(item) for item in deny_raw if isinstance(item, str) and item.strip()],
        }

        metadata = envelope.get("metadata") if isinstance(envelope.get("metadata"), dict) else {}
        action = envelope.get("action") if isinstance(envelope.get("action"), dict) else {}
        nonce = str(envelope.get("nonce") or uuid.uuid4().hex)

        max_attempts_raw = envelope.get("max_attempts", 1)
        try:
            max_attempts = max(int(max_attempts_raw), 1)
        except (TypeError, ValueError):
            max_attempts = 1

        ledger.intent_submitted(
            self.root,
            intent_id=intent_id,
            actor_id=self.actor,
            topic_id=topic_id,
            payload_hash_value=str(payload_hash_value),
            parents=parents,
            metadata={
                **metadata,
                "intent": str(envelope.get("intent") or "syscall.submit"),
                "scope": scope,
                "ttl_hours": ttl_hours,
                "created_at": now,
                "expires_at": expires_at,
                "nonce": nonce,
                "action": {
                    "kind": str(action.get("kind") or "read_file"),
                    "target": action.get("target"),
                    "params": action.get("params") if isinstance(action.get("params"), dict) else {},
                    "mode": str(action.get("mode") or "execute"),
                },
            },
            expected_head=str(envelope.get("expected_ref")) if envelope.get("expected_ref") is not None else None,
            max_head_attempts=max_attempts,
        )
        return intent_id

    def check(self, intent_id: str, context_refs: list[str] | None = None) -> str:
        _ = context_refs
        with self._decision_lock():
            existing_decision_id = self._find_latest_decision_id(intent_id)
            if isinstance(existing_decision_id, str) and existing_decision_id:
                return existing_decision_id

            intent = self._resolve_intent_record(intent_id)
            metadata = intent.get("metadata") if isinstance(intent.get("metadata"), dict) else {}

            action = metadata.get("action") if isinstance(metadata.get("action"), dict) else {}
            scope = metadata.get("scope") if isinstance(metadata.get("scope"), dict) else {"allow": ["**"], "deny": []}

            created_at = str(metadata.get("created_at") or _now_iso())
            expires_at = str(
                metadata.get("expires_at")
                or (datetime.now().astimezone() + timedelta(hours=1)).isoformat(timespec="seconds")
            )
            nonce = str(metadata.get("nonce") or intent_id)

            ticket = {
                "id": intent_id,
                "intent": str(metadata.get("intent") or "syscall.intent"),
                "scope": scope,
                "ttl_hours": int(metadata.get("ttl_hours", 1) or 1),
                "created_at": created_at,
                "expires_at": expires_at,
                "nonce": nonce,
                "metadata": {
                    "origin": "syscall",
                },
            }

            gov = governance.load_governance(self.root)
            decision = check_action(
                gov,
                self._session,
                ticket,
                {
                    "kind": str(action.get("kind") or "read_file"),
                    "target": action.get("target"),
                    "params": action.get("params") if isinstance(action.get("params"), dict) else {},
                    "mode": str(action.get("mode") or "execute"),
                },
            )

            decision_id = decision.constraints.get("decision_id") if isinstance(decision.constraints, dict) else None
            if not isinstance(decision_id, str) or not decision_id.strip():
                raise ExoError(
                    code="DECISION_ID_MISSING",
                    message=f"Decision ID missing for intent {intent_id}",
                    details={"status": decision.status, "reasons": decision.reasons},
                    blocked=True,
                )
            return decision_id

    def decide_override(self, intent_id: str, override_cap: str, rationale_ref: str, outcome: str = "ALLOW") -> str:
        cap = self._require_control_cap("decide_override", override_cap)
        _ = cap
        intent = self._resolve_intent_record(intent_id)
        _ = intent

        rationale = rationale_ref.strip()
        if not rationale:
            raise ExoError(code="RATIONALE_REF_INVALID", message="rationale_ref is required", blocked=True)

        rationale_rows = ledger.read_records(self.root, ref_id=rationale, limit=1)
        if not rationale_rows:
            cursor_line = rationale.split(":", 1)
            if not (len(cursor_line) == 2 and cursor_line[0] == "line" and cursor_line[1].isdigit()):
                raise ExoError(
                    code="RATIONALE_REF_NOT_FOUND",
                    message=f"Rationale reference not found: {rationale}",
                    details={"rationale_ref": rationale},
                    blocked=True,
                )
            line_no = int(cursor_line[1])
            prior_cursor = f"line:{line_no - 1}" if line_no > 1 else None
            probe = ledger.subscribe(self.root, since_cursor=prior_cursor, limit=1)
            events = probe.get("events", []) if isinstance(probe, dict) else []
            if not events or str(events[0].get("cursor")) != rationale:
                raise ExoError(
                    code="RATIONALE_REF_NOT_FOUND",
                    message=f"Rationale reference not found: {rationale}",
                    details={"rationale_ref": rationale},
                    blocked=True,
                )

        normalized_outcome = outcome.strip().upper()
        if normalized_outcome not in {"ALLOW", "DENY", "ESCALATE", "SANDBOX"}:
            raise ExoError(
                code="OVERRIDE_OUTCOME_INVALID",
                message="outcome must be one of ALLOW|DENY|ESCALATE|SANDBOX",
                blocked=True,
            )

        decision_id = f"DEC-OVR-{uuid.uuid4().hex[:12].upper()}"
        gov_lock = governance.load_governance_lock(self.root)
        ledger.decision_recorded(
            self.root,
            decision_id=decision_id,
            intent_id=intent_id,
            policy_version=str(gov_lock.get("version", "0.1")),
            outcome=normalized_outcome,
            reasons_hash=ledger.payload_hash({"rationale_ref": rationale, "actor": self.actor}),
            reasons=[f"override by {self.actor}", f"rationale_ref={rationale}"],
            constraints={
                "override": {
                    "actor": self.actor,
                    "capability": override_cap,
                    "rationale_ref": rationale,
                }
            },
        )
        return decision_id

    def begin(self, decision_id: str, executor_ref: str, idem_key: str) -> str:
        effect_id = f"EFF-{uuid.uuid4().hex[:12].upper()}"
        ledger.execution_begun(
            self.root,
            effect_id=effect_id,
            decision_id=decision_id,
            executor_ref=executor_ref,
            idempotency_key=idem_key,
        )
        return effect_id

    def commit(self, effect_id: str, status: str, artifact_refs: list[str] | None = None) -> None:
        ledger.execution_result(self.root, effect_id=effect_id, status=status, artifact_refs=artifact_refs or [])

    def read(self, ref_id: str | None, selector: dict[str, Any] | None) -> list[dict[str, Any]]:
        selector_data = selector if isinstance(selector, dict) else {}
        limit = int(selector_data.get("limit", 200) or 200)
        since_cursor = selector_data.get("sinceCursor")
        type_filter = selector_data.get("typeFilter")

        topic_id = selector_data.get("topic_id")
        intent_id = selector_data.get("intent_id")
        lookup_ref = ref_id

        if isinstance(ref_id, str) and ref_id.startswith(("repo:", "topic:")):
            topic_id = ref_id
            lookup_ref = None

        return ledger.read_records(
            self.root,
            record_type=str(type_filter) if isinstance(type_filter, str) and type_filter.strip() else None,
            intent_id=str(intent_id) if isinstance(intent_id, str) and intent_id.strip() else None,
            topic_id=str(topic_id) if isinstance(topic_id, str) and topic_id.strip() else None,
            ref_id=str(lookup_ref) if isinstance(lookup_ref, str) and lookup_ref.strip() else None,
            since_cursor=str(since_cursor) if isinstance(since_cursor, str) and since_cursor.strip() else None,
            limit=max(limit, 1),
        )

    def head(self, topic_id: str) -> str | None:
        return ledger.head(self.root, topic_id)

    def cas_head(
        self,
        topic_id: str,
        expected_ref: str | None,
        new_ref: str | None,
        control_cap: str,
        max_attempts: int = 1,
    ) -> dict[str, Any]:
        _ = self._require_control_cap("cas_head", control_cap)
        try:
            attempts = max(int(max_attempts), 1)
        except (TypeError, ValueError):
            attempts = 1
        result = ledger.cas_head_retry(
            self.root,
            topic_id,
            expected_ref,
            new_ref,
            max_attempts=attempts,
        )
        if not bool(result.get("ok")):
            raise ExoError(
                code="CAS_HEAD_CONFLICT",
                message=f"CAS head conflict for topic {topic_id}",
                details=result,
                blocked=True,
            )
        return result

    def subscribe(self, topic_id: str, since_cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
        return ledger.subscribe(self.root, topic_id=topic_id, since_cursor=since_cursor, limit=limit)

    def ack(self, ref_id: str, actor_cap: str, required: int = 1) -> dict[str, Any]:
        cap = actor_cap.strip()
        if not cap:
            raise ExoError(code="CAPABILITY_DENIED", message="ack requires actor_cap", blocked=True)
        ack_ref = ledger.acked(self.root, actor_id=self.actor, ref_id=ref_id)
        quorum = ledger.ack_status(self.root, ref_id=ref_id, required=required)
        return {
            "ack_record": {
                "log_path": ack_ref.log_path,
                "line": ack_ref.line,
                "record_hash": ack_ref.record_hash,
                "record_type": ack_ref.record_type,
                "ref_id": ack_ref.ref_id,
            },
            "quorum": quorum,
        }

    def escalate(self, intent_id: str, kind: str, ctx_refs: list[str] | None = None) -> None:
        self._resolve_intent_record(intent_id)
        ledger.escalated(
            self.root,
            intent_id=intent_id,
            escalation_kind=kind,
            context_refs=ctx_refs or [],
        )

    def policy_set(self, policy_bundle: str | None, policy_cap: str, version: str | None = None) -> str:
        _ = self._require_control_cap("policy_set", policy_cap)

        lock = tickets.ensure_lock(self.root)
        ticket_id = str(lock.get("ticket_id", "")).strip()
        if not ticket_id:
            raise ExoError(code="LOCK_TICKET_INVALID", message="Active lock missing ticket_id", blocked=True)
        ticket = tickets.load_ticket(self.root, ticket_id)
        ticket_type = str(ticket.get("type", "")).strip().lower()
        if ticket_type != "governance" and not ticket_id.startswith("GOV-"):
            raise ExoError(
                code="POLICY_TICKET_REQUIRED",
                message="policy_set requires a governance ticket lock (type=governance or GOV-*)",
                details={"ticket_id": ticket_id, "ticket_type": ticket_type},
                blocked=True,
            )

        if isinstance(policy_bundle, str) and policy_bundle.strip():
            bundle_path = Path(policy_bundle)
            if not bundle_path.is_absolute():
                bundle_path = (self.root / bundle_path).resolve()
            if not bundle_path.exists():
                raise ExoError(
                    code="POLICY_BUNDLE_NOT_FOUND",
                    message=f"Policy bundle not found: {policy_bundle}",
                    blocked=True,
                )
            content = bundle_path.read_text(encoding="utf-8")
            (self.root / ".exo" / "CONSTITUTION.md").write_text(content, encoding="utf-8")

        current_lock = governance.load_governance_lock(self.root)
        target_version = (
            version.strip() if isinstance(version, str) and version.strip() else str(current_lock.get("version", "0.1"))
        )
        compiled = governance.compile_constitution(self.root, version=target_version)
        return str(compiled.get("version", "0.1"))
