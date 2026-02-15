"""Chain reaction follow-up ticket engine.

Inspects session-finish results (feature trace, requirement trace, drift,
tool usage) and creates follow-up tickets for governance gaps.

This is the chain reaction: session finishes → gaps detected → tickets created
→ next session picks them up → self-sustaining governance loop.

Deterministic (no LLM). Called from session-finish and CLI/MCP.
"""
# @feature:follow-up-chain

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from exo.kernel.utils import now_iso

CONFIG_PATH = Path(".exo/config.yaml")


@dataclass(frozen=True)
class FollowUpTicket:
    """A follow-up ticket to be created from a governance gap."""

    title: str
    kind: str  # "task" or "epic"
    rationale: str  # why this follow-up exists
    labels: tuple[str, ...]  # e.g. ("governance", "traceability")
    source: str  # which check triggered it (e.g. "feature_trace")
    severity: str  # "high" | "medium" | "low"


@dataclass(frozen=True)
class FollowUpReport:
    """Result of detecting and optionally creating follow-up tickets."""

    detected: tuple[FollowUpTicket, ...]
    created_ids: tuple[str, ...]  # ticket IDs that were actually created
    skipped: int  # duplicates or over-budget


# ---------------------------------------------------------------------------
# Detection rules
# ---------------------------------------------------------------------------

_DRIFT_THRESHOLD = 0.7


def detect_follow_ups(
    repo: Path,
    *,
    ticket_id: str = "",
    trace_report: Any | None = None,
    req_trace_report: Any | None = None,
    drift_data: dict[str, Any] | None = None,
    tools_summary: dict[str, Any] | None = None,
) -> list[FollowUpTicket]:
    """Detect governance gaps that warrant follow-up tickets.

    All inputs are optional — skips checks for which data is not provided.
    """
    follow_ups: list[FollowUpTicket] = []

    # --- Feature trace gaps ---
    if trace_report is not None:
        violations = getattr(trace_report, "violations", [])
        uncovered = [v for v in violations if getattr(v, "kind", "") == "uncovered_code"]
        if uncovered:
            follow_ups.append(
                FollowUpTicket(
                    title="Add @feature: tags to uncovered source files",
                    kind="task",
                    rationale=f"{len(uncovered)} source file(s) have no feature coverage",
                    labels=("governance", "traceability"),
                    source="feature_trace",
                    severity="medium",
                )
            )

        unbound = [v for v in violations if getattr(v, "kind", "") == "unbound_feature"]
        if unbound:
            follow_ups.append(
                FollowUpTicket(
                    title="Add code for unbound features or remove from manifest",
                    kind="task",
                    rationale=f"{len(unbound)} feature(s) defined in manifest but have no code tags",
                    labels=("governance", "traceability"),
                    source="feature_trace",
                    severity="medium",
                )
            )

    # --- Requirement trace gaps ---
    if req_trace_report is not None:
        violations = getattr(req_trace_report, "violations", [])
        uncovered_reqs = [v for v in violations if getattr(v, "kind", "") == "uncovered_req"]
        if uncovered_reqs:
            follow_ups.append(
                FollowUpTicket(
                    title="Add @req: annotations for uncovered requirements",
                    kind="task",
                    rationale=f"{len(uncovered_reqs)} requirement(s) have no code annotations",
                    labels=("governance", "traceability"),
                    source="requirement_trace",
                    severity="medium",
                )
            )

    # --- Drift score ---
    if drift_data is not None:
        drift_score = drift_data.get("drift_score", 0.0)
        if isinstance(drift_score, (int, float)) and drift_score > _DRIFT_THRESHOLD:
            follow_ups.append(
                FollowUpTicket(
                    title="Reduce session drift — scope exceeded budget",
                    kind="task",
                    rationale=f"Drift score {drift_score:.2f} exceeds threshold {_DRIFT_THRESHOLD}",
                    labels=("governance", "drift"),
                    source="drift_detection",
                    severity="high",
                )
            )

    # --- Tool awareness ---
    if tools_summary is not None:
        created = tools_summary.get("tools_created", 0)
        used = tools_summary.get("tools_used", 0)
        total = tools_summary.get("total_tools", 0)
        if total > 0 and created > 0 and used == 0:
            follow_ups.append(
                FollowUpTicket(
                    title="Review and use existing tools before building new ones",
                    kind="task",
                    rationale=f"Created {created} tool(s) but used 0 of {total} existing tool(s)",
                    labels=("tools",),
                    source="tool_awareness",
                    severity="low",
                )
            )

    return follow_ups


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def create_follow_ups(
    repo: Path,
    *,
    parent_ticket_id: str,
    follow_ups: list[FollowUpTicket],
    max_per_session: int = 5,
) -> FollowUpReport:
    """Create follow-up tickets from detected governance gaps.

    - Deduplicates against existing open tickets under the same parent.
    - Respects max_per_session cap.
    - Returns a report of what was created vs skipped.
    """
    from exo.kernel.tickets import (
        allocate_ticket_id,
        load_all_tickets,
        save_ticket,
    )

    repo = Path(repo).resolve()

    # Load existing tickets for dedup
    existing = load_all_tickets(repo)
    existing_titles: set[str] = set()
    for tkt in existing:
        if tkt.get("parent_id") == parent_ticket_id and tkt.get("status") in ("todo", "in_progress"):
            existing_titles.add(str(tkt.get("title", "")).strip())

    created_ids: list[str] = []
    skipped = 0

    for fu in follow_ups:
        if len(created_ids) >= max_per_session:
            skipped += len(follow_ups) - len(created_ids) - skipped
            break

        # Dedup by title
        if fu.title in existing_titles:
            skipped += 1
            continue

        tid = allocate_ticket_id(repo, kind=fu.kind)
        save_ticket(
            repo,
            {
                "id": tid,
                "title": fu.title,
                "kind": fu.kind,
                "status": "todo",
                "priority": 3 if fu.severity == "medium" else (2 if fu.severity == "high" else 4),
                "parent_id": parent_ticket_id,
                "labels": list(fu.labels),
                "notes": [f"Auto-created by chain reaction: {fu.rationale}"],
                "created_at": now_iso(),
            },
        )
        created_ids.append(tid)

    return FollowUpReport(
        detected=tuple(follow_ups),
        created_ids=tuple(created_ids),
        skipped=skipped,
    )


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def follow_up_to_dict(fu: FollowUpTicket) -> dict[str, Any]:
    """Convert a FollowUpTicket to a plain dict."""
    return {
        "title": fu.title,
        "kind": fu.kind,
        "rationale": fu.rationale,
        "labels": list(fu.labels),
        "source": fu.source,
        "severity": fu.severity,
    }


def follow_ups_to_list(follow_ups: list[FollowUpTicket]) -> list[dict[str, Any]]:
    """Convert list of FollowUpTickets to plain dicts."""
    return [follow_up_to_dict(fu) for fu in follow_ups]


def report_to_dict(report: FollowUpReport) -> dict[str, Any]:
    """Convert FollowUpReport to a plain dict for JSON."""
    return {
        "detected": follow_ups_to_list(list(report.detected)),
        "detected_count": len(report.detected),
        "created_ids": list(report.created_ids),
        "created_count": len(report.created_ids),
        "skipped": report.skipped,
    }


def format_follow_ups_human(report: FollowUpReport) -> str:
    """Format follow-up report as human-readable text."""
    if not report.detected:
        return "Follow-ups: (none — governance looks clean)"
    lines = [f"Follow-ups: {len(report.detected)} detected"]
    for fu in report.detected:
        sev_label = {"high": "HIGH", "medium": "MED", "low": "LOW"}.get(fu.severity, fu.severity.upper())
        lines.append(f"  [{sev_label}] {fu.title}")
        lines.append(f"         {fu.rationale}")
    if report.created_ids:
        lines.append(f"  created: {', '.join(report.created_ids)}")
    if report.skipped:
        lines.append(f"  skipped: {report.skipped} (duplicate or over budget)")
    return "\n".join(lines)
