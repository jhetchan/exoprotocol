"""Error Reflection Layer.

Lightweight, single-step learning persistence for agent sessions. Agents call
``reflect()`` to record operational patterns and insights. Future session
bootstraps inject active reflections as "Operational Learnings" so agents
benefit from accumulated knowledge without the 4-step evolution pipeline.

Storage: ``.exo/memory/reflections/REF-NNN.yaml`` — one file per reflection.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from exo.kernel.errors import ExoError
from exo.kernel.utils import dump_yaml, ensure_dir, gen_timestamp_id, load_yaml, now_iso

REFLECTIONS_DIR = Path(".exo/memory/reflections")
VALID_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
VALID_STATUSES = frozenset({"active", "superseded", "dismissed"})
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_MAX_BOOTSTRAP_REFLECTIONS = 10


@dataclass
class Reflection:
    """A single persisted operational learning."""

    id: str
    pattern: str
    insight: str
    severity: str  # low | medium | high | critical
    scope: str  # "global" or a ticket ID
    actor: str
    session_id: str
    created_at: str
    status: str  # active | superseded | dismissed
    tags: tuple[str, ...] = ()
    hit_count: int = 0


# ── ID generation ───────────────────────────────────────────────────


def _next_reflection_id(repo: Path) -> str:
    """Generate a collision-resistant reflection ID: REF-YYYYMMDD-HHMMSS-XXXX."""
    return gen_timestamp_id("REF")


# ── Core API ────────────────────────────────────────────────────────


def reflect(
    repo: Path,
    *,
    pattern: str,
    insight: str,
    severity: str = "medium",
    scope: str = "global",
    actor: str = "agent:unknown",
    session_id: str = "",
    tags: list[str] | None = None,
) -> Reflection:
    """Record an operational learning. Returns the created Reflection.

    Args:
        repo: Repository root path.
        pattern: What keeps happening (the recurring failure/error).
        insight: What was learned (the fix/workaround/understanding).
        severity: low | medium | high | critical.
        scope: "global" or a ticket ID for scoped reflections.
        actor: Who created the reflection.
        session_id: Session in which it was created.
        tags: Optional categorization tags.
    """
    repo = Path(repo).resolve()

    pattern_value = pattern.strip() if isinstance(pattern, str) else ""
    if not pattern_value:
        raise ExoError(
            code="REFLECT_PATTERN_REQUIRED",
            message="pattern is required for a reflection",
            blocked=True,
        )

    insight_value = insight.strip() if isinstance(insight, str) else ""
    if not insight_value:
        raise ExoError(
            code="REFLECT_INSIGHT_REQUIRED",
            message="insight is required for a reflection",
            blocked=True,
        )

    severity_value = severity.strip().lower() if isinstance(severity, str) else "medium"
    if severity_value not in VALID_SEVERITIES:
        raise ExoError(
            code="REFLECT_SEVERITY_INVALID",
            message=f"severity must be one of {sorted(VALID_SEVERITIES)}, got: {severity_value}",
            blocked=True,
        )

    scope_value = scope.strip() if isinstance(scope, str) and scope.strip() else "global"
    tag_tuple = tuple(str(t).strip() for t in (tags or []) if str(t).strip())

    ref_id = _next_reflection_id(repo)
    reflection = Reflection(
        id=ref_id,
        pattern=pattern_value,
        insight=insight_value,
        severity=severity_value,
        scope=scope_value,
        actor=actor,
        session_id=session_id,
        created_at=now_iso(),
        status="active",
        tags=tag_tuple,
        hit_count=0,
    )

    # Write YAML file
    ref_dir = repo / REFLECTIONS_DIR
    ensure_dir(ref_dir)
    ref_path = ref_dir / f"{ref_id}.yaml"
    dump_yaml(ref_path, _reflection_to_yaml_dict(reflection))

    # Sync to memory index (advisory)
    with contextlib.suppress(Exception):
        _sync_to_memory_index(repo, reflection)

    return reflection


def load_reflections(
    repo: Path,
    *,
    status: str | None = None,
    scope: str | None = None,
) -> list[Reflection]:
    """Load all reflections with optional filtering.

    Returns reflections sorted by creation time (newest first).
    """
    repo = Path(repo).resolve()
    ref_dir = repo / REFLECTIONS_DIR
    if not ref_dir.exists():
        return []

    reflections: list[Reflection] = []
    for path in sorted(ref_dir.glob("REF-*.yaml")):
        try:
            data = load_yaml(path)
            if not isinstance(data, dict):
                continue
            ref = _yaml_dict_to_reflection(data)
            if status and ref.status != status.strip().lower():
                continue
            if scope and ref.scope != scope.strip():
                continue
            reflections.append(ref)
        except Exception:  # noqa: BLE001
            continue

    # Sort: newest first
    reflections.sort(key=lambda r: r.created_at, reverse=True)
    return reflections


def reflections_for_bootstrap(
    repo: Path,
    ticket_id: str,
) -> list[Reflection]:
    """Return active reflections relevant to a given ticket.

    Includes global-scope and ticket-scoped reflections matching ticket_id.
    Sorted by severity (critical first), then by creation time (newest first).
    Capped at MAX_BOOTSTRAP_REFLECTIONS.
    """
    all_active = load_reflections(repo, status="active")
    relevant = [r for r in all_active if r.scope == "global" or r.scope == ticket_id]
    # Sort: severity (critical first), then newest first
    relevant.sort(key=lambda r: (_SEVERITY_ORDER.get(r.severity, 99), r.created_at))
    return relevant[:_MAX_BOOTSTRAP_REFLECTIONS]


def increment_hit_count(repo: Path, reflection_id: str) -> None:
    """Increment the hit_count for a reflection (called when injected into bootstrap)."""
    repo = Path(repo).resolve()
    ref_path = repo / REFLECTIONS_DIR / f"{reflection_id}.yaml"
    if not ref_path.exists():
        return
    data = load_yaml(ref_path)
    if not isinstance(data, dict):
        return
    data["hit_count"] = int(data.get("hit_count", 0)) + 1
    dump_yaml(ref_path, data)


def dismiss_reflection(repo: Path, reflection_id: str) -> Reflection:
    """Set a reflection's status to 'dismissed' so it stops appearing in bootstraps."""
    repo = Path(repo).resolve()
    ref_path = repo / REFLECTIONS_DIR / f"{reflection_id}.yaml"
    if not ref_path.exists():
        raise ExoError(
            code="REFLECT_NOT_FOUND",
            message=f"Reflection not found: {reflection_id}",
            blocked=True,
        )
    data = load_yaml(ref_path)
    if not isinstance(data, dict):
        raise ExoError(
            code="REFLECT_PARSE_ERROR",
            message=f"Failed to parse reflection: {reflection_id}",
            blocked=True,
        )
    data["status"] = "dismissed"
    dump_yaml(ref_path, data)
    return _yaml_dict_to_reflection(data)


# ── Memory Index Sync ───────────────────────────────────────────────


def _sync_to_memory_index(repo: Path, reflection: Reflection) -> str:
    """Append a failure_modes entry to .exo/memory/index.yaml.

    Returns the generated FM-NNN ID.
    """
    from exo.stdlib.evolution import (
        load_memory_index,
        next_memory_id,
        save_memory_index,
    )

    index = load_memory_index(repo)
    fm_id = next_memory_id(index, "FM")

    failure_mode = {
        "id": fm_id,
        "name": reflection.pattern[:120],
        "detection": {
            "signal": reflection.pattern,
            "source": f"reflection:{reflection.id}",
        },
        "mitigation": {
            "practice": reflection.insight,
        },
        "severity": reflection.severity,
        "frequency": "recurring",
    }

    if not isinstance(index.get("failure_modes"), list):
        index["failure_modes"] = []
    index["failure_modes"].append(failure_mode)
    save_memory_index(repo, index)
    return fm_id


# ── Serialization ───────────────────────────────────────────────────


def _reflection_to_yaml_dict(ref: Reflection) -> dict[str, Any]:
    """Convert Reflection to a dict suitable for YAML storage."""
    return {
        "id": ref.id,
        "pattern": ref.pattern,
        "insight": ref.insight,
        "severity": ref.severity,
        "scope": ref.scope,
        "actor": ref.actor,
        "session_id": ref.session_id,
        "created_at": ref.created_at,
        "status": ref.status,
        "tags": list(ref.tags),
        "hit_count": ref.hit_count,
    }


def _yaml_dict_to_reflection(data: dict[str, Any]) -> Reflection:
    """Convert a YAML dict back to a Reflection."""
    raw_tags = data.get("tags")
    tags = tuple(str(t) for t in raw_tags) if isinstance(raw_tags, list) else ()
    return Reflection(
        id=str(data.get("id", "")),
        pattern=str(data.get("pattern", "")),
        insight=str(data.get("insight", "")),
        severity=str(data.get("severity", "medium")),
        scope=str(data.get("scope", "global")),
        actor=str(data.get("actor", "")),
        session_id=str(data.get("session_id", "")),
        created_at=str(data.get("created_at", "")),
        status=str(data.get("status", "active")),
        tags=tags,
        hit_count=int(data.get("hit_count", 0)),
    )


def reflect_to_dict(ref: Reflection) -> dict[str, Any]:
    """Convert Reflection to a plain dict for JSON serialization."""
    return _reflection_to_yaml_dict(ref)


def reflections_to_list(refs: list[Reflection]) -> list[dict[str, Any]]:
    """Batch serialization."""
    return [reflect_to_dict(r) for r in refs]


def format_reflections_human(refs: list[Reflection]) -> str:
    """Format reflections as human-readable text."""
    if not refs:
        return "Reflections: (none)"
    lines = [f"Reflections: {len(refs)} total"]
    for ref in refs:
        status_tag = f" [{ref.status}]" if ref.status != "active" else ""
        scope_tag = f" (scope: {ref.scope})" if ref.scope != "global" else " (global)"
        lines.append(f"  {ref.id} [{ref.severity.upper()}]{status_tag}{scope_tag}")
        lines.append(f"    pattern: {ref.pattern}")
        lines.append(f"    insight: {ref.insight}")
        if ref.tags:
            lines.append(f"    tags: {', '.join(ref.tags)}")
        if ref.hit_count > 0:
            lines.append(f"    seen by {ref.hit_count} session(s)")
    return "\n".join(lines)


def format_bootstrap_reflections(refs: list[Reflection]) -> list[str]:
    """Format reflections for bootstrap prompt injection. Returns lines."""
    if not refs:
        return []
    severity_markers = {"critical": "!!!", "high": "!!", "medium": "!", "low": ""}
    lines = [
        "## Operational Learnings",
        "The following patterns have been learned from prior sessions. Heed these to avoid repeating known mistakes.",
        "",
    ]
    for ref in refs:
        marker = severity_markers.get(ref.severity, "")
        lines.append(f"- [{ref.severity.upper()}]{marker} {ref.pattern}")
        lines.append(f"  -> {ref.insight}")
        lines.append(f"  (ref: {ref.id}, scope: {ref.scope})")
        lines.append("")

    total_active = len(refs)
    if total_active >= _MAX_BOOTSTRAP_REFLECTIONS:
        lines.append(f"(Showing top {_MAX_BOOTSTRAP_REFLECTIONS}. Run `exo reflections` for the full list.)")
        lines.append("")

    return lines


# ── LEARNINGS.md Generation ────────────────────────────────────────

LEARNINGS_PATH = Path(".exo/LEARNINGS.md")


def generate_learnings(repo: Path) -> str:
    """Generate vendor-neutral LEARNINGS.md content from reflections + memory index.

    This file is the canonical, portable record of accumulated operational
    knowledge.  Any agent, any vendor, any IDE can read it.
    """
    repo = Path(repo).resolve()
    lines: list[str] = [
        "# Operational Learnings",
        "",
        "Auto-generated by ExoProtocol from governed session history.",
        "Do not edit manually — regenerate with `exo adapter-generate` or `exo upgrade`.",
        "",
    ]

    # Section 1: Active reflections
    active = load_reflections(repo, status="active")
    severity_markers = {"critical": "!!!", "high": "!!", "medium": "!", "low": ""}

    if active:
        lines.append("## Reflections")
        lines.append("")
        lines.append("Patterns learned from prior sessions. Heed these to avoid repeating known mistakes.")
        lines.append("")
        for ref in active:
            marker = severity_markers.get(ref.severity, "")
            scope_tag = f" (scope: {ref.scope})" if ref.scope != "global" else ""
            lines.append(f"- **[{ref.severity.upper()}]{marker}** {ref.pattern}{scope_tag}")
            lines.append(f"  - {ref.insight}")
            if ref.tags:
                lines.append(f"  - tags: {', '.join(ref.tags)}")
            lines.append("")
    else:
        lines.append("## Reflections")
        lines.append("")
        lines.append("No active reflections yet. Use `exo reflect` to record operational learnings.")
        lines.append("")

    # Section 2: Failure modes from memory index
    try:
        from exo.stdlib.evolution import load_memory_index

        index = load_memory_index(repo)
        failure_modes = index.get("failure_modes", [])
        if failure_modes:
            lines.append("## Known Failure Modes")
            lines.append("")
            for fm in failure_modes:
                name = fm.get("name", fm.get("id", "unknown"))
                severity = fm.get("severity", "medium")
                detection = fm.get("detection", {})
                mitigation = fm.get("mitigation", {})
                signal = detection.get("signal", "")
                practice = mitigation.get("practice", "")
                lines.append(f"- **{name}** [{severity}]")
                if signal:
                    lines.append(f"  - detect: {signal}")
                if practice:
                    lines.append(f"  - mitigate: {practice}")
                lines.append("")
    except Exception:  # noqa: BLE001
        pass

    # Section 3: Summary stats
    total_active = len(active)
    dismissed = load_reflections(repo, status="dismissed")
    total_dismissed = len(dismissed)
    lines.append("---")
    lines.append("")
    lines.append(f"*{total_active} active reflection(s), {total_dismissed} dismissed.*")
    lines.append("")

    return "\n".join(lines)


def write_learnings(repo: Path) -> str:
    """Generate and write .exo/LEARNINGS.md. Returns the file path."""
    repo = Path(repo).resolve()
    content = generate_learnings(repo)
    learnings_path = repo / LEARNINGS_PATH
    learnings_path.parent.mkdir(parents=True, exist_ok=True)
    learnings_path.write_text(content, encoding="utf-8")
    return str(LEARNINGS_PATH)
