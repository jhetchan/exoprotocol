from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


DecisionStatus = Literal["ALLOW", "DENY", "REQUIRE_ATTESTATION"]
TicketValidationStatus = Literal["VALID", "INVALID"]
TicketKind = Literal["intent", "epic", "task"]
IntentRisk = Literal["low", "medium", "high"]

TICKET_KINDS: set[str] = {"intent", "epic", "task"}
INTENT_RISKS: set[str] = {"low", "medium", "high"}


@dataclass(frozen=True)
class Governance:
    root: str
    source_file: str
    source_hash: str
    actual_source_hash: str
    rules: list[dict[str, Any]]
    lock_data: dict[str, Any]


@dataclass(frozen=True)
class VerificationReport:
    valid: bool
    reasons: list[str]
    expected_hash: str | None = None
    actual_hash: str | None = None


@dataclass(frozen=True)
class Session:
    id: str
    root: str
    actor: str
    opened_at: str
    env_fingerprint: dict[str, Any]


@dataclass(frozen=True)
class Ticket:
    id: str
    intent: str
    scope: dict[str, list[str]]
    ttl_hours: int
    created_at: str
    expires_at: str
    nonce: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TicketStatus:
    status: TicketValidationStatus
    reasons: list[str]
    normalized_ticket: dict[str, Any]


@dataclass(frozen=True)
class Action:
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    target: str | None = None
    mode: str = "execute"


@dataclass(frozen=True)
class Decision:
    status: DecisionStatus
    reasons: list[str]
    required_evidence: list[str]
    constraints: dict[str, Any]


@dataclass(frozen=True)
class Plan:
    steps: list[dict[str, Any]]
    constraints: dict[str, Any]
    required_audits: list[str]


@dataclass(frozen=True)
class AuditRef:
    log_path: str
    line: int
    event_hash: str
    ts: str


@dataclass(frozen=True)
class LedgerRef:
    log_path: str
    line: int
    record_hash: str
    ts: str
    record_type: str
    ref_id: str | None = None


@dataclass(frozen=True)
class Receipt:
    kernel_name: str
    kernel_version: str
    action_hash: str
    decision_hash: str
    plan_hash: str
    audit_hashes: list[str]
    timestamp: str
    receipt_hash: str


def to_dict(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return value
