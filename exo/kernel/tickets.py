from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import ExoError
from . import ledger
from .types import Governance, Session, Ticket, TicketStatus, to_dict
from .utils import dump_json, dump_yaml, load_json, load_yaml, now_iso


TICKETS_DIR = Path(".exo/tickets")
LOCK_FILE = Path(".exo/locks/ticket.lock.json")


TICKET_ID_RE = re.compile(r"^TICKET-(\d+)(?:-EPIC)?$")
PATH_RE = re.compile(r"^(TICKET-\d+(?:-EPIC)?|GOV-\d+|PRACTICE-\d+)\.ya?ml$")


def normalize_ticket(ticket: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(ticket)
    normalized.setdefault("type", "feature")
    normalized.setdefault("status", "todo")
    normalized.setdefault("priority", 3)
    normalized.setdefault("parent_id", None)
    normalized.setdefault("spec_ref", None)
    normalized.setdefault("scope", {})
    normalized.setdefault("budgets", {})
    normalized.setdefault("checks", [])
    normalized.setdefault("notes", [])
    normalized.setdefault("blockers", [])
    normalized.setdefault("labels", [])
    normalized.setdefault("created_at", now_iso())

    scope = normalized.get("scope") or {}
    scope.setdefault("allow", ["**"])
    scope.setdefault("deny", [])
    normalized["scope"] = scope

    budgets = normalized.get("budgets") or {}
    budgets.setdefault("max_files_changed", 12)
    budgets.setdefault("max_loc_changed", 400)
    normalized["budgets"] = budgets

    return normalized


def ticket_file_name(ticket_id: str) -> str:
    return f"{ticket_id}.yaml"


def ticket_path(repo: Path, ticket_id: str) -> Path:
    return repo / TICKETS_DIR / ticket_file_name(ticket_id)


def list_ticket_paths(repo: Path) -> list[Path]:
    directory = repo / TICKETS_DIR
    if not directory.exists():
        return []
    files = [path for path in directory.iterdir() if PATH_RE.match(path.name)]
    return sorted(files)


def load_ticket(repo: Path, ticket_id: str) -> dict[str, Any]:
    path = ticket_path(repo, ticket_id)
    if not path.exists():
        raise ExoError(code="TICKET_NOT_FOUND", message=f"Ticket not found: {ticket_id}")
    data = load_yaml(path)
    data = normalize_ticket(data)
    data.setdefault("id", ticket_id)
    return data


def load_all_tickets(repo: Path) -> list[dict[str, Any]]:
    tickets: list[dict[str, Any]] = []
    for path in list_ticket_paths(repo):
        raw = load_yaml(path)
        raw = normalize_ticket(raw)
        raw.setdefault("id", path.stem)
        tickets.append(raw)
    return tickets


def save_ticket(repo: Path, ticket: dict[str, Any]) -> Path:
    if "id" not in ticket:
        raise ExoError(code="TICKET_INVALID", message="Ticket missing id")
    normalized = normalize_ticket(ticket)
    path = ticket_path(repo, str(ticket["id"]))
    dump_yaml(path, normalized)
    return path


def _existing_ticket_numbers(repo: Path) -> list[int]:
    numbers: list[int] = []
    for path in list_ticket_paths(repo):
        match = TICKET_ID_RE.match(path.stem)
        if not match:
            continue
        numbers.append(int(match.group(1)))
    return sorted(numbers)


def next_ticket_id(repo: Path) -> str:
    numbers = _existing_ticket_numbers(repo)
    nxt = (numbers[-1] + 1) if numbers else 1
    return f"TICKET-{nxt:03d}"


def lock_path(repo: Path) -> Path:
    return repo / LOCK_FILE


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def is_lock_expired(lock: dict[str, Any]) -> bool:
    expires = lock.get("expires_at")
    if not expires:
        return True
    return datetime.now().astimezone() >= _parse_dt(expires)


def load_lock(repo: Path) -> dict[str, Any] | None:
    path = lock_path(repo)
    if not path.exists():
        return None
    lock = load_json(path)
    if is_lock_expired(lock):
        path.unlink(missing_ok=True)
        return None
    return lock


def acquire_lock(
    repo: Path,
    ticket_id: str,
    *,
    owner: str = "human",
    role: str = "developer",
    duration_hours: int = 2,
    base: str = "main",
) -> dict[str, Any]:
    existing = load_lock(repo)
    if existing and existing.get("ticket_id") != ticket_id:
        raise ExoError(
            code="LOCK_HELD",
            message=f"Ticket lock already held by {existing.get('owner')}",
            details=existing,
            blocked=True,
        )
    if existing and existing.get("ticket_id") == ticket_id:
        existing_owner = str(existing.get("owner", "")).strip()
        if existing_owner and existing_owner != owner:
            raise ExoError(
                code="LOCK_COLLISION",
                message=f"Ticket lock collision on {ticket_id}: held by {existing_owner}",
                details=existing,
                blocked=True,
            )

        renewed_at = datetime.now().astimezone()
        renewed_expires = renewed_at + timedelta(hours=duration_hours)
        workspace = existing.get("workspace") if isinstance(existing.get("workspace"), dict) else {}
        branch = str(workspace.get("branch") or f"codex/{ticket_id}")
        base_branch = str(workspace.get("base") or base)
        fencing_raw = existing.get("fencing_token", 0)
        try:
            fencing_token = int(fencing_raw) + 1
        except (TypeError, ValueError):
            fencing_token = 1

        renewed = {
            "ticket_id": ticket_id,
            "owner": owner,
            "role": role,
            "created_at": str(existing.get("created_at") or renewed_at.isoformat(timespec="seconds")),
            "updated_at": renewed_at.isoformat(timespec="seconds"),
            "heartbeat_at": renewed_at.isoformat(timespec="seconds"),
            "expires_at": renewed_expires.isoformat(timespec="seconds"),
            "lease_expires_at": renewed_expires.isoformat(timespec="seconds"),
            "fencing_token": fencing_token,
            "workspace": {
                "branch": branch,
                "base": base_branch,
            },
        }
        dump_json(lock_path(repo), renewed)
        return renewed

    created = datetime.now().astimezone()
    expires = created + timedelta(hours=duration_hours)
    lock = {
        "ticket_id": ticket_id,
        "owner": owner,
        "role": role,
        "created_at": created.isoformat(timespec="seconds"),
        "updated_at": created.isoformat(timespec="seconds"),
        "heartbeat_at": created.isoformat(timespec="seconds"),
        "expires_at": expires.isoformat(timespec="seconds"),
        "lease_expires_at": expires.isoformat(timespec="seconds"),
        "fencing_token": 1,
        "workspace": {
            "branch": f"codex/{ticket_id}",
            "base": base,
        },
    }
    dump_json(lock_path(repo), lock)
    return lock


def ensure_lock(repo: Path, ticket_id: str | None = None) -> dict[str, Any]:
    lock = load_lock(repo)
    if not lock:
        raise ExoError(
            code="LOCK_REQUIRED",
            message="No active ticket lock found",
            blocked=True,
        )
    if ticket_id and lock.get("ticket_id") != ticket_id:
        raise ExoError(
            code="LOCK_MISMATCH",
            message=f"Active lock is for {lock.get('ticket_id')}, not {ticket_id}",
            details=lock,
            blocked=True,
        )
    return lock


def release_lock(repo: Path, ticket_id: str | None = None) -> bool:
    existing = load_lock(repo)
    if not existing:
        return False
    if ticket_id and existing.get("ticket_id") != ticket_id:
        return False
    lock_path(repo).unlink(missing_ok=True)
    return True


def blockers_resolved(ticket: dict[str, Any], index: dict[str, dict[str, Any]]) -> bool:
    blockers = ticket.get("blockers") or []
    if not blockers:
        return True
    for blocker_id in blockers:
        blocker = index.get(blocker_id)
        if not blocker:
            return False
        if blocker.get("status") not in {"done", "archived"}:
            return False
    return True


def _normalize_scope(scope: dict[str, Any] | None) -> dict[str, list[str]]:
    raw = scope if isinstance(scope, dict) else {}
    allow_raw = raw.get("allow")
    deny_raw = raw.get("deny")

    allow: list[str] = []
    deny: list[str] = []

    if isinstance(allow_raw, list):
        allow = [str(item) for item in allow_raw if isinstance(item, str) and item.strip()]
    if isinstance(deny_raw, list):
        deny = [str(item) for item in deny_raw if isinstance(item, str) and item.strip()]

    if not allow:
        allow = ["**"]

    return {"allow": allow, "deny": deny}


def mint_ticket(
    session: Session | dict[str, Any],
    intent: str,
    scope: dict[str, Any] | None,
    ttl: int,
) -> Ticket:
    session_data = to_dict(session) if isinstance(session, Session) else (session if isinstance(session, dict) else {})
    created = datetime.now().astimezone()
    ttl_hours = max(int(ttl), 1)
    expires = created + timedelta(hours=ttl_hours)

    ticket_id = f"REQ-{created.strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:6].upper()}"
    metadata: dict[str, Any] = {}
    session_id = session_data.get("id")
    if isinstance(session_id, str) and session_id.strip():
        metadata["session_id"] = session_id
    actor_id = str(session_data.get("actor", "unknown"))
    root_raw = session_data.get("root")
    root = Path(str(root_raw)).resolve() if isinstance(root_raw, str) and root_raw.strip() else Path(".").resolve()
    normalized_scope = _normalize_scope(scope)
    nonce = uuid4().hex
    intent_text = intent.strip()

    payload_hash_value = ledger.payload_hash(
        {
            "intent": intent_text,
            "scope": normalized_scope,
            "ttl_hours": ttl_hours,
            "nonce": nonce,
        }
    )

    topic_id = f"repo:{root.as_posix()}"
    expected_head = ledger.head(root, topic_id)
    intent_ref = ledger.intent_submitted(
        root,
        intent_id=ticket_id,
        actor_id=actor_id,
        topic_id=topic_id,
        payload_hash_value=payload_hash_value,
        metadata={"session_id": session_id} if isinstance(session_id, str) and session_id.strip() else {},
        expected_head=expected_head,
        max_head_attempts=3,
    )
    metadata["intent_record"] = {
        "log_path": intent_ref.log_path,
        "line": intent_ref.line,
        "record_hash": intent_ref.record_hash,
        "record_type": intent_ref.record_type,
    }

    return Ticket(
        id=ticket_id,
        intent=intent_text,
        scope=normalized_scope,
        ttl_hours=ttl_hours,
        created_at=created.isoformat(timespec="seconds"),
        expires_at=expires.isoformat(timespec="seconds"),
        nonce=nonce,
        metadata=metadata,
    )


def validate_ticket(gov: Governance, ticket: Ticket | dict[str, Any]) -> TicketStatus:
    _ = gov
    data = to_dict(ticket) if isinstance(ticket, Ticket) else (ticket if isinstance(ticket, dict) else {})
    reasons: list[str] = []

    ticket_id = data.get("id")
    if not isinstance(ticket_id, str) or not ticket_id.strip():
        reasons.append("ticket.id must be a non-empty string")
    is_persistent = isinstance(ticket_id, str) and (
        ticket_id.startswith("TICKET-") or ticket_id.startswith("GOV-") or ticket_id.startswith("PRACTICE-")
    )

    intent = data.get("intent")
    if (not isinstance(intent, str) or not intent.strip()) and is_persistent:
        title = data.get("title")
        if isinstance(title, str) and title.strip():
            intent = title
    if not isinstance(intent, str) or not intent.strip():
        reasons.append("ticket.intent must be a non-empty string")

    nonce = data.get("nonce")
    if (not isinstance(nonce, str) or not nonce.strip()) and not is_persistent:
        reasons.append("ticket.nonce is required for replay protection")
    if not isinstance(nonce, str):
        nonce = ""

    scope = _normalize_scope(data.get("scope") if isinstance(data.get("scope"), dict) else {})
    if not scope.get("allow"):
        reasons.append("ticket.scope.allow must include at least one pattern")

    ttl_hours = data.get("ttl_hours")
    if not isinstance(ttl_hours, int):
        if isinstance(data.get("budgets"), dict):
            raw_ttl = data.get("budgets", {}).get("ttl_hours", 2)
            try:
                ttl_hours = int(raw_ttl)
            except (TypeError, ValueError):
                ttl_hours = 2
        else:
            ttl_hours = 2
    if ttl_hours < 1:
        reasons.append("ticket.ttl_hours must be an integer >= 1")

    created_at_raw = data.get("created_at")
    expires_at_raw = data.get("expires_at")
    created_at: datetime | None = None
    expires_at: datetime | None = None
    if not isinstance(created_at_raw, str):
        if is_persistent:
            created_at_raw = now_iso()
        else:
            reasons.append("ticket.created_at must be RFC3339 date-time")
    if isinstance(created_at_raw, str):
        try:
            created_at = datetime.fromisoformat(created_at_raw)
        except ValueError:
            reasons.append("ticket.created_at must be RFC3339 date-time")

    if not isinstance(expires_at_raw, str):
        if not is_persistent:
            reasons.append("ticket.expires_at must be RFC3339 date-time")
    if isinstance(expires_at_raw, str):
        try:
            expires_at = datetime.fromisoformat(expires_at_raw)
        except ValueError:
            reasons.append("ticket.expires_at must be RFC3339 date-time")

    now = datetime.now().astimezone()
    if expires_at is not None and expires_at <= now:
        reasons.append("ticket is expired")
    if created_at and expires_at and expires_at <= created_at:
        reasons.append("ticket.expires_at must be greater than ticket.created_at")

    normalized: dict[str, Any] = {
        "id": ticket_id,
        "intent": intent,
        "scope": scope,
        "ttl_hours": ttl_hours,
        "created_at": created_at_raw,
        "expires_at": expires_at_raw,
        "nonce": nonce,
        "budgets": data.get("budgets") if isinstance(data.get("budgets"), dict) else {},
        "metadata": data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
    }

    status = "VALID" if not reasons else "INVALID"
    return TicketStatus(status=status, reasons=reasons, normalized_ticket=normalized)
