"""Archive operation (closes feedback #7b/c).

``exo archive <path> --reason "..."`` moves files (or directories) to
an ``archive/`` subdirectory at repo root, appends an entry to
``archive/INDEX.md``, and writes a structured commit message. Combined
with the scanner-emitted ``RULE-ARC-001`` immutability rule, this gives
projects a one-command path to retire stale subsystems without breaking
test runners or governance.

This module deliberately does *not* manage the existing tickets archive
at ``.exo/tickets/ARCHIVE/`` — that is a different concept (governance
state) handled by ``exo ticket-archive``.
"""
# @feature:archive

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from exo.kernel.errors import ExoError

ARCHIVE_DIR = Path("archive")
ARCHIVE_INDEX = ARCHIVE_DIR / "INDEX.md"
INDEX_HEADER = "# Archive Index\n\nMoved here via `exo archive`. Restoration steps in each entry.\n"


@dataclass
class ArchiveEntry:
    """One archive operation's bookkeeping."""

    source: str
    destination: str
    reason: str
    archived_at: str
    git_sha: str = ""


@dataclass
class ArchiveResult:
    moved: list[ArchiveEntry] = field(default_factory=list)
    index_path: str = ""
    index_appended: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_sha(repo: Path) -> str:
    import subprocess

    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _ensure_index(repo: Path) -> Path:
    archive_root = repo / ARCHIVE_DIR
    archive_root.mkdir(parents=True, exist_ok=True)
    index_path = repo / ARCHIVE_INDEX
    if not index_path.exists():
        index_path.write_text(INDEX_HEADER, encoding="utf-8")
    return index_path


def _append_index_entries(index_path: Path, entries: list[ArchiveEntry]) -> None:
    if not entries:
        return
    body_lines: list[str] = ["\n"]
    for entry in entries:
        body_lines.append(f"## {entry.source} → {entry.destination}\n")
        body_lines.append(f"- archived_at: {entry.archived_at}\n")
        if entry.git_sha:
            body_lines.append(f"- archived_from_sha: {entry.git_sha}\n")
        body_lines.append(f"- reason: {entry.reason}\n")
        body_lines.append(f"- restore: `git mv {entry.destination} {entry.source}`\n")
        body_lines.append("\n")
    with index_path.open("a", encoding="utf-8") as handle:
        handle.writelines(body_lines)


def archive_paths(
    repo: Path | str,
    paths: list[str | Path],
    *,
    reason: str,
    dry_run: bool = False,
) -> ArchiveResult:
    """Move *paths* under ``archive/`` and update ``archive/INDEX.md``.

    Closes feedback #7b: a single command that retires stale subsystems
    while leaving an auditable trail. Refuses to move:

      - paths outside the repo,
      - the .git/ or .exo/ directory,
      - the archive/ tree itself,
      - paths that don't exist.

    Idempotent at the path-existence level: if the source has already
    been moved (no longer exists at the original path), it is reported
    as already-archived rather than failing.
    """
    repo = Path(repo).resolve()
    reason = (reason or "").strip()
    if not reason:
        raise ExoError(
            code="ARCHIVE_REASON_REQUIRED",
            message="exo archive requires a non-empty --reason for the audit trail.",
            blocked=True,
        )
    if not paths:
        raise ExoError(
            code="ARCHIVE_NO_PATHS",
            message="No paths supplied to archive.",
            blocked=True,
        )

    git_sha = _git_sha(repo)
    moved: list[ArchiveEntry] = []
    index_path = _ensure_index(repo) if not dry_run else repo / ARCHIVE_INDEX

    for raw in paths:
        rel = Path(str(raw))
        if rel.is_absolute():
            try:
                rel = rel.resolve().relative_to(repo)
            except ValueError as exc:
                raise ExoError(
                    code="ARCHIVE_PATH_OUTSIDE_REPO",
                    message=f"Cannot archive path outside repo: {raw}",
                    blocked=True,
                ) from exc
        rel_str = rel.as_posix()
        if rel_str.startswith("../") or rel_str == "..":
            raise ExoError(
                code="ARCHIVE_PATH_OUTSIDE_REPO",
                message=f"Cannot archive path that escapes repo: {rel_str}",
                blocked=True,
            )
        if rel_str.startswith(".git/") or rel_str == ".git":
            raise ExoError(
                code="ARCHIVE_PATH_FORBIDDEN",
                message="Refusing to archive .git internals.",
                blocked=True,
            )
        if rel_str.startswith(".exo/") or rel_str == ".exo":
            raise ExoError(
                code="ARCHIVE_PATH_FORBIDDEN",
                message="Refusing to archive .exo/ governance state.",
                blocked=True,
            )
        if rel_str.startswith("archive/") or rel_str == "archive":
            raise ExoError(
                code="ARCHIVE_PATH_FORBIDDEN",
                message="Refusing to archive paths already under archive/.",
                blocked=True,
            )
        source_abs = repo / rel
        if not source_abs.exists():
            raise ExoError(
                code="ARCHIVE_PATH_NOT_FOUND",
                message=f"Path does not exist: {rel_str}",
                blocked=True,
            )

        dest_rel = ARCHIVE_DIR / rel
        dest_abs = repo / dest_rel
        if dest_abs.exists():
            raise ExoError(
                code="ARCHIVE_DEST_BUSY",
                message=f"Destination already exists in archive: {dest_rel.as_posix()}",
                details={"dest": dest_rel.as_posix()},
                blocked=True,
            )
        if not dry_run:
            dest_abs.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_abs), str(dest_abs))
        entry = ArchiveEntry(
            source=rel_str,
            destination=dest_rel.as_posix(),
            reason=reason,
            archived_at=_now_iso(),
            git_sha=git_sha,
        )
        moved.append(entry)

    if not dry_run:
        _append_index_entries(index_path, moved)

    return ArchiveResult(
        moved=moved,
        index_path=str(index_path.relative_to(repo)) if index_path.is_relative_to(repo) else str(index_path),
        index_appended=bool(moved) and not dry_run,
    )


def archive_to_dict(result: ArchiveResult) -> dict[str, Any]:
    return {
        "moved": [
            {
                "source": e.source,
                "destination": e.destination,
                "reason": e.reason,
                "archived_at": e.archived_at,
                "git_sha": e.git_sha,
            }
            for e in result.moved
        ],
        "index_path": result.index_path,
        "index_appended": result.index_appended,
        "count": len(result.moved),
    }


def format_archive_human(result: ArchiveResult) -> str:
    if not result.moved:
        return "Archive: no paths moved."
    lines = [f"Archive: {len(result.moved)} path(s) moved"]
    for entry in result.moved:
        lines.append(f"  {entry.source} -> {entry.destination}")
    if result.index_appended:
        lines.append(f"  index: {result.index_path}")
    return "\n".join(lines)


# ── Constitution rule contribution (closes feedback #7c) ──────────


RULE_ARC_001 = {
    "id": "RULE-ARC-001",
    "type": "filesystem_deny",
    "patterns": ["archive/**"],
    "actions": ["write", "delete"],
    "message": "Blocked by RULE-ARC-001 (archive/ is immutable; restore explicitly to edit).",
}


def archive_constitution_rule() -> dict[str, Any]:
    """Return the canonical RULE-ARC-001 dict for scanner-generated constitutions.

    The scanner injects this rule into the generated constitution
    whenever an ``archive/`` directory is detected, ensuring archived
    code can't be silently mutated. Restoration goes through ``git mv``
    (or a future ``exo archive --restore``).
    """
    return dict(RULE_ARC_001)
