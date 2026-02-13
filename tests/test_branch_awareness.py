"""Tests for branch awareness / sibling session feature.

Verifies:
- _current_git_branch() helper
- git_branch recorded in session payload at start/resume
- Branch shown in _exo_banner() for start/finish/resume events
- Sibling session awareness injected into bootstrap prompt
- Branch drift detection at session-finish
- scan_sessions() includes git_branch per entry
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from exo.kernel import governance as governance_mod
from exo.kernel import tickets as tickets_mod
from exo.orchestrator import AgentSessionManager
from exo.orchestrator.session import (
    _current_git_branch,
    _exo_banner,
    scan_sessions,
    SESSION_CACHE_DIR,
    SESSION_INDEX_PATH,
)
from exo.kernel.utils import ensure_dir


def _policy_block(payload: dict) -> str:
    return "```yaml exo-policy\n" + json.dumps(payload, ensure_ascii=True, indent=2) + "\n```\n"


def _bootstrap_repo(tmp_path: Path) -> Path:
    repo = tmp_path
    exo_dir = repo / ".exo"
    exo_dir.mkdir(parents=True, exist_ok=True)
    constitution = (
        "# Test Constitution\n\n"
        + _policy_block({
            "id": "RULE-SEC-001",
            "type": "filesystem_deny",
            "patterns": ["**/.env*"],
            "actions": ["read", "write"],
            "message": "Secret deny",
        })
    )
    (exo_dir / "CONSTITUTION.md").write_text(constitution, encoding="utf-8")
    governance_mod.compile_constitution(repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    return repo


def _seed_ticket(repo: Path, ticket_id: str = "TICKET-111") -> None:
    tickets_mod.save_ticket(repo, {
        "id": ticket_id,
        "type": "feature",
        "title": "Branch awareness test ticket",
        "status": "active",
        "priority": 4,
        "scope": {"allow": ["README.md", ".exo/**"], "deny": []},
        "checks": [],
    })


def _acquire_lock(repo: Path, ticket_id: str = "TICKET-111", owner: str = "agent:test") -> None:
    tickets_mod.acquire_lock(repo, ticket_id, owner=owner, role="developer", duration_hours=1)


# ── _current_git_branch tests ────────────────────────────────────


class TestCurrentGitBranch:
    def test_returns_string(self, tmp_path: Path) -> None:
        # On non-git dir, should return empty string
        result = _current_git_branch(tmp_path)
        assert isinstance(result, str)

    def test_returns_empty_on_non_git(self, tmp_path: Path) -> None:
        result = _current_git_branch(tmp_path)
        assert result == ""

    @patch("exo.orchestrator.session.subprocess.run")
    def test_returns_branch_from_git(self, mock_run: object, tmp_path: Path) -> None:
        mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "feature/cool-thing\n"})()
        result = _current_git_branch(tmp_path)
        assert result == "feature/cool-thing"

    @patch("exo.orchestrator.session.subprocess.run")
    def test_handles_git_failure(self, mock_run: object, tmp_path: Path) -> None:
        mock_run.return_value = type("R", (), {"returncode": 1, "stdout": ""})()
        result = _current_git_branch(tmp_path)
        assert result == ""

    @patch("exo.orchestrator.session.subprocess.run", side_effect=FileNotFoundError("no git"))
    def test_handles_no_git_binary(self, mock_run: object, tmp_path: Path) -> None:
        result = _current_git_branch(tmp_path)
        assert result == ""


# ── Banner branch display tests ──────────────────────────────────


class TestBannerBranch:
    def test_start_banner_includes_branch(self) -> None:
        banner = _exo_banner(
            event="start",
            ticket_id="T-001",
            actor="agent:test",
            branch="feature/auth",
        )
        assert "branch: feature/auth" in banner

    def test_start_banner_no_branch_when_empty(self) -> None:
        banner = _exo_banner(
            event="start",
            ticket_id="T-001",
            actor="agent:test",
            branch="",
        )
        assert "branch:" not in banner

    def test_finish_banner_includes_branch(self) -> None:
        banner = _exo_banner(
            event="finish",
            ticket_id="T-001",
            verify="passed",
            branch="main",
        )
        assert "branch: main" in banner

    def test_resume_banner_includes_branch(self) -> None:
        banner = _exo_banner(
            event="resume",
            ticket_id="T-001",
            actor="agent:test",
            branch="feature/branch-aware",
        )
        assert "branch: feature/branch-aware" in banner

    def test_banner_branch_default_empty(self) -> None:
        # Default is empty, so no branch line should appear
        banner = _exo_banner(event="start", ticket_id="T-001", actor="a")
        assert "branch:" not in banner


# ── Session start records git_branch ─────────────────────────────


class TestSessionStartBranch:
    @patch("exo.orchestrator.session._current_git_branch", return_value="feature/new-thing")
    def test_start_records_git_branch_in_payload(self, mock_branch: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _acquire_lock(repo)
        manager = AgentSessionManager(repo, actor="agent:test")
        out = manager.start(ticket_id="TICKET-111", vendor="test", model="test-model")
        session = out["session"]
        assert session["git_branch"] == "feature/new-thing"

    @patch("exo.orchestrator.session._current_git_branch", return_value="feature/x")
    def test_start_bootstrap_includes_git_branch(self, mock_branch: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _acquire_lock(repo)
        manager = AgentSessionManager(repo, actor="agent:test")
        out = manager.start(ticket_id="TICKET-111")
        bootstrap = out["bootstrap_prompt"]
        assert "git_branch: feature/x" in bootstrap

    @patch("exo.orchestrator.session._current_git_branch", return_value="main")
    def test_start_banner_contains_branch(self, mock_branch: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _acquire_lock(repo)
        manager = AgentSessionManager(repo, actor="agent:test")
        out = manager.start(ticket_id="TICKET-111")
        assert "branch: main" in out["exo_banner"]


# ── Sibling session awareness ────────────────────────────────────


class TestSiblingAwareness:
    @patch("exo.orchestrator.session._current_git_branch", return_value="feature/b")
    def test_no_siblings_no_section(self, mock_branch: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _acquire_lock(repo)
        manager = AgentSessionManager(repo, actor="agent:test")
        out = manager.start(ticket_id="TICKET-111")
        assert "Sibling Sessions" not in out["bootstrap_prompt"]

    @patch("exo.orchestrator.session._current_git_branch", return_value="feature/b")
    def test_sibling_injected_into_bootstrap(self, mock_branch: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _seed_ticket(repo, "TICKET-222")

        # Create a "sibling" active session for a different actor
        cache_dir = repo / SESSION_CACHE_DIR
        ensure_dir(cache_dir)
        sibling_payload = {
            "session_id": "SES-SIBLING-001",
            "status": "active",
            "actor": "agent:other",
            "ticket_id": "TICKET-222",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "git_branch": "feature/other-work",
            "pid": 99999,
        }
        (cache_dir / "agent-other.active.json").write_text(
            json.dumps(sibling_payload), encoding="utf-8"
        )

        _acquire_lock(repo)
        manager = AgentSessionManager(repo, actor="agent:test")
        out = manager.start(ticket_id="TICKET-111")
        bootstrap = out["bootstrap_prompt"]
        assert "Sibling Sessions" in bootstrap
        assert "agent:other" in bootstrap
        assert "TICKET-222" in bootstrap
        assert "feature/other-work" in bootstrap

    @patch("exo.orchestrator.session._current_git_branch", return_value="main")
    def test_same_actor_not_listed_as_sibling(self, mock_branch: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)

        # Create active session for same actor (should not show as sibling)
        cache_dir = repo / SESSION_CACHE_DIR
        ensure_dir(cache_dir)
        own_payload = {
            "session_id": "SES-OWN-001",
            "status": "active",
            "actor": "agent:test",
            "ticket_id": "TICKET-111",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "git_branch": "main",
            "pid": 99999,
        }
        (cache_dir / "agent-test.active.json").write_text(
            json.dumps(own_payload), encoding="utf-8"
        )

        _acquire_lock(repo)
        manager = AgentSessionManager(repo, actor="agent:test")
        # Will reuse the existing session (same actor/ticket), so check bootstrap
        # on a fresh start with different ticket
        _seed_ticket(repo, "TICKET-333")
        tickets_mod.release_lock(repo, ticket_id="TICKET-111")
        _acquire_lock(repo, "TICKET-333")

        # Clean up the active file so we can start fresh
        (cache_dir / "agent-test.active.json").unlink(missing_ok=True)
        manager2 = AgentSessionManager(repo, actor="agent:test")
        out = manager2.start(ticket_id="TICKET-333")
        # Own actor should not appear in sibling list
        assert "Sibling Sessions" not in out["bootstrap_prompt"]

    @patch("exo.orchestrator.session._current_git_branch", return_value="feature/x")
    def test_multiple_siblings_listed(self, mock_branch: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _seed_ticket(repo, "TICKET-222")
        _seed_ticket(repo, "TICKET-333")

        cache_dir = repo / SESSION_CACHE_DIR
        ensure_dir(cache_dir)
        for i, (actor, ticket, branch) in enumerate([
            ("agent:alice", "TICKET-222", "feature/alice"),
            ("agent:bob", "TICKET-333", "feature/bob"),
        ]):
            payload = {
                "session_id": f"SES-SIB-{i}",
                "status": "active",
                "actor": actor,
                "ticket_id": ticket,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "git_branch": branch,
                "pid": 99990 + i,
            }
            token = actor.replace(":", "-").replace(".", "-")
            (cache_dir / f"{token}.active.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

        _acquire_lock(repo)
        manager = AgentSessionManager(repo, actor="agent:test")
        out = manager.start(ticket_id="TICKET-111")
        bootstrap = out["bootstrap_prompt"]
        assert "agent:alice" in bootstrap
        assert "agent:bob" in bootstrap
        assert "feature/alice" in bootstrap
        assert "feature/bob" in bootstrap


# ── Session finish — branch drift ────────────────────────────────


class TestBranchDrift:
    def _start_session(self, repo: Path, branch: str = "feature/x") -> dict:
        with patch("exo.orchestrator.session._current_git_branch", return_value=branch):
            _acquire_lock(repo)
            manager = AgentSessionManager(repo, actor="agent:test")
            return manager.start(ticket_id="TICKET-111")

    def test_no_drift_when_same_branch(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        self._start_session(repo, "feature/x")

        with patch("exo.orchestrator.session._current_git_branch", return_value="feature/x"):
            manager = AgentSessionManager(repo, actor="agent:test")
            out = manager.finish(summary="done", skip_check=True, break_glass_reason="test")

        assert out["branch_drifted"] is False
        assert out["git_branch"] == "feature/x"

    def test_drift_detected_when_branch_changed(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        self._start_session(repo, "feature/x")

        with patch("exo.orchestrator.session._current_git_branch", return_value="feature/y"):
            manager = AgentSessionManager(repo, actor="agent:test")
            out = manager.finish(summary="done", skip_check=True, break_glass_reason="test")

        assert out["branch_drifted"] is True
        assert out["git_branch"] == "feature/y"

    def test_no_drift_when_start_branch_empty(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        self._start_session(repo, "")  # no git branch at start

        with patch("exo.orchestrator.session._current_git_branch", return_value="main"):
            manager = AgentSessionManager(repo, actor="agent:test")
            out = manager.finish(summary="done", skip_check=True, break_glass_reason="test")

        # Can't determine drift if start branch unknown
        assert out["branch_drifted"] is False

    def test_finish_banner_includes_branch(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        self._start_session(repo, "feature/x")

        with patch("exo.orchestrator.session._current_git_branch", return_value="feature/x"):
            manager = AgentSessionManager(repo, actor="agent:test")
            out = manager.finish(summary="done", skip_check=True, break_glass_reason="test")

        assert "branch: feature/x" in out["exo_banner"]

    def test_drift_in_memento(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        self._start_session(repo, "feature/x")

        with patch("exo.orchestrator.session._current_git_branch", return_value="feature/y"):
            manager = AgentSessionManager(repo, actor="agent:test")
            out = manager.finish(summary="done", skip_check=True, break_glass_reason="test")

        memento_path = repo / out["memento_path"]
        memento = memento_path.read_text(encoding="utf-8")
        assert "git_branch_start: feature/x" in memento
        assert "git_branch_finish: feature/y" in memento
        assert "branch_drifted: True" in memento

    def test_drift_in_session_index(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        self._start_session(repo, "feature/x")

        with patch("exo.orchestrator.session._current_git_branch", return_value="feature/y"):
            manager = AgentSessionManager(repo, actor="agent:test")
            manager.finish(summary="done", skip_check=True, break_glass_reason="test")

        index_path = repo / SESSION_INDEX_PATH
        rows = [json.loads(line) for line in index_path.read_text().strip().splitlines()]
        assert len(rows) >= 1
        last_row = rows[-1]
        assert last_row["git_branch"] == "feature/y"
        assert last_row["branch_drifted"] is True


# ── Session resume — branch tracking ────────────────────────────


class TestResumeBranch:
    @patch("exo.orchestrator.session._current_git_branch", return_value="feature/x")
    def test_resume_records_branch(self, mock_branch: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _acquire_lock(repo)

        manager = AgentSessionManager(repo, actor="agent:test")
        manager.start(ticket_id="TICKET-111")
        manager.suspend(reason="lunch break")

        out = manager.resume()
        session = out["session"]
        assert session["git_branch"] == "feature/x"

    @patch("exo.orchestrator.session._current_git_branch", return_value="feature/resumed")
    def test_resume_bootstrap_includes_branch(self, mock_branch: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _acquire_lock(repo)

        manager = AgentSessionManager(repo, actor="agent:test")
        manager.start(ticket_id="TICKET-111")
        manager.suspend(reason="break")

        out = manager.resume()
        assert "git_branch: feature/resumed" in out["bootstrap_prompt"]

    @patch("exo.orchestrator.session._current_git_branch", return_value="feature/r")
    def test_resume_banner_includes_branch(self, mock_branch: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _acquire_lock(repo)

        manager = AgentSessionManager(repo, actor="agent:test")
        manager.start(ticket_id="TICKET-111")
        manager.suspend(reason="break")

        out = manager.resume()
        assert "branch: feature/r" in out["exo_banner"]


# ── scan_sessions includes git_branch ────────────────────────────


class TestScanSessionsBranch:
    def test_scan_includes_git_branch(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        cache_dir = repo / SESSION_CACHE_DIR
        ensure_dir(cache_dir)

        payload = {
            "session_id": "SES-SCAN-001",
            "status": "active",
            "actor": "agent:scan",
            "ticket_id": "TICKET-001",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "git_branch": "feature/scan-test",
            "pid": 12345,
        }
        (cache_dir / "agent-scan.active.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

        scan = scan_sessions(repo)
        active = scan["active_sessions"]
        assert len(active) == 1
        assert active[0]["git_branch"] == "feature/scan-test"

    def test_scan_empty_branch_when_missing(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        cache_dir = repo / SESSION_CACHE_DIR
        ensure_dir(cache_dir)

        payload = {
            "session_id": "SES-OLD-001",
            "status": "active",
            "actor": "agent:old",
            "ticket_id": "TICKET-001",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "pid": 12345,
            # no git_branch key — simulates pre-feature session
        }
        (cache_dir / "agent-old.active.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

        scan = scan_sessions(repo)
        active = scan["active_sessions"]
        assert len(active) == 1
        assert active[0]["git_branch"] == ""
