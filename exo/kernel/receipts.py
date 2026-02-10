from __future__ import annotations

import json
from typing import Any

from .types import Action, AuditRef, Receipt, Session, Ticket, to_dict
from .utils import now_iso, sha256_text
from .version import KERNEL_NAME, KERNEL_VERSION


def _stable_hash(payload: Any) -> str:
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except TypeError:
        encoded = json.dumps(str(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256_text(encoded)


def seal_result(
    session: Session | dict[str, Any],
    ticket: Ticket | dict[str, Any],
    action: Action | dict[str, Any],
    result: dict[str, Any],
    audit_refs: list[AuditRef | dict[str, Any]],
) -> Receipt:
    action_data = to_dict(action)
    result_data = result if isinstance(result, dict) else {}

    decision_data = result_data.get("decision", {})
    plan_data = result_data.get("plan", {})

    action_hash = _stable_hash(action_data)
    decision_hash = _stable_hash(decision_data)
    plan_hash = _stable_hash(plan_data)

    hashes: list[str] = []
    for ref in audit_refs:
        if isinstance(ref, AuditRef):
            hashes.append(ref.event_hash)
            continue
        if isinstance(ref, dict) and isinstance(ref.get("event_hash"), str):
            hashes.append(str(ref["event_hash"]))

    timestamp = now_iso()
    receipt_hash = _stable_hash(
        {
            "session": to_dict(session),
            "ticket": to_dict(ticket),
            "kernel_name": KERNEL_NAME,
            "kernel_version": KERNEL_VERSION,
            "action_hash": action_hash,
            "decision_hash": decision_hash,
            "plan_hash": plan_hash,
            "audit_hashes": hashes,
            "timestamp": timestamp,
        }
    )

    return Receipt(
        kernel_name=KERNEL_NAME,
        kernel_version=KERNEL_VERSION,
        action_hash=action_hash,
        decision_hash=decision_hash,
        plan_hash=plan_hash,
        audit_hashes=hashes,
        timestamp=timestamp,
        receipt_hash=receipt_hash,
    )
