from __future__ import annotations

from typing import Any


def run(payload: dict[str, Any]) -> dict[str, Any]:
    ticket = payload.get("ticket") or {}
    checks = ticket.get("checks") if isinstance(ticket, dict) else []
    if not isinstance(checks, list):
        checks = []
    return {"checks": checks}
