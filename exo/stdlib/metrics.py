"""Governance metrics API for dashboards.

Computes aggregate metrics from session history, drift data, and governance
state. Designed for consumption by external dashboards and monitoring tools.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from exo.kernel.utils import now_iso

SESSION_INDEX_PATH = Path(".exo/memory/sessions/index.jsonl")


def _load_index(repo: Path) -> list[dict[str, Any]]:
    """Load session index entries."""
    index_path = repo / SESSION_INDEX_PATH
    if not index_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in index_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def compute_metrics(repo: Path) -> dict[str, Any]:
    """Compute governance metrics from session history and governance state.

    Returns a dashboard-ready dict with session statistics, drift distribution,
    ticket throughput, and verification rates.
    """
    repo = Path(repo).resolve()
    entries = _load_index(repo)

    total = len(entries)
    if total == 0:
        return {
            "session_count": 0,
            "verify_passed": 0,
            "verify_failed": 0,
            "verify_bypassed": 0,
            "verify_pass_rate": 0.0,
            "avg_drift_score": 0.0,
            "max_drift_score": 0.0,
            "drift_distribution": {"low": 0, "medium": 0, "high": 0},
            "tickets_touched": 0,
            "actors": [],
            "actor_count": 0,
            "mode_counts": {"work": 0, "audit": 0},
            "computed_at": now_iso(),
        }

    # Verification stats
    passed = sum(1 for e in entries if e.get("verify") == "passed")
    failed = sum(1 for e in entries if e.get("verify") == "failed")
    bypassed = sum(1 for e in entries if e.get("verify") == "bypassed")
    pass_rate = passed / total if total > 0 else 0.0

    # Drift distribution
    drift_scores = [e["drift_score"] for e in entries if e.get("drift_score") is not None]
    avg_drift = sum(drift_scores) / len(drift_scores) if drift_scores else 0.0
    max_drift = max(drift_scores) if drift_scores else 0.0
    low = sum(1 for d in drift_scores if d < 0.3)
    medium = sum(1 for d in drift_scores if 0.3 <= d < 0.7)
    high = sum(1 for d in drift_scores if d >= 0.7)

    # Ticket throughput
    ticket_ids = {e.get("ticket_id", "") for e in entries if e.get("ticket_id")}

    # Actor breakdown
    actors_set: dict[str, int] = {}
    for entry in entries:
        actor = entry.get("actor", "")
        if actor:
            actors_set[actor] = actors_set.get(actor, 0) + 1
    actors = [{"actor": a, "session_count": c} for a, c in sorted(actors_set.items())]

    # Mode counts
    work_count = sum(1 for e in entries if e.get("mode", "work") == "work")
    audit_count = sum(1 for e in entries if e.get("mode") == "audit")

    return {
        "session_count": total,
        "verify_passed": passed,
        "verify_failed": failed,
        "verify_bypassed": bypassed,
        "verify_pass_rate": round(pass_rate, 3),
        "avg_drift_score": round(avg_drift, 3),
        "max_drift_score": round(max_drift, 3),
        "drift_distribution": {"low": low, "medium": medium, "high": high},
        "tickets_touched": len(ticket_ids),
        "actors": actors,
        "actor_count": len(actors),
        "mode_counts": {"work": work_count, "audit": audit_count},
        "computed_at": now_iso(),
    }


def format_metrics_human(data: dict[str, Any]) -> str:
    """Format metrics for human-readable CLI output."""
    lines: list[str] = []
    lines.append(f"Governance Metrics: {data['session_count']} session(s)")
    lines.append("")

    lines.append("Verification:")
    lines.append(
        f"  passed: {data['verify_passed']}, failed: {data['verify_failed']}, "
        f"bypassed: {data['verify_bypassed']}"
    )
    lines.append(f"  pass rate: {data['verify_pass_rate']:.1%}")
    lines.append("")

    lines.append("Drift:")
    lines.append(f"  avg: {data['avg_drift_score']:.3f}, max: {data['max_drift_score']:.3f}")
    dd = data.get("drift_distribution", {})
    lines.append(f"  distribution: low={dd.get('low', 0)}, medium={dd.get('medium', 0)}, high={dd.get('high', 0)}")
    lines.append("")

    lines.append(f"Tickets touched: {data['tickets_touched']}")
    lines.append(f"Actors: {data['actor_count']}")
    for actor in data.get("actors", []):
        lines.append(f"  {actor['actor']}: {actor['session_count']} session(s)")

    mc = data.get("mode_counts", {})
    lines.append(f"Modes: work={mc.get('work', 0)}, audit={mc.get('audit', 0)}")

    return "\n".join(lines)
