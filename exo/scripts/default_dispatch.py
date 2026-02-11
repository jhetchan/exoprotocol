from __future__ import annotations

from typing import Any

from exo.stdlib import dispatch


def run(payload: dict[str, Any]) -> dict[str, Any]:
    tickets = payload.get("tickets", [])
    if not isinstance(tickets, list):
        return {}
    raw_scheduler = payload.get("scheduler")
    scheduler = raw_scheduler if isinstance(raw_scheduler, dict) else None
    raw_active_lock = payload.get("active_lock")
    active_lock = raw_active_lock if isinstance(raw_active_lock, dict) else None

    result = dispatch.choose_next_ticket(tickets, scheduler=scheduler, active_lock=active_lock)
    ticket = result.get("ticket")
    if not ticket:
        return {"ticket_id": None, "reasoning": result.get("reasoning", {})}
    return {
        "ticket_id": ticket.get("id"),
        "reasoning": result.get("reasoning", {}),
    }
