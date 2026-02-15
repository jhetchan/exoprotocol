"""Tests for ticket archival and git SHA traceability.

Covers:
- archive_ticket() — single ticket archival
- archive_done_tickets() — batch archival of all done tickets
- CLI: exo ticket-archive
- GC integration: exo gc --archive-done
- Git SHA traceability: commits captured at session-finish, stored on ticket
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from exo.kernel import governance as governance_mod
from exo.kernel import tickets
from exo.kernel.tickets import (
    archive_done_tickets,
    archive_ticket,
    normalize_ticket,
    save_ticket,
)
from exo.stdlib.gc import gc, gc_to_dict, format_gc_human


def _policy_block(rule: dict[str, Any]) -> str:
    return f"\n```yaml exo-policy\n{json.dumps(rule)}\n```\n"


def _bootstrap_repo(tmp_path: Path) -> Path:
    repo = tmp_path
    exo_dir = repo / ".exo"
    exo_dir.mkdir(parents=True, exist_ok=True)

    deny_rule = {
        "id": "RULE-SEC-001",
        "type": "filesystem_deny",
        "patterns": ["**/.env*"],
        "actions": ["read", "write"],
        "message": "Secret deny",
    }
    lock_rule = {
        "id": "RULE-LOCK-001",
        "type": "require_lock",
        "message": "Lock required",
    }
    constitution = "# Constitution\n" + _policy_block(deny_rule) + _policy_block(lock_rule)
    (exo_dir / "CONSTITUTION.md").write_text(constitution, encoding="utf-8")
    governance_mod.compile_constitution(repo)
    return repo


def _create_ticket(repo: Path, ticket_id: str, status: str = "done") -> dict[str, Any]:
    """Create a ticket with given status."""
    ticket = {
        "id": ticket_id,
        "title": f"Test ticket {ticket_id}",
        "intent": f"Test {ticket_id}",
        "priority": 2,
        "labels": [],
        "type": "feature",
        "status": status,
    }
    save_ticket(repo, ticket)
    return normalize_ticket(ticket)


# ── Single Ticket Archival ─────────────────────────────────────────


class TestArchiveTicket:
    def test_archive_done_ticket(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TKT-001", status="done")
        dest = archive_ticket(repo, "TKT-001")
        assert dest.exists()
        assert "ARCHIVE" in str(dest)
        assert not (repo / ".exo" / "tickets" / "TKT-001.yaml").exists()

    def test_archive_not_done_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TKT-002", status="todo")
        try:
            archive_ticket(repo, "TKT-002")
            assert False, "Should have raised ExoError"
        except Exception as e:
            assert "TICKET_NOT_DONE" in str(e)

    def test_archive_not_found_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        try:
            archive_ticket(repo, "TKT-NONEXIST")
            assert False, "Should have raised ExoError"
        except Exception as e:
            assert "TICKET_NOT_FOUND" in str(e)

    def test_archived_file_readable(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TKT-003", status="done")
        dest = archive_ticket(repo, "TKT-003")
        from exo.kernel.utils import load_yaml

        data = load_yaml(dest)
        assert data["id"] == "TKT-003"
        assert data["status"] == "done"


# ── Batch Archival ────────────────────────────────────────────────


class TestArchiveDoneTickets:
    def test_archive_all_done(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001", status="done")
        _create_ticket(repo, "TICKET-002", status="done")
        _create_ticket(repo, "TICKET-003", status="todo")
        archived = archive_done_tickets(repo)
        assert len(archived) == 2
        ids = {a["id"] for a in archived}
        assert "TICKET-001" in ids
        assert "TICKET-002" in ids
        # todo ticket stays
        assert (repo / ".exo" / "tickets" / "TICKET-003.yaml").exists()

    def test_archive_none_done(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-010", status="todo")
        archived = archive_done_tickets(repo)
        assert len(archived) == 0

    def test_archive_empty_repo(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        archived = archive_done_tickets(repo)
        assert len(archived) == 0

    def test_archive_creates_directory(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        archive_dir = repo / ".exo" / "tickets" / "ARCHIVE"
        if archive_dir.exists():
            import shutil

            shutil.rmtree(archive_dir)
        _create_ticket(repo, "TICKET-020", status="done")
        archive_done_tickets(repo)
        assert archive_dir.exists()


# ── CLI Integration ────────────────────────────────────────────────


class TestCLITicketArchive:
    def test_archive_single(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-030", status="done")
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), "ticket-archive", "TICKET-030"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"]
        assert data["data"]["count"] == 1

    def test_archive_all(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-040", status="done")
        _create_ticket(repo, "TICKET-041", status="done")
        _create_ticket(repo, "TICKET-042", status="active")
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), "ticket-archive"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"]
        assert data["data"]["count"] == 2


# ── GC Integration ─────────────────────────────────────────────────


class TestGCArchive:
    def test_gc_without_archive(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-050", status="done")
        report = gc(repo)
        assert report.tickets_archived == 0
        # Ticket still in place
        assert (repo / ".exo" / "tickets" / "TICKET-050.yaml").exists()

    def test_gc_with_archive(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-051", status="done")
        _create_ticket(repo, "TICKET-052", status="todo")
        report = gc(repo, archive_done=True)
        assert report.tickets_archived == 1
        assert "TICKET-051" in report.tickets_archived_ids
        assert not (repo / ".exo" / "tickets" / "TICKET-051.yaml").exists()
        # todo ticket stays
        assert (repo / ".exo" / "tickets" / "TICKET-052.yaml").exists()

    def test_gc_archive_dry_run(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-053", status="done")
        report = gc(repo, archive_done=True, dry_run=True)
        assert report.tickets_archived == 1
        # File still in place during dry run
        assert (repo / ".exo" / "tickets" / "TICKET-053.yaml").exists()

    def test_gc_archive_in_dict(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-054", status="done")
        report = gc(repo, archive_done=True)
        d = gc_to_dict(report)
        assert d["tickets_archived"] == 1
        assert "TICKET-054" in d["tickets_archived_ids"]

    def test_gc_archive_in_human(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-055", status="done")
        report = gc(repo, archive_done=True)
        text = format_gc_human(report)
        assert "tickets archived" in text


# ── Git SHA Traceability ──────────────────────────────────────────


class TestGitSHATraceability:
    def _init_git_repo(self, repo: Path) -> None:
        """Initialize a git repo with an initial commit."""
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, timeout=10)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True)
        (repo / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=str(repo), capture_output=True, timeout=10)

    def test_finish_captures_commits(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        self._init_git_repo(repo)
        _create_ticket(repo, "TKT-SHA-1", status="todo")
        tickets.acquire_lock(repo, "TKT-SHA-1", owner="agent:test", role="developer")

        from exo.orchestrator.session import AgentSessionManager

        mgr = AgentSessionManager(repo, actor="agent:test")
        start_result = mgr.start(vendor="test", model="test")

        # Create a commit during the session
        (repo / "work.py").write_text("print('hello')\n")
        subprocess.run(["git", "add", "work.py"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "session work"], cwd=str(repo), capture_output=True, timeout=10)

        result = mgr.finish(
            summary="test session",
            ticket_id="TKT-SHA-1",
            set_status="done",
            skip_check=True,
            break_glass_reason="test",
        )
        assert "commits" in result
        assert "commit_count" in result

    def test_finish_stores_commits_on_ticket(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        self._init_git_repo(repo)
        _create_ticket(repo, "TKT-SHA-2", status="todo")
        tickets.acquire_lock(repo, "TKT-SHA-2", owner="agent:test", role="developer")

        from exo.orchestrator.session import AgentSessionManager

        mgr = AgentSessionManager(repo, actor="agent:test")
        mgr.start(vendor="test", model="test")

        # Create a commit during the session
        (repo / "file.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "file.py"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "add file"], cwd=str(repo), capture_output=True, timeout=10)

        mgr.finish(
            summary="done",
            ticket_id="TKT-SHA-2",
            set_status="done",
            skip_check=True,
            break_glass_reason="test",
        )

        # Reload ticket and check commits
        ticket = tickets.load_ticket(repo, "TKT-SHA-2")
        assert "commits" in ticket
        assert len(ticket["commits"]) >= 1

    def test_finish_returns_commit_count(self, tmp_path: Path) -> None:
        """commit_count is always present in finish result."""
        repo = _bootstrap_repo(tmp_path)
        self._init_git_repo(repo)
        _create_ticket(repo, "TKT-SHA-3", status="todo")
        tickets.acquire_lock(repo, "TKT-SHA-3", owner="agent:test", role="developer")

        from exo.orchestrator.session import AgentSessionManager

        mgr = AgentSessionManager(repo, actor="agent:test")
        mgr.start(vendor="test", model="test")

        result = mgr.finish(
            summary="no work",
            ticket_id="TKT-SHA-3",
            set_status="keep",
            skip_check=True,
            break_glass_reason="test",
        )
        assert "commit_count" in result
        assert isinstance(result["commit_count"], int)

    def test_commits_in_memento(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        self._init_git_repo(repo)
        _create_ticket(repo, "TKT-SHA-4", status="todo")
        tickets.acquire_lock(repo, "TKT-SHA-4", owner="agent:test", role="developer")

        from exo.orchestrator.session import AgentSessionManager

        mgr = AgentSessionManager(repo, actor="agent:test")
        mgr.start(vendor="test", model="test")

        (repo / "m.py").write_text("m = 1\n")
        subprocess.run(["git", "add", "m.py"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "memento test"], cwd=str(repo), capture_output=True, timeout=10)

        result = mgr.finish(
            summary="memento check",
            ticket_id="TKT-SHA-4",
            set_status="done",
            skip_check=True,
            break_glass_reason="test",
        )

        memento_path = repo / result["memento_path"]
        content = memento_path.read_text(encoding="utf-8")
        assert "## Commits" in content
