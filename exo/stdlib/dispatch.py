from __future__ import annotations

from datetime import datetime
from typing import Any

from exo.kernel.tickets import blockers_resolved


def _ticket_age_hours(ticket: dict[str, Any], now: datetime) -> float:
    created_raw = ticket.get("created_at")
    if not created_raw:
        return 0.0
    try:
        created = datetime.fromisoformat(created_raw)
    except ValueError:
        return 0.0
    delta = now - created
    return max(delta.total_seconds() / 3600.0, 0.0)


def _unblocks_count(ticket: dict[str, Any]) -> int:
    data = ticket.get("unblocks")
    if isinstance(data, list):
        return len(data)
    return 0


def score_ticket(ticket: dict[str, Any], now: datetime) -> float:
    priority = int(ticket.get("priority", 3))
    blockers_count = _unblocks_count(ticket)
    age_hours = _ticket_age_hours(ticket, now)
    return priority * 100 + blockers_count * 30 + age_hours * 0.1


def choose_next_ticket(tickets: list[dict[str, Any]]) -> dict[str, Any]:
    now = datetime.now().astimezone()
    index = {str(ticket.get("id")): ticket for ticket in tickets}

    candidates: list[dict[str, Any]] = []
    for ticket in tickets:
        if ticket.get("status") != "todo":
            continue
        if not blockers_resolved(ticket, index):
            continue
        entry = dict(ticket)
        entry["_score"] = score_ticket(ticket, now)
        entry["_age_hours"] = _ticket_age_hours(ticket, now)
        entry["_unblocks"] = _unblocks_count(ticket)
        candidates.append(entry)

    if not candidates:
        return {
            "ticket": None,
            "reasoning": {
                "candidate_count": 0,
                "reason": "No todo tickets with resolved blockers",
            },
        }

    candidates.sort(
        key=lambda t: (
            -float(t["_score"]),
            -int(t.get("priority", 3)),
            -int(t["_unblocks"]),
            -float(t["_age_hours"]),
            str(t.get("id")),
        )
    )

    chosen = candidates[0]
    reasoning = {
        "candidate_count": len(candidates),
        "formula": "score = priority*100 + blockers_count*30 + age_hours*0.1",
        "score": chosen["_score"],
        "priority": chosen.get("priority", 3),
        "unblocks_count": chosen["_unblocks"],
        "age_hours": round(float(chosen["_age_hours"]), 2),
        "tie_breakers": ["priority", "unblocks_count", "oldest"],
    }

    chosen.pop("_score", None)
    chosen.pop("_age_hours", None)
    chosen.pop("_unblocks", None)
    return {"ticket": chosen, "reasoning": reasoning}
