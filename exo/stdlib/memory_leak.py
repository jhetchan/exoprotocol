"""Private memory leak detection for session-finish.

Detects when an agent writes to private memory files (e.g. ~/.claude/MEMORY.md)
during a governed session without also creating an ExoProtocol reflection.
Advisory only — never blocks session-finish.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from exo.stdlib.reflect import load_reflections

CONFIG_PATH = ".exo/config.yaml"


@dataclass
class MemoryLeakWarning:
    """A single private-memory-written-without-reflection advisory."""

    path: str
    mtime: str
    session_started: str
    has_reflection: bool
    message: str


def _load_private_memory_config(repo: Path, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load private_memory config section."""
    if config is not None:
        return config.get("private_memory", {})
    cfg_path = repo / CONFIG_PATH
    if cfg_path.exists():
        import yaml

        with contextlib.suppress(Exception):
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data.get("private_memory", {})
    return {}


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO timestamp string to datetime. Returns None on failure."""
    with contextlib.suppress(Exception):
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def detect_memory_leaks(
    repo: Path,
    session_id: str,
    started_at: str,
    *,
    config: dict[str, Any] | None = None,
) -> list[MemoryLeakWarning]:
    """Detect private memory files modified during a session without a reflection.

    Args:
        repo: Repository root.
        session_id: Current session ID.
        started_at: ISO timestamp of when the session started.
        config: Optional full config dict; if None, loads from .exo/config.yaml.

    Returns:
        List of warnings for files written without corresponding reflections.
    """
    pm_config = _load_private_memory_config(repo, config)

    if not pm_config.get("enabled", True):
        return []

    watch_paths: list[str] = pm_config.get("watch_paths", [])
    if not watch_paths:
        return []

    start_dt = _parse_iso(started_at)
    if start_dt is None:
        return []

    # Check which watched paths were modified after session start
    modified_paths: list[tuple[str, str]] = []  # (original_path, mtime_iso)
    for raw_path in watch_paths:
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        expanded = Path(raw_path.strip()).expanduser()
        if not expanded.exists():
            continue
        mtime = datetime.fromtimestamp(expanded.stat().st_mtime, tz=timezone.utc)
        if mtime > start_dt:
            modified_paths.append((raw_path.strip(), mtime.isoformat()))

    if not modified_paths:
        return []

    # Check if any reflections were created during this session
    all_refs = load_reflections(repo)
    session_refs = [r for r in all_refs if r.session_id == session_id]
    has_reflection = len(session_refs) > 0

    if has_reflection:
        return []

    # Emit warnings for each modified file
    warnings: list[MemoryLeakWarning] = []
    for raw_path, mtime_iso in modified_paths:
        warnings.append(
            MemoryLeakWarning(
                path=raw_path,
                mtime=mtime_iso,
                session_started=started_at,
                has_reflection=False,
                message=f"Private memory written ({raw_path}) without ExoProtocol reflection — use `exo reflect` to share learnings",
            )
        )

    return warnings


def format_memory_leak_warnings(warnings: list[MemoryLeakWarning]) -> str:
    """Format warnings as a markdown section for mementos.

    Returns empty string if no warnings.
    """
    if not warnings:
        return ""

    lines = ["## Private Memory Warnings"]
    for w in warnings:
        lines.append(f"- {w.message}")

    return "\n".join(lines)


def warnings_to_dicts(warnings: list[MemoryLeakWarning]) -> list[dict[str, Any]]:
    """Serialize warnings to dicts for return payloads."""
    return [
        {
            "path": w.path,
            "mtime": w.mtime,
            "session_started": w.session_started,
            "has_reflection": w.has_reflection,
            "message": w.message,
        }
        for w in warnings
    ]
