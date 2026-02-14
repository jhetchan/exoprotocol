"""Intent timeline: trace every ticket back to its declared intent."""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from exo.kernel.tickets import load_all_tickets, resolve_intent_root
from exo.kernel.utils import now_iso

SESSION_INDEX_PATH = Path(".exo/memory/sessions/index.jsonl")


def _load_session_index(repo: Path) -> list[dict[str, Any]]:
    path = repo / SESSION_INDEX_PATH
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def _age_label(iso_ts: str | None) -> str:
    if not iso_ts:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso_ts))
        delta = datetime.now().astimezone() - dt
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return f"{int(delta.total_seconds() / 60)}m ago"
        if hours < 24:
            return f"{int(hours)}h ago"
        return f"{int(hours / 24)}d ago"
    except (TypeError, ValueError):
        return ""


def build_intent_timeline(repo: Path) -> dict[str, Any]:
    """Build a structured timeline of all intents and their descendants.

    Returns:
        {
            "intents": [...],
            "orphan_tickets": [...],
            "summary": {...}
        }
    """
    repo = Path(repo).resolve()
    all_tickets = load_all_tickets(repo)
    sessions = _load_session_index(repo)

    # Index tickets by ID
    ticket_index: dict[str, dict[str, Any]] = {}
    for t in all_tickets:
        tid = str(t.get("id", "")).strip()
        if tid:
            ticket_index[tid] = t

    # Index sessions by ticket_id
    sessions_by_ticket: dict[str, list[dict[str, Any]]] = {}
    for s in sessions:
        tid = str(s.get("ticket_id", "")).strip()
        if tid:
            sessions_by_ticket.setdefault(tid, []).append(s)

    # Classify tickets
    intents: list[dict[str, Any]] = []
    epics: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    orphans: list[dict[str, Any]] = []

    for t in all_tickets:
        kind = str(t.get("kind", "task")).strip().lower()
        tid = str(t.get("id", "")).strip()

        # GOV-* and PRACTICE-* are exempt from intent hierarchy
        if tid.startswith(("GOV-", "PRACTICE-")):
            continue

        if kind == "intent":
            intents.append(t)
        elif kind == "epic":
            epics.append(t)
        else:
            tasks.append(t)

    # Build intent trees
    intent_trees: list[dict[str, Any]] = []

    for intent in intents:
        str(intent.get("id", ""))
        tree = _build_intent_tree(intent, ticket_index, sessions_by_ticket)
        intent_trees.append(tree)

    # Find orphan tickets (tasks/epics that don't trace to an intent)
    for t in epics + tasks:
        tid = str(t.get("id", "")).strip()
        root = resolve_intent_root(repo, t)
        if root is None:
            orphans.append(
                {
                    "id": tid,
                    "kind": str(t.get("kind", "task")),
                    "title": str(t.get("title") or t.get("intent") or ""),
                    "status": str(t.get("status", "")),
                    "parent_id": t.get("parent_id"),
                    "sessions": _format_sessions(sessions_by_ticket.get(tid, [])),
                }
            )

    # Summary stats
    completed_intents = [i for i in intent_trees if i.get("status") == "done"]
    active_intents = [i for i in intent_trees if i.get("status") in {"active", "todo", "in_progress"}]
    stale_intents = [i for i in intent_trees if i.get("stale")]
    drift_scores = [i["drift_avg"] for i in intent_trees if i.get("drift_avg") is not None]
    avg_drift = round(sum(drift_scores) / len(drift_scores), 3) if drift_scores else None

    return {
        "intents": intent_trees,
        "orphan_tickets": orphans,
        "summary": {
            "total_intents": len(intent_trees),
            "completed": len(completed_intents),
            "active": len(active_intents),
            "stale": len(stale_intents),
            "orphan_tickets": len(orphans),
            "avg_drift": avg_drift,
            "generated_at": now_iso(),
        },
    }


def _build_intent_tree(
    intent: dict[str, Any],
    ticket_index: dict[str, dict[str, Any]],
    sessions_by_ticket: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Build a tree for a single intent, including its epics and tasks."""
    intent_id = str(intent.get("id", ""))
    children_ids = intent.get("children") or []

    # Find all tickets that reference this intent as parent
    descendants: list[dict[str, Any]] = []
    for cid in children_ids:
        cid_str = str(cid).strip()
        child = ticket_index.get(cid_str)
        if child:
            descendants.append(_format_descendant(child, ticket_index, sessions_by_ticket))

    # Also find tickets that reference this intent via parent_id but aren't in children
    child_set = set(str(c).strip() for c in children_ids)
    for tid, t in ticket_index.items():
        parent = str(t.get("parent_id", "")).strip()
        if parent == intent_id and tid not in child_set:
            descendants.append(_format_descendant(t, ticket_index, sessions_by_ticket))

    # Collect drift scores from sessions
    all_sessions: list[dict[str, Any]] = []
    all_sessions.extend(sessions_by_ticket.get(intent_id, []))
    for desc in descendants:
        all_sessions.extend(sessions_by_ticket.get(desc.get("id", ""), []))

    drift_scores = []
    for s in all_sessions:
        drift_raw = s.get("drift_score")
        if drift_raw is not None:
            with contextlib.suppress(TypeError, ValueError):
                drift_scores.append(float(drift_raw))

    drift_avg = round(sum(drift_scores) / len(drift_scores), 3) if drift_scores else None

    # Check staleness
    stale = _is_stale(intent, all_sessions)

    return {
        "id": intent_id,
        "kind": "intent",
        "title": str(intent.get("title") or intent.get("intent") or ""),
        "status": str(intent.get("status", "")),
        "risk": str(intent.get("risk", "medium")),
        "boundary": str(intent.get("boundary", "")),
        "success_condition": str(intent.get("success_condition", "")),
        "brain_dump_length": len(str(intent.get("brain_dump", ""))),
        "created_at": str(intent.get("created_at", "")),
        "descendants": descendants,
        "descendant_count": len(descendants),
        "sessions": _format_sessions(sessions_by_ticket.get(intent_id, [])),
        "drift_avg": drift_avg,
        "stale": stale,
    }


def _format_descendant(
    ticket: dict[str, Any],
    ticket_index: dict[str, dict[str, Any]],
    sessions_by_ticket: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    tid = str(ticket.get("id", ""))
    kind = str(ticket.get("kind", "task"))

    # If epic, find its children
    children: list[dict[str, Any]] = []
    if kind == "epic":
        child_ids = ticket.get("children") or []
        for cid in child_ids:
            child = ticket_index.get(str(cid).strip())
            if child:
                children.append(_format_descendant(child, ticket_index, sessions_by_ticket))

    return {
        "id": tid,
        "kind": kind,
        "title": str(ticket.get("title") or ticket.get("intent") or ""),
        "status": str(ticket.get("status", "")),
        "parent_id": ticket.get("parent_id"),
        "sessions": _format_sessions(sessions_by_ticket.get(tid, [])),
        "children": children,
    }


def _format_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    formatted: list[dict[str, Any]] = []
    for s in sessions:
        formatted.append(
            {
                "session_id": str(s.get("session_id", "")),
                "actor": str(s.get("actor", "")),
                "vendor": str(s.get("vendor", "")),
                "model": str(s.get("model", "")),
                "started_at": str(s.get("started_at", "")),
                "finished_at": str(s.get("finished_at", "")),
                "age": _age_label(s.get("finished_at") or s.get("started_at")),
                "verify": str(s.get("verify", "")),
                "drift_score": s.get("drift_score"),
            }
        )
    return formatted


def _is_stale(ticket: dict[str, Any], sessions: list[dict[str, Any]]) -> bool:
    """A ticket is stale if it's not done and has no session activity in 5 days."""
    status = str(ticket.get("status", "")).strip().lower()
    if status in {"done", "archived", "cancelled"}:
        return False

    if not sessions:
        # No sessions at all — check if ticket is old
        created = ticket.get("created_at")
        if isinstance(created, str):
            try:
                dt = datetime.fromisoformat(created)
                hours = (datetime.now().astimezone() - dt).total_seconds() / 3600
                return hours > 120  # 5 days
            except (TypeError, ValueError):
                pass
        return False

    # Check most recent session
    latest_ts: str | None = None
    for s in sessions:
        ts = s.get("finished_at") or s.get("started_at")
        if isinstance(ts, str) and (latest_ts is None or ts > latest_ts):
            latest_ts = ts

    if latest_ts:
        try:
            dt = datetime.fromisoformat(latest_ts)
            hours = (datetime.now().astimezone() - dt).total_seconds() / 3600
            return hours > 120  # 5 days
        except (TypeError, ValueError):
            pass

    return False


def format_timeline_human(timeline: dict[str, Any]) -> str:
    """Format intent timeline for human-readable CLI output."""
    lines: list[str] = []
    intents = timeline.get("intents", [])

    for intent in intents:
        drift_label = f"drift:{intent['drift_avg']}" if intent.get("drift_avg") is not None else "drift:—"
        stale_label = "  STALE" if intent.get("stale") else ""
        status = intent.get("status", "")

        lines.append(
            f"{intent['id']}  {intent.get('kind', 'intent')}  "
            f'"{intent.get("title", "")}"  '
            f"{status}  {drift_label}{stale_label}"
        )

        # Show sessions on intent itself
        for s in intent.get("sessions", []):
            drift_s = f"drift:{s['drift_score']}" if s.get("drift_score") is not None else "drift:—"
            lines.append(
                f"  ├─ {s['session_id'][:20]}  {s.get('model', '')}  "
                f"{s.get('verify', '')}  {drift_s}  {s.get('age', '')}"
            )

        # Show descendants
        for desc in intent.get("descendants", []):
            _format_descendant_human(desc, lines, depth=1)

        # Show boundary
        boundary = intent.get("boundary", "")
        if boundary:
            lines.append(f'  └─ boundary: "{boundary}"')

        lines.append("")

    # Show orphans
    orphans = timeline.get("orphan_tickets", [])
    if orphans:
        lines.append("Orphan tickets (no intent root):")
        for o in orphans:
            lines.append(f'  {o["id"]}  {o.get("kind", "task")}  "{o.get("title", "")}"  {o.get("status", "")}')
        lines.append("")

    # Summary
    summary = timeline.get("summary", {})
    parts: list[str] = []
    if summary.get("completed"):
        avg = f" (avg drift {summary['avg_drift']})" if summary.get("avg_drift") is not None else ""
        parts.append(f"{summary['completed']} completed{avg}")
    if summary.get("active"):
        parts.append(f"{summary['active']} active")
    if summary.get("stale"):
        parts.append(f"{summary['stale']} stale")
    if summary.get("orphan_tickets"):
        parts.append(f"{summary['orphan_tickets']} orphan")
    if parts:
        lines.append("Summary: " + ", ".join(parts))

    return "\n".join(lines)


def _format_descendant_human(desc: dict[str, Any], lines: list[str], depth: int) -> None:
    indent = "  " * depth
    prefix = "├─" if depth > 0 else ""
    status = desc.get("status", "")
    lines.append(f'{indent}{prefix} {desc["id"]}  {desc.get("kind", "task")}  "{desc.get("title", "")}"  {status}')

    for s in desc.get("sessions", []):
        drift_s = f"drift:{s['drift_score']}" if s.get("drift_score") is not None else "drift:—"
        lines.append(
            f"{indent}  │ {s['session_id'][:20]}  {s.get('model', '')}  "
            f"{s.get('verify', '')}  {drift_s}  {s.get('age', '')}"
        )

    for child in desc.get("children", []):
        _format_descendant_human(child, lines, depth + 1)
