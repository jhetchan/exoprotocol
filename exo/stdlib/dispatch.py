from __future__ import annotations

from datetime import datetime
from typing import Any

from exo.kernel.tickets import blockers_resolved


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_lane(raw: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    allowed_raw = raw.get("allowed_types")
    if not isinstance(allowed_raw, list):
        return None
    allowed_types = [str(item).strip().lower() for item in allowed_raw if isinstance(item, str) and item.strip()]
    if not allowed_types:
        return None

    count = max(_safe_int(raw.get("count"), 1), 1)
    name_raw = raw.get("name")
    name = str(name_raw).strip() if isinstance(name_raw, str) and name_raw.strip() else f"lane-{index + 1}"
    return {
        "name": name,
        "allowed_types": allowed_types,
        "count": count,
    }


def _normalize_scheduler(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {
            "enabled": False,
            "lanes": [],
            "global_concurrency_limit": None,
        }

    lanes: list[dict[str, Any]] = []
    lanes_raw = raw.get("lanes")
    if isinstance(lanes_raw, list):
        for index, entry in enumerate(lanes_raw):
            lane = _normalize_lane(entry, index)
            if lane:
                lanes.append(lane)

    global_limit = None
    global_raw = raw.get("global_concurrency_limit")
    if global_raw is not None:
        parsed = _safe_int(global_raw, 0)
        if parsed > 0:
            global_limit = parsed

    enabled_raw = raw.get("enabled")
    enabled = enabled_raw if isinstance(enabled_raw, bool) else bool(lanes or global_limit is not None)

    return {
        "enabled": enabled,
        "lanes": lanes,
        "global_concurrency_limit": global_limit,
    }


def _match_lane(ticket_type: str, lanes: list[dict[str, Any]]) -> dict[str, Any] | None:
    normalized_type = ticket_type.strip().lower()
    for lane in lanes:
        allowed_types = lane.get("allowed_types", [])
        if "*" in allowed_types or normalized_type in allowed_types:
            return lane
    return None


def _active_ticket_count(tickets: list[dict[str, Any]]) -> int:
    return sum(1 for ticket in tickets if str(ticket.get("status", "")).strip().lower() == "active")


def _lane_occupancy(tickets: list[dict[str, Any]], lanes: list[dict[str, Any]]) -> dict[str, int]:
    counts = {str(lane["name"]): 0 for lane in lanes}
    for ticket in tickets:
        if str(ticket.get("status", "")).strip().lower() != "active":
            continue
        ticket_type = str(ticket.get("type", "feature"))
        lane = _match_lane(ticket_type, lanes)
        if lane:
            lane_name = str(lane["name"])
            counts[lane_name] = counts.get(lane_name, 0) + 1
    return counts


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


def choose_next_ticket(
    tickets: list[dict[str, Any]],
    *,
    scheduler: dict[str, Any] | None = None,
    active_lock: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now().astimezone()
    index = {str(ticket.get("id")): ticket for ticket in tickets}
    scheduler_state = _normalize_scheduler(scheduler)
    scheduler_enabled = bool(scheduler_state.get("enabled"))
    lanes = scheduler_state.get("lanes", [])
    global_limit = scheduler_state.get("global_concurrency_limit")
    active_count = _active_ticket_count(tickets)
    lane_counts = _lane_occupancy(tickets, lanes) if scheduler_enabled else {}
    active_lock_ticket = ""
    if isinstance(active_lock, dict):
        active_lock_ticket = str(active_lock.get("ticket_id", "")).strip()

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
                "scheduler": {
                    "enabled": scheduler_enabled,
                    "global_concurrency_limit": global_limit,
                    "active_tickets": active_count,
                    "active_lock_ticket": active_lock_ticket or None,
                    "lanes": [
                        {
                            "name": lane["name"],
                            "allowed_types": lane["allowed_types"],
                            "limit": lane["count"],
                            "active": lane_counts.get(str(lane["name"]), 0),
                        }
                        for lane in lanes
                    ],
                },
            },
        }

    filtered: list[dict[str, Any]] = []
    blocked_candidates: list[dict[str, Any]] = []
    global_at_capacity = bool(global_limit is not None and active_count >= int(global_limit))

    for candidate in candidates:
        ticket_type = str(candidate.get("type", "feature"))
        lane = _match_lane(ticket_type, lanes) if scheduler_enabled else None
        lane_name = str(lane["name"]) if lane else None
        lane_limit = int(lane["count"]) if lane else None
        lane_active = lane_counts.get(lane_name, 0) if lane_name else 0

        blocked_reason: str | None = None
        if scheduler_enabled and global_at_capacity:
            blocked_reason = "global_limit"
        elif scheduler_enabled and lane_name and lane_limit is not None and lane_active >= lane_limit:
            blocked_reason = "lane_full"

        if blocked_reason:
            blocked_candidates.append(
                {
                    "ticket_id": str(candidate.get("id", "")),
                    "type": ticket_type,
                    "reason": blocked_reason,
                    "lane": lane_name,
                    "lane_active": lane_active if lane_name else None,
                    "lane_limit": lane_limit,
                }
            )
            continue

        candidate["_lane"] = lane_name
        filtered.append(candidate)

    if not filtered:
        reason = "No dispatchable todo ticket found"
        if global_at_capacity:
            reason = "Global concurrency limit reached"
        elif any(str(item.get("reason")) == "lane_full" for item in blocked_candidates):
            reason = "All dispatchable tickets are blocked by lane capacity"

        return {
            "ticket": None,
            "reasoning": {
                "candidate_count": 0,
                "dispatchable_before_scheduler": len(candidates),
                "reason": reason,
                "blocked_candidates": blocked_candidates[:20],
                "scheduler": {
                    "enabled": scheduler_enabled,
                    "global_concurrency_limit": global_limit,
                    "active_tickets": active_count,
                    "active_lock_ticket": active_lock_ticket or None,
                    "lanes": [
                        {
                            "name": lane["name"],
                            "allowed_types": lane["allowed_types"],
                            "limit": lane["count"],
                            "active": lane_counts.get(str(lane["name"]), 0),
                        }
                        for lane in lanes
                    ],
                },
            },
        }

    filtered.sort(
        key=lambda t: (
            -float(t["_score"]),
            -int(t.get("priority", 3)),
            -int(t["_unblocks"]),
            -float(t["_age_hours"]),
            str(t.get("id")),
        )
    )

    chosen = filtered[0]
    reasoning = {
        "candidate_count": len(filtered),
        "dispatchable_before_scheduler": len(candidates),
        "formula": "score = priority*100 + blockers_count*30 + age_hours*0.1",
        "score": chosen["_score"],
        "priority": chosen.get("priority", 3),
        "unblocks_count": chosen["_unblocks"],
        "age_hours": round(float(chosen["_age_hours"]), 2),
        "tie_breakers": ["priority", "unblocks_count", "oldest"],
        "selected_lane": chosen.get("_lane"),
        "scheduler": {
            "enabled": scheduler_enabled,
            "global_concurrency_limit": global_limit,
            "active_tickets": active_count,
            "active_lock_ticket": active_lock_ticket or None,
            "lanes": [
                {
                    "name": lane["name"],
                    "allowed_types": lane["allowed_types"],
                    "limit": lane["count"],
                    "active": lane_counts.get(str(lane["name"]), 0),
                }
                for lane in lanes
            ],
        },
    }

    chosen.pop("_score", None)
    chosen.pop("_age_hours", None)
    chosen.pop("_unblocks", None)
    chosen.pop("_lane", None)
    return {"ticket": chosen, "reasoning": reasoning}
