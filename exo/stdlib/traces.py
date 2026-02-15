"""Structured trace export: OTel-compatible JSONL from governance events.

Converts session index entries into OpenTelemetry-compatible spans.
No OTel dependency required — generates compliant JSON directly.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

SESSION_INDEX_PATH = ".exo/memory/sessions/index.jsonl"
TRACES_OUTPUT_PATH = ".exo/logs/traces.jsonl"

# OTel attribute keys mapped from session index fields
_ATTR_MAP: list[tuple[str, str]] = [
    ("exo.session_id", "session_id"),
    ("exo.ticket_id", "ticket_id"),
    ("exo.actor", "actor"),
    ("exo.vendor", "vendor"),
    ("exo.model", "model"),
    ("exo.mode", "mode"),
    ("exo.verify", "verify"),
    ("exo.set_status", "set_status"),
    ("exo.ticket_status", "ticket_status"),
    ("exo.drift_score", "drift_score"),
    ("exo.trace_passed", "trace_passed"),
    ("exo.artifact_count", "artifact_count"),
    ("exo.error_count", "error_count"),
    ("exo.git_branch", "git_branch"),
]


def _trace_id(session_id: str) -> str:
    """Deterministic 32-char hex trace ID from session ID."""
    return hashlib.sha256(session_id.encode()).hexdigest()[:32]


def _span_id(session_id: str, suffix: str = "") -> str:
    """Deterministic 16-char hex span ID."""
    raw = f"{session_id}:{suffix}" if suffix else session_id
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _iso_to_unix_nano(iso_str: str) -> int:
    """Convert ISO 8601 timestamp to Unix nanoseconds."""
    if not iso_str:
        return 0
    dt = datetime.fromisoformat(iso_str)
    return int(dt.timestamp() * 1_000_000_000)


def _session_to_span(entry: dict[str, Any]) -> dict[str, Any]:
    """Convert a session index entry into an OTel-compatible span."""
    session_id = entry.get("session_id", "")
    started = entry.get("started_at", "")
    finished = entry.get("finished_at", "")

    # Status from verify result
    verify = entry.get("verify", "")
    if verify == "passed":
        status_code = "OK"
    elif verify == "failed":
        status_code = "ERROR"
    else:
        status_code = "UNSET"

    # Attributes — exo.* namespace
    attributes: dict[str, Any] = {}
    for attr_name, key in _ATTR_MAP:
        val = entry.get(key)
        if val is not None:
            attributes[attr_name] = val

    # Events for sub-operations that occurred during the session
    events: list[dict[str, Any]] = []
    finish_nano = _iso_to_unix_nano(finished)

    drift_score = entry.get("drift_score")
    if drift_score is not None:
        events.append(
            {
                "name": "drift_check",
                "timeUnixNano": finish_nano,
                "attributes": {"exo.drift_score": drift_score},
            }
        )

    trace_passed = entry.get("trace_passed")
    if trace_passed is not None:
        events.append(
            {
                "name": "feature_trace",
                "timeUnixNano": finish_nano,
                "attributes": {
                    "exo.trace_passed": trace_passed,
                    "exo.trace_violations": entry.get("trace_violations", 0),
                },
            }
        )

    mode = entry.get("mode", "work")
    return {
        "traceId": _trace_id(session_id),
        "spanId": _span_id(session_id),
        "parentSpanId": "",
        "name": f"exo.session.{mode}",
        "kind": "INTERNAL",
        "startTimeUnixNano": _iso_to_unix_nano(started),
        "endTimeUnixNano": finish_nano,
        "status": {
            "code": status_code,
            "message": entry.get("break_glass_reason", ""),
        },
        "attributes": attributes,
        "events": events,
    }


def export_traces(
    repo: Path,
    *,
    since: str | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Export governance events as OTel-compatible JSONL traces.

    Reads session index and converts entries to OTel spans.

    Args:
        repo: Repository root.
        since: ISO timestamp — only export sessions started after this time.
        write: If True, write to traces.jsonl. If False, return spans only.

    Returns:
        Dict with export metadata and spans.
    """
    repo = Path(repo).resolve()
    index_path = repo / SESSION_INDEX_PATH

    if not index_path.exists():
        return {
            "spans": [],
            "span_count": 0,
            "since": since,
            "output_path": None,
        }

    # Parse since threshold
    since_nano = _iso_to_unix_nano(since) if since else 0

    entries: list[dict[str, Any]] = []
    for line in index_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # Convert to spans
    spans: list[dict[str, Any]] = []
    for entry in entries:
        span = _session_to_span(entry)
        if since_nano and span["startTimeUnixNano"] < since_nano:
            continue
        spans.append(span)

    # Sort by start time
    spans.sort(key=lambda s: s["startTimeUnixNano"])

    output_path = None
    if write and spans:
        out = repo / TRACES_OUTPUT_PATH
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            for span in spans:
                f.write(json.dumps(span, ensure_ascii=True) + "\n")
        output_path = str(out.relative_to(repo))

    return {
        "spans": spans,
        "span_count": len(spans),
        "since": since,
        "output_path": output_path,
    }


def format_traces_human(result: dict[str, Any]) -> str:
    """Format trace export result for human-readable CLI output."""
    lines: list[str] = []
    spans = result.get("spans", [])
    lines.append(f"Exported {len(spans)} span(s)")

    if result.get("since"):
        lines.append(f"Since: {result['since']}")
    if result.get("output_path"):
        lines.append(f"Output: {result['output_path']}")

    lines.append("")
    for span in spans:
        name = span.get("name", "?")
        status = span.get("status", {}).get("code", "?")
        attrs = span.get("attributes", {})
        session_id = attrs.get("exo.session_id", "?")
        actor = attrs.get("exo.actor", "?")
        lines.append(f"  {session_id} [{name}] status={status} actor={actor}")

        for event in span.get("events", []):
            ename = event.get("name", "?")
            eattrs = event.get("attributes", {})
            detail_parts = [f"{k.split('.')[-1]}={v}" for k, v in eattrs.items()]
            lines.append(f"    + {ename}: {', '.join(detail_parts)}")

    return "\n".join(lines)
