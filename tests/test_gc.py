"""Tests for the background garbage collection module.

Covers:
- Memento GC (age-based removal of .md files)
- Cursor GC (age-based removal of .cursor files)
- Bootstrap GC (orphaned bootstrap cleanup)
- Session index compaction (JSONL pruning)
- Empty directory cleanup
- Dry-run mode
- CLI integration (exo gc)
- Human output formatting
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from exo.kernel import governance as governance_mod
from exo.stdlib.gc import (
    _cleanup_empty_dirs,
    _compact_index,
    _extract_session_ids_from_paths,
    _file_age_days,
    _gc_bootstraps,
    _gc_cursors,
    _gc_mementos,
    format_gc_human,
    gc,
    gc_to_dict,
)


def _policy_block(rule: dict[str, Any]) -> str:
    return f"\n```yaml exo-policy\n{json.dumps(rule)}\n```\n"


def _bootstrap_repo(tmp_path: Path) -> Path:
    repo = tmp_path
    exo_dir = repo / ".exo"
    exo_dir.mkdir(parents=True, exist_ok=True)
    constitution = "# Test Constitution\n\n" + _policy_block(
        {
            "id": "RULE-SEC-001",
            "type": "filesystem_deny",
            "patterns": ["**/.env*"],
            "actions": ["read", "write"],
            "message": "Secret deny",
        }
    )
    (exo_dir / "CONSTITUTION.md").write_text(constitution, encoding="utf-8")
    governance_mod.compile_constitution(repo)
    return repo


def _create_old_file(path: Path, age_days: float) -> None:
    """Create a file and set its mtime to simulate age."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("test content\n", encoding="utf-8")
    old_time = time.time() - (age_days * 86400)
    os.utime(path, (old_time, old_time))


def _create_fresh_file(path: Path) -> None:
    """Create a file with current mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("test content\n", encoding="utf-8")


def _create_memento(repo: Path, ticket_id: str, session_id: str, age_days: float = 0) -> Path:
    """Create a memento file."""
    path = repo / ".exo" / "memory" / "sessions" / ticket_id / f"{session_id}.md"
    if age_days > 0:
        _create_old_file(path, age_days)
    else:
        _create_fresh_file(path)
    return path


def _create_cursor(repo: Path, actor: str, age_days: float = 0) -> Path:
    """Create a cursor file."""
    path = repo / ".exo" / "cache" / "orchestrator" / f"{actor}.cursor"
    if age_days > 0:
        _create_old_file(path, age_days)
    else:
        _create_fresh_file(path)
    return path


def _create_bootstrap(repo: Path, actor: str, age_days: float = 0) -> Path:
    """Create a bootstrap file."""
    path = repo / ".exo" / "cache" / "sessions" / f"{actor}.bootstrap.md"
    if age_days > 0:
        _create_old_file(path, age_days)
    else:
        _create_fresh_file(path)
    return path


def _create_active_session(repo: Path, actor: str) -> Path:
    """Create an active session file."""
    path = repo / ".exo" / "cache" / "sessions" / f"{actor}.active.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"actor": actor, "status": "active"}), encoding="utf-8")
    return path


def _write_index(repo: Path, entries: list[dict[str, Any]]) -> Path:
    """Write session index JSONL file."""
    index_path = repo / ".exo" / "memory" / "sessions" / "index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return index_path


# ── File Age ────────────────────────────────────────────────────────


class TestFileAge:
    def test_fresh_file_age(self, tmp_path: Path) -> None:
        path = tmp_path / "fresh.txt"
        _create_fresh_file(path)
        age = _file_age_days(path)
        assert age < 1.0

    def test_old_file_age(self, tmp_path: Path) -> None:
        path = tmp_path / "old.txt"
        _create_old_file(path, 45.0)
        age = _file_age_days(path)
        assert 44.0 <= age <= 46.0

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.txt"
        age = _file_age_days(path)
        assert age == 0.0


# ── Memento GC ──────────────────────────────────────────────────────


class TestGCMementos:
    def test_no_mementos(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        scanned, removed, paths = _gc_mementos(repo, 30.0, False)
        assert scanned == 0
        assert removed == 0
        assert paths == []

    def test_remove_old_mementos(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_memento(repo, "TICKET-001", "SES-OLD-001", age_days=45)
        _create_memento(repo, "TICKET-001", "SES-FRESH-002", age_days=0)
        scanned, removed, paths = _gc_mementos(repo, 30.0, False)
        assert scanned == 2
        assert removed == 1
        assert len(paths) == 1
        assert "SES-OLD-001" in paths[0]

    def test_dry_run_preserves_files(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        path = _create_memento(repo, "TICKET-001", "SES-OLD-001", age_days=45)
        scanned, removed, paths = _gc_mementos(repo, 30.0, True)
        assert removed == 1
        assert path.exists()  # file not deleted in dry run

    def test_multiple_tickets(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_memento(repo, "TICKET-001", "SES-OLD-001", age_days=60)
        _create_memento(repo, "TICKET-002", "SES-OLD-002", age_days=90)
        _create_memento(repo, "TICKET-003", "SES-FRESH-003", age_days=5)
        scanned, removed, paths = _gc_mementos(repo, 30.0, False)
        assert scanned == 3
        assert removed == 2


# ── Cursor GC ───────────────────────────────────────────────────────


class TestGCCursors:
    def test_no_cursors(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        scanned, removed, paths = _gc_cursors(repo, 30.0, False)
        assert scanned == 0

    def test_remove_old_cursors(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_cursor(repo, "agent-worker", age_days=45)
        _create_cursor(repo, "agent-fresh", age_days=5)
        scanned, removed, paths = _gc_cursors(repo, 30.0, False)
        assert scanned == 2
        assert removed == 1
        assert "agent-worker" in paths[0]

    def test_dry_run_preserves_cursors(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        path = _create_cursor(repo, "agent-old", age_days=60)
        scanned, removed, paths = _gc_cursors(repo, 30.0, True)
        assert removed == 1
        assert path.exists()


# ── Bootstrap GC ────────────────────────────────────────────────────


class TestGCBootstraps:
    def test_no_bootstraps(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        scanned, removed, paths = _gc_bootstraps(repo, 30.0, False)
        assert scanned == 0

    def test_remove_orphaned_bootstraps(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_bootstrap(repo, "agent-orphan", age_days=45)
        scanned, removed, paths = _gc_bootstraps(repo, 30.0, False)
        assert scanned == 1
        assert removed == 1

    def test_keep_bootstrap_with_active_session(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_bootstrap(repo, "agent-active", age_days=45)
        _create_active_session(repo, "agent-active")
        scanned, removed, paths = _gc_bootstraps(repo, 30.0, False)
        assert scanned == 1
        assert removed == 0  # kept because active session exists

    def test_keep_fresh_orphan_bootstrap(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_bootstrap(repo, "agent-fresh-orphan", age_days=5)
        scanned, removed, paths = _gc_bootstraps(repo, 30.0, False)
        assert scanned == 1
        assert removed == 0  # kept because not old enough


# ── Index Compaction ────────────────────────────────────────────────


class TestCompactIndex:
    def test_no_index(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        before, after, pruned = _compact_index(repo, set(), False)
        assert before == 0

    def test_no_pruning_needed(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_index(
            repo,
            [
                {"session_id": "SES-001", "ticket_id": "TICKET-001"},
                {"session_id": "SES-002", "ticket_id": "TICKET-002"},
            ],
        )
        before, after, pruned = _compact_index(repo, set(), False)
        assert before == 2
        assert after == 2
        assert pruned == 0

    def test_prune_removed_sessions(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_index(
            repo,
            [
                {"session_id": "SES-001", "ticket_id": "TICKET-001"},
                {"session_id": "SES-002", "ticket_id": "TICKET-002"},
                {"session_id": "SES-003", "ticket_id": "TICKET-003"},
            ],
        )
        before, after, pruned = _compact_index(repo, {"SES-001", "SES-003"}, False)
        assert before == 3
        assert after == 1
        assert pruned == 2

    def test_dry_run_preserves_index(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        index_path = _write_index(
            repo,
            [
                {"session_id": "SES-001", "ticket_id": "TICKET-001"},
            ],
        )
        _compact_index(repo, {"SES-001"}, True)
        # Verify file still has original content
        with index_path.open("r") as f:
            assert len(f.readlines()) == 1


# ── Empty Dir Cleanup ───────────────────────────────────────────────


class TestCleanupEmptyDirs:
    def test_no_empty_dirs(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_memento(repo, "TICKET-001", "SES-001")
        removed = _cleanup_empty_dirs(repo, False)
        assert removed == 0

    def test_remove_empty_dirs(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        empty_dir = repo / ".exo" / "memory" / "sessions" / "TICKET-EMPTY"
        empty_dir.mkdir(parents=True, exist_ok=True)
        removed = _cleanup_empty_dirs(repo, False)
        assert removed == 1
        assert not empty_dir.exists()

    def test_dry_run_keeps_dirs(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        empty_dir = repo / ".exo" / "memory" / "sessions" / "TICKET-EMPTY"
        empty_dir.mkdir(parents=True, exist_ok=True)
        removed = _cleanup_empty_dirs(repo, True)
        assert removed == 1
        assert empty_dir.exists()  # not actually removed


# ── Session ID Extraction ───────────────────────────────────────────


class TestExtractSessionIds:
    def test_extract_from_paths(self) -> None:
        paths = [
            ".exo/memory/sessions/TICKET-001/SES-20250101-ABCD.md",
            ".exo/memory/sessions/TICKET-002/SES-20250202-EFGH.md",
        ]
        ids = _extract_session_ids_from_paths(paths)
        assert ids == {"SES-20250101-ABCD", "SES-20250202-EFGH"}

    def test_empty_paths(self) -> None:
        assert _extract_session_ids_from_paths([]) == set()

    def test_non_session_paths(self) -> None:
        paths = [".exo/memory/sessions/TICKET-001/notes.md"]
        ids = _extract_session_ids_from_paths(paths)
        assert ids == set()


# ── Composite GC ────────────────────────────────────────────────────


class TestGC:
    def test_empty_repo(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = gc(repo)
        assert report.mementos_removed == 0
        assert report.cursors_removed == 0
        assert report.bootstraps_removed == 0
        assert report.gc_at != ""

    def test_full_gc(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_memento(repo, "TICKET-001", "SES-OLD-001", age_days=45)
        _create_memento(repo, "TICKET-001", "SES-FRESH-002", age_days=5)
        _create_cursor(repo, "agent-old", age_days=60)
        _create_cursor(repo, "agent-fresh", age_days=3)
        _create_bootstrap(repo, "agent-orphan", age_days=40)
        _write_index(
            repo,
            [
                {"session_id": "SES-OLD-001", "ticket_id": "TICKET-001"},
                {"session_id": "SES-FRESH-002", "ticket_id": "TICKET-001"},
            ],
        )
        report = gc(repo, max_age_days=30.0)
        assert report.mementos_removed == 1
        assert report.cursors_removed == 1
        assert report.bootstraps_removed == 1
        assert report.index_entries_pruned == 1
        assert report.index_entries_after == 1

    def test_custom_age(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_memento(repo, "TICKET-001", "SES-001", age_days=10)
        report = gc(repo, max_age_days=7.0)
        assert report.mementos_removed == 1

    def test_dry_run(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        path = _create_memento(repo, "TICKET-001", "SES-OLD-001", age_days=45)
        report = gc(repo, dry_run=True)
        assert report.dry_run is True
        assert report.mementos_removed == 1
        assert path.exists()  # file preserved

    def test_max_age_default(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = gc(repo)
        assert report.max_age_days == 30.0


# ── Report Output ──────────────────────────────────────────────────


class TestGCReport:
    def test_to_dict_structure(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = gc(repo)
        d = gc_to_dict(report)
        assert "mementos_scanned" in d
        assert "mementos_removed" in d
        assert "cursors_scanned" in d
        assert "cursors_removed" in d
        assert "bootstraps_scanned" in d
        assert "bootstraps_removed" in d
        assert "index_entries_before" in d
        assert "index_entries_after" in d
        assert "index_entries_pruned" in d
        assert "empty_dirs_removed" in d
        assert "dry_run" in d
        assert "max_age_days" in d
        assert "gc_at" in d
        assert "total_removed" in d

    def test_total_removed_calculation(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_memento(repo, "TICKET-001", "SES-OLD-001", age_days=45)
        _create_cursor(repo, "agent-old", age_days=60)
        report = gc(repo)
        d = gc_to_dict(report)
        assert d["total_removed"] == 2

    def test_human_format_complete(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_memento(repo, "TICKET-001", "SES-OLD-001", age_days=45)
        report = gc(repo)
        text = format_gc_human(report)
        assert "Garbage Collection: COMPLETE" in text
        assert "mementos: 1/1 removed" in text

    def test_human_format_dry_run(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = gc(repo, dry_run=True)
        text = format_gc_human(report)
        assert "DRY RUN" in text

    def test_human_format_index_pruned(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_memento(repo, "TICKET-001", "SES-OLD-001", age_days=45)
        _write_index(
            repo,
            [
                {"session_id": "SES-OLD-001", "ticket_id": "TICKET-001"},
            ],
        )
        report = gc(repo)
        text = format_gc_human(report)
        assert "index:" in text
        assert "pruned" in text


# ── CLI Integration ─────────────────────────────────────────────────


class TestCLIGC:
    def test_json_output(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), "gc"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"]
        assert "mementos_scanned" in data["data"]
        assert "total_removed" in data["data"]

    def test_human_output(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "human", "--repo", str(repo), "gc"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "Garbage Collection: COMPLETE" in result.stdout

    def test_dry_run_flag(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), "gc", "--dry-run"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["dry_run"] is True

    def test_max_age_flag(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), "gc", "--max-age-days", "7"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["max_age_days"] == 7.0

    def test_gc_with_old_files(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_memento(repo, "TICKET-001", "SES-OLD-001", age_days=45)
        _create_cursor(repo, "agent-old", age_days=60)
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), "gc"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["mementos_removed"] == 1
        assert data["data"]["cursors_removed"] == 1
