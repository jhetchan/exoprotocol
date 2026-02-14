"""Background garbage collection for old mementos, cursors, and bootstraps.

Scans `.exo/memory/sessions/` for old memento files, `.exo/cache/orchestrator/`
for orphaned cursors, and `.exo/cache/sessions/` for leftover bootstrap files.
Also compacts the session index JSONL by removing entries for GC'd sessions.

This is designed to run periodically or on-demand (`exo gc`) to reclaim disk
space and keep the governance data directory clean.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from exo.kernel.utils import now_iso

SESSION_MEMORY_DIR = Path(".exo/memory/sessions")
SESSION_INDEX_PATH = SESSION_MEMORY_DIR / "index.jsonl"
SESSION_CACHE_DIR = Path(".exo/cache/sessions")
CURSOR_CACHE_DIR = Path(".exo/cache/orchestrator")


@dataclass
class GCReport:
    """Result of a garbage collection run."""

    mementos_scanned: int = 0
    mementos_removed: int = 0
    mementos_removed_paths: list[str] = field(default_factory=list)
    cursors_scanned: int = 0
    cursors_removed: int = 0
    cursors_removed_paths: list[str] = field(default_factory=list)
    bootstraps_scanned: int = 0
    bootstraps_removed: int = 0
    bootstraps_removed_paths: list[str] = field(default_factory=list)
    index_entries_before: int = 0
    index_entries_after: int = 0
    index_entries_pruned: int = 0
    empty_dirs_removed: int = 0
    dry_run: bool = False
    max_age_days: float = 30.0
    gc_at: str = ""


def _file_age_days(path: Path) -> float:
    """Get file age in days based on modification time."""
    try:
        mtime = path.stat().st_mtime
        age_seconds = max(datetime.now().timestamp() - mtime, 0.0)
        return age_seconds / 86400.0
    except OSError:
        return 0.0


def _gc_mementos(repo: Path, max_age_days: float, dry_run: bool) -> tuple[int, int, list[str]]:
    """Scan and remove old memento files."""
    memory_dir = repo / SESSION_MEMORY_DIR
    if not memory_dir.exists():
        return 0, 0, []

    scanned = 0
    removed = 0
    removed_paths: list[str] = []

    for ticket_dir in sorted(memory_dir.iterdir()):
        if not ticket_dir.is_dir():
            continue
        for memento in sorted(ticket_dir.glob("*.md")):
            scanned += 1
            age = _file_age_days(memento)
            if age >= max_age_days:
                rel = str(memento.relative_to(repo))
                removed_paths.append(rel)
                removed += 1
                if not dry_run:
                    memento.unlink(missing_ok=True)

    return scanned, removed, removed_paths


def _gc_cursors(repo: Path, max_age_days: float, dry_run: bool) -> tuple[int, int, list[str]]:
    """Scan and remove old cursor files."""
    cursor_dir = repo / CURSOR_CACHE_DIR
    if not cursor_dir.exists():
        return 0, 0, []

    scanned = 0
    removed = 0
    removed_paths: list[str] = []

    for cursor_file in sorted(cursor_dir.glob("*.cursor")):
        scanned += 1
        age = _file_age_days(cursor_file)
        if age >= max_age_days:
            rel = str(cursor_file.relative_to(repo))
            removed_paths.append(rel)
            removed += 1
            if not dry_run:
                cursor_file.unlink(missing_ok=True)

    return scanned, removed, removed_paths


def _gc_bootstraps(repo: Path, max_age_days: float, dry_run: bool) -> tuple[int, int, list[str]]:
    """Scan and remove old bootstrap prompt files (no active session)."""
    cache_dir = repo / SESSION_CACHE_DIR
    if not cache_dir.exists():
        return 0, 0, []

    # Build set of actors with active sessions
    active_actors: set[str] = set()
    for active_file in cache_dir.glob("*.active.json"):
        # actor token is the stem before ".active"
        actor_token = active_file.stem.replace(".active", "")
        active_actors.add(actor_token)

    scanned = 0
    removed = 0
    removed_paths: list[str] = []

    for bootstrap in sorted(cache_dir.glob("*.bootstrap.md")):
        scanned += 1
        actor_token = bootstrap.stem.replace(".bootstrap", "")
        # Only GC bootstraps with no corresponding active session
        if actor_token in active_actors:
            continue
        age = _file_age_days(bootstrap)
        if age >= max_age_days:
            rel = str(bootstrap.relative_to(repo))
            removed_paths.append(rel)
            removed += 1
            if not dry_run:
                bootstrap.unlink(missing_ok=True)

    return scanned, removed, removed_paths


def _compact_index(
    repo: Path,
    removed_session_ids: set[str],
    dry_run: bool,
) -> tuple[int, int, int]:
    """Compact session index.jsonl by removing entries for GC'd sessions."""
    index_path = repo / SESSION_INDEX_PATH
    if not index_path.exists():
        return 0, 0, 0

    lines: list[str] = []
    kept: list[str] = []
    try:
        with index_path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return 0, 0, 0

    before = len(lines)
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            kept.append(raw)
            continue
        if not isinstance(entry, dict):
            kept.append(raw)
            continue
        session_id = str(entry.get("session_id", "")).strip()
        if session_id and session_id in removed_session_ids:
            continue  # prune this entry
        kept.append(raw)

    after = len(kept)
    pruned = before - after

    if pruned > 0 and not dry_run:
        with index_path.open("w", encoding="utf-8") as f:
            for entry_line in kept:
                f.write(entry_line + "\n")

    return before, after, pruned


def _cleanup_empty_dirs(repo: Path, dry_run: bool) -> int:
    """Remove empty ticket directories under session memory."""
    memory_dir = repo / SESSION_MEMORY_DIR
    if not memory_dir.exists():
        return 0

    removed = 0
    for ticket_dir in sorted(memory_dir.iterdir()):
        if not ticket_dir.is_dir():
            continue
        # Don't remove the memory dir itself
        if ticket_dir == memory_dir:
            continue
        try:
            # Check if directory is empty (no files or subdirs)
            contents = list(ticket_dir.iterdir())
            if not contents:
                removed += 1
                if not dry_run:
                    ticket_dir.rmdir()
        except OSError:
            continue

    return removed


def _extract_session_ids_from_paths(paths: list[str]) -> set[str]:
    """Extract session IDs from memento file paths (SES-*.md)."""
    ids: set[str] = set()
    for path in paths:
        basename = Path(path).stem  # e.g., "SES-20250101120000-ABCD1234"
        if basename.startswith("SES-"):
            ids.add(basename)
    return ids


def gc(
    repo: Path,
    *,
    max_age_days: float = 30.0,
    dry_run: bool = False,
) -> GCReport:
    """Run garbage collection across mementos, cursors, and bootstraps.

    Args:
        repo: Repository root path.
        max_age_days: Age threshold in days for removing files.
        dry_run: Preview what would be removed without deleting.

    Returns:
        GCReport with details of what was (or would be) removed.
    """
    repo = Path(repo).resolve()
    report = GCReport(
        dry_run=dry_run,
        max_age_days=max_age_days,
        gc_at=now_iso(),
    )

    # 1. GC old mementos
    m_scanned, m_removed, m_paths = _gc_mementos(repo, max_age_days, dry_run)
    report.mementos_scanned = m_scanned
    report.mementos_removed = m_removed
    report.mementos_removed_paths = m_paths

    # 2. GC old cursors
    c_scanned, c_removed, c_paths = _gc_cursors(repo, max_age_days, dry_run)
    report.cursors_scanned = c_scanned
    report.cursors_removed = c_removed
    report.cursors_removed_paths = c_paths

    # 3. GC orphaned bootstraps
    b_scanned, b_removed, b_paths = _gc_bootstraps(repo, max_age_days, dry_run)
    report.bootstraps_scanned = b_scanned
    report.bootstraps_removed = b_removed
    report.bootstraps_removed_paths = b_paths

    # 4. Compact session index
    removed_session_ids = _extract_session_ids_from_paths(m_paths)
    i_before, i_after, i_pruned = _compact_index(repo, removed_session_ids, dry_run)
    report.index_entries_before = i_before
    report.index_entries_after = i_after
    report.index_entries_pruned = i_pruned

    # 5. Clean up empty ticket directories
    report.empty_dirs_removed = _cleanup_empty_dirs(repo, dry_run)

    return report


def gc_to_dict(report: GCReport) -> dict[str, Any]:
    """Convert GCReport to a plain dict for serialization."""
    return {
        "mementos_scanned": report.mementos_scanned,
        "mementos_removed": report.mementos_removed,
        "mementos_removed_paths": report.mementos_removed_paths,
        "cursors_scanned": report.cursors_scanned,
        "cursors_removed": report.cursors_removed,
        "cursors_removed_paths": report.cursors_removed_paths,
        "bootstraps_scanned": report.bootstraps_scanned,
        "bootstraps_removed": report.bootstraps_removed,
        "bootstraps_removed_paths": report.bootstraps_removed_paths,
        "index_entries_before": report.index_entries_before,
        "index_entries_after": report.index_entries_after,
        "index_entries_pruned": report.index_entries_pruned,
        "empty_dirs_removed": report.empty_dirs_removed,
        "dry_run": report.dry_run,
        "max_age_days": report.max_age_days,
        "gc_at": report.gc_at,
        "total_removed": report.mementos_removed + report.cursors_removed + report.bootstraps_removed,
    }


def format_gc_human(report: GCReport) -> str:
    """Format GC report as human-readable text."""
    mode = "DRY RUN" if report.dry_run else "COMPLETE"
    total = report.mementos_removed + report.cursors_removed + report.bootstraps_removed
    lines = [
        f"Garbage Collection: {mode}",
        f"  max age: {report.max_age_days} days",
        f"  mementos: {report.mementos_removed}/{report.mementos_scanned} removed",
        f"  cursors: {report.cursors_removed}/{report.cursors_scanned} removed",
        f"  bootstraps: {report.bootstraps_removed}/{report.bootstraps_scanned} removed",
    ]
    if report.index_entries_pruned > 0:
        lines.append(
            f"  index: {report.index_entries_pruned} entries pruned ({report.index_entries_before} -> {report.index_entries_after})"
        )
    if report.empty_dirs_removed > 0:
        lines.append(f"  empty dirs: {report.empty_dirs_removed} removed")
    lines.append(f"  total files removed: {total}")
    return "\n".join(lines)
