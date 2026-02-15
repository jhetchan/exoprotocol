# @feature:ticket-system
# @req: REQ-LOCK-001
from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    import fcntl
except ModuleNotFoundError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from . import ledger
from .errors import ExoError
from .types import INTENT_RISKS, TICKET_KINDS, Governance, Session, Ticket, TicketStatus, to_dict
from .utils import default_topic_id, dump_json, dump_yaml, gen_timestamp_id, load_json, load_yaml, now_iso

TICKETS_DIR = Path(".exo/tickets")
LOCK_FILE = Path(".exo/locks/ticket.lock.json")
LOCK_GUARD_FILE = Path(".exo/locks/.ticket.lock.guard")
ID_GUARD_FILE = Path(".exo/locks/.id.guard")


TICKET_ID_RE = re.compile(r"^TICKET-(\d+)(?:-EPIC)?$")
INTENT_ID_RE = re.compile(r"^INTENT-(\d+)$")
# Match both legacy (TICKET-001, INTENT-001) and new (TKT-YYYYMMDD-..., INT-YYYYMMDD-...) formats
PATH_RE = re.compile(
    r"^(TICKET-\d+(?:-EPIC)?|INTENT-\d+|TKT-\d{8}-\d{6}-[A-Z0-9]{4}(?:-EPIC)?|INT-\d{8}-\d{6}-[A-Z0-9]{4}|GOV-\d+|PRACTICE-\d+)\.ya?ml$"
)


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

    # Intent accountability fields
    kind_raw = str(normalized.get("kind") or "task").strip().lower()
    normalized["kind"] = kind_raw if kind_raw in TICKET_KINDS else "task"
    normalized.setdefault("brain_dump", "")
    normalized.setdefault("boundary", "")
    normalized.setdefault("success_condition", "")
    risk_raw = str(normalized.get("risk") or "medium").strip().lower()
    normalized["risk"] = risk_raw if risk_raw in INTENT_RISKS else "medium"
    normalized.setdefault("children", [])

    # Resource profile (machine awareness)
    rp_raw = str(normalized.get("resource_profile") or "default").strip().lower()
    normalized["resource_profile"] = rp_raw if rp_raw in {"default", "light", "heavy"} else "default"

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
        raise ExoError(
            code="TICKET_NOT_FOUND",
            message=f"Ticket not found: {ticket_id}",
            details={
                "ticket_id": ticket_id,
                "expected_path": str(path.relative_to(repo))
                if repo in path.parents or path.parent == repo
                else str(path),
                "hint": f"No ticket file exists for '{ticket_id}'. Create it first with `exo intent-create` or `exo ticket-create`.",
            },
        )
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


def next_ticket_id(repo: Path) -> str:
    return gen_timestamp_id("TKT")


def next_intent_id(repo: Path) -> str:
    return gen_timestamp_id("INT")


@contextmanager
def _id_guard(repo: Path):
    """File lock for atomic ticket/intent ID allocation."""
    if fcntl is None:
        yield
        return
    guard = repo / ID_GUARD_FILE
    guard.parent.mkdir(parents=True, exist_ok=True)
    with guard.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def allocate_ticket_id(repo: Path, *, kind: str = "task") -> str:
    """Atomically allocate the next ticket ID and reserve it on disk.

    Holds a file lock while scanning for the max ID and writing a
    minimal placeholder file, preventing TOCTOU races between
    concurrent callers.
    """
    with _id_guard(repo):
        tid = next_ticket_id(repo)
        if kind == "epic":
            tid = f"{tid}-EPIC"
        path = ticket_path(repo, tid)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            dump_yaml(path, {"id": tid, "status": "todo", "kind": kind, "created_at": now_iso()})
        return tid


def allocate_intent_id(repo: Path) -> str:
    """Atomically allocate the next intent ID and reserve it on disk.

    Same concurrency-safe pattern as allocate_ticket_id.
    """
    with _id_guard(repo):
        tid = next_intent_id(repo)
        path = ticket_path(repo, tid)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            dump_yaml(path, {"id": tid, "status": "todo", "kind": "intent", "created_at": now_iso()})
        return tid


def resolve_intent_root(repo: Path, ticket: dict[str, Any], *, max_depth: int = 20) -> dict[str, Any] | None:
    """Walk parent_id chain upward to find the root intent ticket.

    Returns the intent ticket dict, or None if the chain is broken or no
    intent root exists.
    """
    visited: set[str] = set()
    current = ticket
    for _ in range(max_depth):
        current_id = str(current.get("id", "")).strip()
        if not current_id or current_id in visited:
            return None
        visited.add(current_id)

        if str(current.get("kind", "task")).strip().lower() == "intent":
            return current

        parent_id = current.get("parent_id")
        if not isinstance(parent_id, str) or not parent_id.strip():
            return None

        try:
            current = load_ticket(repo, parent_id.strip())
        except ExoError:
            return None

    return None


def validate_intent_hierarchy(repo: Path, ticket: dict[str, Any]) -> list[str]:
    """Validate that a ticket's parent chain terminates at a kind=intent root.

    Returns a list of warning/error reason strings (empty = valid).
    Only enforced for TICKET-* and INTENT-* IDs; GOV-*/PRACTICE-* are exempt.
    """
    ticket_id = str(ticket.get("id", "")).strip()
    kind = str(ticket.get("kind", "task")).strip().lower()
    reasons: list[str] = []

    # Governance and practice tickets are exempt from intent hierarchy
    if ticket_id.startswith(("GOV-", "PRACTICE-")):
        return reasons

    if kind == "intent":
        # Intents are roots — no parent required
        brain_dump = str(ticket.get("brain_dump", "")).strip()
        if not brain_dump:
            reasons.append("intent ticket should include brain_dump (original user input)")
        return reasons

    if kind == "epic":
        parent_id = ticket.get("parent_id")
        if not isinstance(parent_id, str) or not parent_id.strip():
            reasons.append("epic ticket must have parent_id linking to an intent")
            return reasons

    if kind == "task":
        parent_id = ticket.get("parent_id")
        if not isinstance(parent_id, str) or not parent_id.strip():
            reasons.append("task ticket has no parent_id; cannot trace to intent root")
            return reasons

    # Walk upward to find intent root
    root = resolve_intent_root(repo, ticket)
    if root is None:
        reasons.append(f"parent chain for {ticket_id} does not terminate at a kind=intent root")

    return reasons


def lock_path(repo: Path) -> Path:
    return repo / LOCK_FILE


def lock_guard_path(repo: Path) -> Path:
    return repo / LOCK_GUARD_FILE


@contextmanager
def _lock_guard(repo: Path):
    if fcntl is None:
        yield
        return
    guard = lock_guard_path(repo)
    guard.parent.mkdir(parents=True, exist_ok=True)
    with guard.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_lock_from_path(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    lock = load_json(path)
    if is_lock_expired(lock):
        path.unlink(missing_ok=True)
        return None
    return lock


def write_lock(repo: Path, lock: dict[str, Any]) -> dict[str, Any]:
    with _lock_guard(repo):
        dump_json(lock_path(repo), lock)
    return lock


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _normalize_duration_hours(duration_hours: int) -> int:
    try:
        value = int(duration_hours)
    except (TypeError, ValueError):
        raise ExoError(
            code="LOCK_DURATION_INVALID",
            message="duration_hours must be an integer >= 1",
            blocked=True,
        ) from None
    if value < 1:
        raise ExoError(
            code="LOCK_DURATION_INVALID",
            message="duration_hours must be an integer >= 1",
            blocked=True,
        )
    return value


def is_lock_expired(lock: dict[str, Any]) -> bool:
    expires = lock.get("expires_at")
    if not expires:
        return True
    return datetime.now().astimezone() >= _parse_dt(expires)


def load_lock(repo: Path) -> dict[str, Any] | None:
    return _load_lock_from_path(lock_path(repo))


def acquire_lock(
    repo: Path,
    ticket_id: str,
    *,
    owner: str = "human",
    role: str = "developer",
    duration_hours: int = 2,
    base: str = "main",
) -> dict[str, Any]:
    normalized_duration = _normalize_duration_hours(duration_hours)
    lock_file = lock_path(repo)
    with _lock_guard(repo):
        existing = _load_lock_from_path(lock_file)
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
            renewed_expires = renewed_at + timedelta(hours=normalized_duration)
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
            dump_json(lock_file, renewed)
            return renewed

        created = datetime.now().astimezone()
        expires = created + timedelta(hours=normalized_duration)
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
        dump_json(lock_file, lock)
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


def heartbeat_lock(
    repo: Path,
    ticket_id: str | None = None,
    *,
    owner: str | None = None,
    duration_hours: int = 2,
) -> dict[str, Any]:
    normalized_duration = _normalize_duration_hours(duration_hours)
    lock_file = lock_path(repo)
    with _lock_guard(repo):
        existing = _load_lock_from_path(lock_file)
        if not existing:
            raise ExoError(
                code="LOCK_REQUIRED",
                message="No active ticket lock found",
                blocked=True,
            )
        if ticket_id and existing.get("ticket_id") != ticket_id:
            raise ExoError(
                code="LOCK_MISMATCH",
                message=f"Active lock is for {existing.get('ticket_id')}, not {ticket_id}",
                details=existing,
                blocked=True,
            )

        existing_owner = str(existing.get("owner", "")).strip()
        requested_owner = owner.strip() if isinstance(owner, str) and owner.strip() else ""
        if requested_owner and existing_owner and requested_owner != existing_owner:
            raise ExoError(
                code="LOCK_OWNER_MISMATCH",
                message=f"Active lock owner is {existing_owner}; heartbeat owner {requested_owner} is not allowed",
                details=existing,
                blocked=True,
            )

        now = datetime.now().astimezone()
        candidate_expiry = now + timedelta(hours=normalized_duration)
        current_expiry_raw = existing.get("expires_at")
        try:
            current_expiry = _parse_dt(str(current_expiry_raw)) if current_expiry_raw else now
        except Exception:
            current_expiry = now
        next_expiry = candidate_expiry if candidate_expiry >= current_expiry else current_expiry

        updated = dict(existing)
        updated["updated_at"] = now.isoformat(timespec="seconds")
        updated["heartbeat_at"] = now.isoformat(timespec="seconds")
        updated["expires_at"] = next_expiry.isoformat(timespec="seconds")
        updated["lease_expires_at"] = next_expiry.isoformat(timespec="seconds")

        dump_json(lock_file, updated)
        return updated


def release_lock(repo: Path, ticket_id: str | None = None) -> bool:
    lock_file = lock_path(repo)
    with _lock_guard(repo):
        existing = _load_lock_from_path(lock_file)
        if not existing:
            return False
        if ticket_id and existing.get("ticket_id") != ticket_id:
            return False
        lock_file.unlink(missing_ok=True)
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

    topic_id = default_topic_id(root)
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
        ticket_id.startswith("TICKET-")
        or ticket_id.startswith("INTENT-")
        or ticket_id.startswith("GOV-")
        or ticket_id.startswith("PRACTICE-")
    )

    # Validate kind and risk enums
    kind_raw = data.get("kind")
    if isinstance(kind_raw, str) and kind_raw.strip() and kind_raw.strip().lower() not in TICKET_KINDS:
        reasons.append(f"ticket.kind must be one of {sorted(TICKET_KINDS)}")
    risk_raw = data.get("risk")
    if isinstance(risk_raw, str) and risk_raw.strip() and risk_raw.strip().lower() not in INTENT_RISKS:
        reasons.append(f"ticket.risk must be one of {sorted(INTENT_RISKS)}")

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

    if not isinstance(expires_at_raw, str) and not is_persistent:
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
        "kind": str(data.get("kind") or "task").strip().lower(),
        "brain_dump": str(data.get("brain_dump") or ""),
        "boundary": str(data.get("boundary") or ""),
        "success_condition": str(data.get("success_condition") or ""),
        "risk": str(data.get("risk") or "medium").strip().lower(),
        "children": data.get("children") if isinstance(data.get("children"), list) else [],
    }

    status = "VALID" if not reasons else "INVALID"
    return TicketStatus(status=status, reasons=reasons, normalized_ticket=normalized)
