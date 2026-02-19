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
    kind: str = "task",
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
        "kind": kind,
    }


def test_dispatch_falls_back_to_priority_scoring_without_scheduler() -> None:
    tickets = [
        _ticket("TICKET-001", priority=1, age_hours=1),
        _ticket("TICKET-002", priority=5, age_hours=1),
    ]

    result = dispatch.choose_next_ticket(tickets)

    assert result["ticket"] is not None
    # Priority 1 (highest urgency) dispatches before priority 5 (lowest)
    assert result["ticket"]["id"] == "TICKET-001"
    assert result["reasoning"]["formula"] == "score = (6 - priority)*100 + blockers_count*30 + age_hours*0.1"
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


# --- Priority ordering ---


def test_priority_1_dispatches_before_priority_5() -> None:
    """Priority 1 = highest urgency, dispatches first."""
    tickets = [
        _ticket("LOW", priority=5, age_hours=1),
        _ticket("HIGH", priority=1, age_hours=1),
        _ticket("MED", priority=3, age_hours=1),
    ]
    result = dispatch.choose_next_ticket(tickets)
    assert result["ticket"]["id"] == "HIGH"


def test_priority_ordering_full_range() -> None:
    """Verify dispatch order matches priority 1 > 2 > 3 > 4 > 5."""
    tickets = [_ticket(f"P{p}", priority=p, age_hours=1) for p in [5, 3, 1, 4, 2]]
    # Score all and sort to verify full ordering
    now = datetime.now().astimezone()
    scored = sorted(tickets, key=lambda t: -dispatch.score_ticket(t, now))
    assert [t["id"] for t in scored] == ["P1", "P2", "P3", "P4", "P5"]


def test_score_ticket_priority_1_beats_priority_5() -> None:
    now = datetime.now().astimezone()
    high = _ticket("H", priority=1, age_hours=0)
    low = _ticket("L", priority=5, age_hours=0)
    assert dispatch.score_ticket(high, now) > dispatch.score_ticket(low, now)


# --- Kind filtering (epic/intent skip) ---


def test_dispatch_skips_epic_tickets() -> None:
    """Epics are containers, not dispatchable work items."""
    tickets = [
        _ticket("EPIC-001", kind="epic", priority=1),
        _ticket("TASK-001", kind="task", priority=3),
    ]
    result = dispatch.choose_next_ticket(tickets)
    assert result["ticket"]["id"] == "TASK-001"


def test_dispatch_skips_intent_tickets() -> None:
    """Intents are containers, not dispatchable work items."""
    tickets = [
        _ticket("INT-001", kind="intent", priority=1),
        _ticket("TASK-001", kind="task", priority=3),
    ]
    result = dispatch.choose_next_ticket(tickets)
    assert result["ticket"]["id"] == "TASK-001"


def test_dispatch_returns_none_when_only_epics_and_intents() -> None:
    """If all todo tickets are containers, nothing is dispatchable."""
    tickets = [
        _ticket("EPIC-001", kind="epic", priority=1),
        _ticket("INT-001", kind="intent", priority=1),
    ]
    result = dispatch.choose_next_ticket(tickets)
    assert result["ticket"] is None


def test_dispatch_treats_missing_kind_as_task() -> None:
    """Backward compat: tickets without 'kind' field are treated as tasks."""
    raw_ticket = {
        "id": "OLD-001",
        "status": "todo",
        "type": "feature",
        "priority": 1,
        "created_at": datetime.now().astimezone().isoformat(),
        "blockers": [],
    }
    # No 'kind' key at all
    result = dispatch.choose_next_ticket([raw_ticket])
    assert result["ticket"] is not None
    assert result["ticket"]["id"] == "OLD-001"


def test_dispatch_epic_with_task_children_dispatches_task() -> None:
    """Epic parent is skipped, child task is dispatched."""
    tickets = [
        _ticket("EPIC-001", kind="epic", priority=1),
        _ticket("TASK-001", kind="task", priority=2),
        _ticket("TASK-002", kind="task", priority=4),
    ]
    result = dispatch.choose_next_ticket(tickets)
    # Task with priority 2 dispatches (higher urgency than priority 4)
    assert result["ticket"]["id"] == "TASK-001"
