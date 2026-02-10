from __future__ import annotations

from typing import Any

from exo.stdlib import dispatch


def run(payload: dict[str, Any]) -> dict[str, Any]:
    tickets = payload.get("tickets", [])
    if not isinstance(tickets, list):
        return {}
    result = dispatch.choose_next_ticket(tickets)
    ticket = result.get("ticket")
    if not ticket:
        return {"ticket_id": None, "reasoning": result.get("reasoning", {})}
    return {
        "ticket_id": ticket.get("id"),
        "reasoning": result.get("reasoning", {}),
    }
