"""Tests for process liveness validation in sessions.

Covers:
- PID tracking in session payload (start includes os.getpid())
- PID liveness check (_pid_alive helper)
- scan_sessions reports pid and pid_alive fields
- Dead-PID sessions flagged as stale
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from exo.kernel import governance as governance_mod
from exo.kernel import tickets
from exo.orchestrator.session import (
    SESSION_CACHE_DIR,
    AgentSessionManager,
    _pid_alive,
    scan_sessions,
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


def _create_ticket(repo: Path, ticket_id: str) -> dict[str, Any]:
    ticket_data = {
        "id": ticket_id,
        "title": f"Test ticket {ticket_id}",
        "intent": f"Test intent {ticket_id}",
        "status": "active",
        "priority": 3,
        "checks": [],
        "scope": {"allow": ["**"], "deny": []},
    }
    tickets.save_ticket(repo, ticket_data)
    tickets.acquire_lock(repo, ticket_id, owner="test-agent", role="developer")
    return ticket_data


# ── PID Alive Helper ────────────────────────────────────────────────


class TestPidAlive:
    def test_current_process_alive(self) -> None:
        pid = os.getpid()
        assert _pid_alive(pid) is True

    def test_none_pid(self) -> None:
        assert _pid_alive(None) is None

    def test_dead_pid(self) -> None:
        # PID 999999999 is extremely unlikely to be alive
        result = _pid_alive(999999999)
        assert result is False

    def test_pid_1_alive(self) -> None:
        # PID 1 (init/launchd) should always be alive
        result = _pid_alive(1)
        assert result is True  # alive (or PermissionError → True)


# ── PID Tracking in Session ────────────────────────────────────────


class TestSessionPID:
    def test_start_includes_pid(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        manager = AgentSessionManager(repo, actor="test-agent")
        result = manager.start(ticket_id="TICKET-001")
        session = result["session"]
        assert "pid" in session
        assert session["pid"] == os.getpid()

    def test_pid_in_active_file(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        manager = AgentSessionManager(repo, actor="test-agent")
        manager.start(ticket_id="TICKET-001")
        # Read the active session file directly
        active_path = repo / SESSION_CACHE_DIR / "test-agent.active.json"
        data = json.loads(active_path.read_text(encoding="utf-8"))
        assert data["pid"] == os.getpid()


# ── Scan Sessions with PID ──────────────────────────────────────────


class TestScanSessionsPID:
    def test_scan_includes_pid_fields(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        manager = AgentSessionManager(repo, actor="test-agent")
        manager.start(ticket_id="TICKET-001")
        scan = scan_sessions(repo)
        assert len(scan["active_sessions"]) == 1
        entry = scan["active_sessions"][0]
        assert "pid" in entry
        assert "pid_alive" in entry
        assert entry["pid"] == os.getpid()
        assert entry["pid_alive"] is True

    def test_scan_no_pid_field(self, tmp_path: Path) -> None:
        """Old session files without pid field should report None."""
        repo = _bootstrap_repo(tmp_path)
        cache_dir = repo / SESSION_CACHE_DIR
        cache_dir.mkdir(parents=True, exist_ok=True)
        active_path = cache_dir / "legacy-agent.active.json"
        active_path.write_text(
            json.dumps(
                {
                    "session_id": "SES-LEGACY",
                    "status": "active",
                    "actor": "legacy-agent",
                    "ticket_id": "TICKET-LEGACY",
                    "started_at": "2025-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        scan = scan_sessions(repo)
        assert len(scan["active_sessions"]) == 1
        entry = scan["active_sessions"][0]
        assert entry["pid"] is None
        assert entry["pid_alive"] is None

    def test_dead_pid_flagged_stale(self, tmp_path: Path) -> None:
        """Sessions with dead PIDs should appear in stale_sessions."""
        repo = _bootstrap_repo(tmp_path)
        cache_dir = repo / SESSION_CACHE_DIR
        cache_dir.mkdir(parents=True, exist_ok=True)
        active_path = cache_dir / "dead-agent.active.json"
        active_path.write_text(
            json.dumps(
                {
                    "session_id": "SES-DEAD",
                    "status": "active",
                    "actor": "dead-agent",
                    "ticket_id": "TICKET-DEAD",
                    "started_at": "2025-01-01T00:00:00+00:00",
                    "pid": 999999999,  # dead PID
                }
            ),
            encoding="utf-8",
        )
        scan = scan_sessions(repo)
        assert len(scan["active_sessions"]) == 1
        entry = scan["active_sessions"][0]
        assert entry["pid"] == 999999999
        assert entry["pid_alive"] is False
        # Dead PID should appear in stale_sessions even if not age-stale
        assert len(scan["stale_sessions"]) >= 1
        stale_ids = [s["session_id"] for s in scan["stale_sessions"]]
        assert "SES-DEAD" in stale_ids

    def test_alive_pid_not_stale(self, tmp_path: Path) -> None:
        """Sessions with alive PIDs and recent start should not be stale."""
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        manager = AgentSessionManager(repo, actor="test-agent")
        manager.start(ticket_id="TICKET-001")
        scan = scan_sessions(repo)
        assert len(scan["stale_sessions"]) == 0

    def test_pid_as_string(self, tmp_path: Path) -> None:
        """PID stored as string in old session files should be parsed."""
        repo = _bootstrap_repo(tmp_path)
        cache_dir = repo / SESSION_CACHE_DIR
        cache_dir.mkdir(parents=True, exist_ok=True)
        active_path = cache_dir / "str-pid.active.json"
        current_pid = os.getpid()
        active_path.write_text(
            json.dumps(
                {
                    "session_id": "SES-STRPID",
                    "status": "active",
                    "actor": "str-pid",
                    "ticket_id": "TICKET-STRPID",
                    "started_at": "2025-01-01T00:00:00+00:00",
                    "pid": str(current_pid),
                }
            ),
            encoding="utf-8",
        )
        scan = scan_sessions(repo)
        entry = scan["active_sessions"][0]
        assert entry["pid"] == current_pid
        assert entry["pid_alive"] is True
