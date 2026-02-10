from __future__ import annotations

from typing import Any


def run(payload: dict[str, Any]) -> dict[str, Any]:
    ticket = payload.get("ticket") or {}
    ticket_id = ticket.get("id", "UNKNOWN")
    return {
        "summary": (
            f"Default do script executed for {ticket_id}. "
            "No autonomous edits generated. Provide --patch or override .exo/scripts/do.py."
        ),
        "patches": [],
        "commands": [],
        "mark_done": False,
    }
