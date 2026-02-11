from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from exo.stdlib import dispatch


def _ticket(
    ticket_id: str,
    *,
    status: str = "todo",
    ticket_type: str = "feature",
    priority: int = 3,
    age_hours: int = 1,
    blockers: list[str] | None = None,
    unblocks: list[str] | None = None,
) -> dict[str, Any]:
    created_at = (datetime.now().astimezone() - timedelta(hours=age_hours)).isoformat(timespec="seconds")
    return {
        "id": ticket_id,
        "status": status,
        "type": ticket_type,
        "priority": priority,
        "created_at": created_at,
        "blockers": blockers or [],
        "unblocks": unblocks or [],
    }


def test_dispatch_falls_back_to_priority_scoring_without_scheduler() -> None:
    tickets = [
        _ticket("TICKET-001", priority=4, age_hours=1),
        _ticket("TICKET-002", priority=5, age_hours=1),
    ]

    result = dispatch.choose_next_ticket(tickets)

    assert result["ticket"] is not None
    assert result["ticket"]["id"] == "TICKET-002"
    assert result["reasoning"]["formula"] == "score = priority*100 + blockers_count*30 + age_hours*0.1"
    assert result["reasoning"]["scheduler"]["enabled"] is False


def test_dispatch_blocks_full_lane_and_selects_open_lane() -> None:
    tickets = [
        _ticket("TICKET-010", status="active", ticket_type="feature", priority=5),
        _ticket("TICKET-011", status="todo", ticket_type="feature", priority=5, age_hours=12),
        _ticket("TICKET-012", status="todo", ticket_type="bug", priority=3, age_hours=1),
    ]
    scheduler = {
        "enabled": True,
        "lanes": [
            {"name": "Feature Lane", "allowed_types": ["feature", "refactor"], "count": 1},
            {"name": "Bug Lane", "allowed_types": ["bug", "security"], "count": 1},
        ],
    }

    result = dispatch.choose_next_ticket(tickets, scheduler=scheduler, active_lock={"ticket_id": "TICKET-010"})

    assert result["ticket"] is not None
    assert result["ticket"]["id"] == "TICKET-012"
    assert result["reasoning"]["selected_lane"] == "Bug Lane"
    assert result["reasoning"]["scheduler"]["active_lock_ticket"] == "TICKET-010"


def test_dispatch_reports_lane_capacity_when_all_candidates_blocked() -> None:
    tickets = [
        _ticket("TICKET-020", status="active", ticket_type="feature", priority=5),
        _ticket("TICKET-021", status="todo", ticket_type="feature", priority=5),
    ]
    scheduler = {
        "enabled": True,
        "lanes": [
            {"name": "Feature Lane", "allowed_types": ["feature"], "count": 1},
        ],
    }

    result = dispatch.choose_next_ticket(tickets, scheduler=scheduler)

    assert result["ticket"] is None
    assert result["reasoning"]["reason"] == "All dispatchable tickets are blocked by lane capacity"
    blocked = result["reasoning"]["blocked_candidates"]
    assert len(blocked) == 1
    assert blocked[0]["ticket_id"] == "TICKET-021"
    assert blocked[0]["reason"] == "lane_full"


def test_dispatch_reports_global_concurrency_capacity() -> None:
    tickets = [
        _ticket("TICKET-030", status="active", ticket_type="bug", priority=5),
        _ticket("TICKET-031", status="todo", ticket_type="bug", priority=5),
    ]
    scheduler = {
        "enabled": True,
        "global_concurrency_limit": 1,
    }

    result = dispatch.choose_next_ticket(tickets, scheduler=scheduler)

    assert result["ticket"] is None
    assert result["reasoning"]["reason"] == "Global concurrency limit reached"
    blocked = result["reasoning"]["blocked_candidates"]
    assert len(blocked) == 1
    assert blocked[0]["ticket_id"] == "TICKET-031"
    assert blocked[0]["reason"] == "global_limit"
