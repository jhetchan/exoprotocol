"""Tests for concurrent session support with scope partitioning.

Covers:
- Concurrent sessions with disjoint scopes (multi-agent teams)
- Scope partition enforcement (blocks on overlap)
- Concurrent session finish behavior
- Coexistence with lock-based sessions
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from exo.kernel import governance as governance_mod
from exo.kernel import tickets
from exo.kernel.errors import ExoError
from exo.orchestrator.session import HANDOFF_PREFIX, SESSION_CACHE_DIR, AgentSessionManager, scan_sessions
from exo.stdlib.conflicts import enforce_scope_partition


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


def _create_ticket(
    repo: Path,
    ticket_id: str,
    scope_allow: list[str] | None = None,
) -> dict[str, Any]:
    ticket_data = {
        "id": ticket_id,
        "title": f"Test ticket {ticket_id}",
        "intent": f"Test intent {ticket_id}",
        "status": "active",
        "priority": 3,
        "checks": [],
        "scope": {"allow": scope_allow or ["**"], "deny": []},
    }
    tickets.save_ticket(repo, ticket_data)
    return ticket_data


def _create_ticket_with_lock(
    repo: Path,
    ticket_id: str,
    owner: str,
    scope_allow: list[str] | None = None,
) -> dict[str, Any]:
    ticket_data = _create_ticket(repo, ticket_id, scope_allow)
    tickets.acquire_lock(repo, ticket_id, owner=owner, role="developer")
    return ticket_data


# ── Scope Partition Enforcement ─────────────────────────────────────


class TestEnforceScopePartition:
    def test_disjoint_passes(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-A", scope_allow=["src/**"])
        _create_ticket(repo, "TICKET-B", scope_allow=["tests/**"])
        siblings = [{"ticket_id": "TICKET-B", "actor": "agent-b"}]
        # Should not raise
        enforce_scope_partition(repo, "TICKET-A", {"allow": ["src/**"], "deny": []}, siblings)

    def test_overlap_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-A", scope_allow=["src/**"])
        _create_ticket(repo, "TICKET-B", scope_allow=["src/api/**"])
        siblings = [{"ticket_id": "TICKET-B", "actor": "agent-b"}]
        with pytest.raises(ExoError, match="SCOPE_PARTITION_VIOLATION"):
            enforce_scope_partition(repo, "TICKET-A", {"allow": ["src/**"], "deny": []}, siblings)

    def test_both_default_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-A")  # default scope ["**"]
        _create_ticket(repo, "TICKET-B")  # default scope ["**"]
        siblings = [{"ticket_id": "TICKET-B", "actor": "agent-b"}]
        with pytest.raises(ExoError, match="concurrent sessions require explicit scope"):
            enforce_scope_partition(repo, "TICKET-A", {"allow": ["**"], "deny": []}, siblings)

    def test_no_siblings_passes(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-A", scope_allow=["src/**"])
        # No siblings — should not raise
        enforce_scope_partition(repo, "TICKET-A", {"allow": ["src/**"], "deny": []}, [])

    def test_error_includes_details(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-A", scope_allow=["src/**"])
        _create_ticket(repo, "TICKET-B", scope_allow=["src/api/**"])
        siblings = [{"ticket_id": "TICKET-B", "actor": "agent-b"}]
        with pytest.raises(ExoError) as exc_info:
            enforce_scope_partition(repo, "TICKET-A", {"allow": ["src/**"], "deny": []}, siblings)
        details = exc_info.value.details
        assert details["sibling_ticket"] == "TICKET-B"
        assert details["sibling_actor"] == "agent-b"
        assert "overlapping_patterns" in details


# ── Concurrent Session Start ────────────────────────────────────────


class TestConcurrentSessionStart:
    def test_two_agents_disjoint_scopes(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TICKET-A", owner="agent-a", scope_allow=["src/**"])
        _create_ticket(repo, "TICKET-B", scope_allow=["tests/**"])

        # Agent A starts normally with lock
        mgr_a = AgentSessionManager(repo, actor="agent-a")
        result_a = mgr_a.start(ticket_id="TICKET-A")
        assert not result_a.get("reused")

        # Agent B starts concurrently (no lock needed)
        mgr_b = AgentSessionManager(repo, actor="agent-b")
        result_b = mgr_b.start(ticket_id="TICKET-B", concurrent=True)
        assert not result_b.get("reused")
        assert result_b["session"]["session_id"]

        # Both sessions active
        scan = scan_sessions(repo)
        assert len(scan["active_sessions"]) == 2

    def test_two_agents_overlapping_scopes(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TICKET-A", owner="agent-a", scope_allow=["src/**"])
        _create_ticket(repo, "TICKET-B", scope_allow=["src/api/**"])

        # Agent A starts normally
        mgr_a = AgentSessionManager(repo, actor="agent-a")
        mgr_a.start(ticket_id="TICKET-A")

        # Agent B blocked — scopes overlap
        mgr_b = AgentSessionManager(repo, actor="agent-b")
        with pytest.raises(ExoError, match="SCOPE_PARTITION_VIOLATION"):
            mgr_b.start(ticket_id="TICKET-B", concurrent=True)

    def test_both_default_scope_blocked(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TICKET-A", owner="agent-a")  # default ["**"]
        _create_ticket(repo, "TICKET-B")  # default ["**"]

        mgr_a = AgentSessionManager(repo, actor="agent-a")
        mgr_a.start(ticket_id="TICKET-A")

        mgr_b = AgentSessionManager(repo, actor="agent-b")
        with pytest.raises(ExoError, match="concurrent sessions require explicit scope"):
            mgr_b.start(ticket_id="TICKET-B", concurrent=True)

    def test_concurrent_no_lock_required(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-B", scope_allow=["tests/**"])
        # No lock at all — concurrent mode doesn't need one
        mgr_b = AgentSessionManager(repo, actor="agent-b")
        result = mgr_b.start(ticket_id="TICKET-B", concurrent=True)
        assert result["session"]["session_id"]
        assert result["session"]["concurrent"] is True

    def test_concurrent_with_lock_coexists(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TICKET-A", owner="agent-a", scope_allow=["src/**"])
        _create_ticket(repo, "TICKET-B", scope_allow=["tests/**"])

        # Agent A has the lock
        mgr_a = AgentSessionManager(repo, actor="agent-a")
        mgr_a.start(ticket_id="TICKET-A")

        # Agent B starts concurrent — lock belongs to A, but B doesn't need it
        mgr_b = AgentSessionManager(repo, actor="agent-b")
        result_b = mgr_b.start(ticket_id="TICKET-B", concurrent=True)
        assert result_b["session"]["concurrent"] is True

        # Lock still belongs to A
        lock = tickets.load_lock(repo)
        assert lock["ticket_id"] == "TICKET-A"
        assert lock["owner"] == "agent-a"

    def test_three_agents_pairwise_disjoint(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TICKET-A", owner="agent-a", scope_allow=["src/**"])
        _create_ticket(repo, "TICKET-B", scope_allow=["tests/**"])
        _create_ticket(repo, "TICKET-C", scope_allow=["docs/**"])

        mgr_a = AgentSessionManager(repo, actor="agent-a")
        mgr_a.start(ticket_id="TICKET-A")

        mgr_b = AgentSessionManager(repo, actor="agent-b")
        mgr_b.start(ticket_id="TICKET-B", concurrent=True)

        mgr_c = AgentSessionManager(repo, actor="agent-c")
        result_c = mgr_c.start(ticket_id="TICKET-C", concurrent=True)
        assert result_c["session"]["session_id"]

        scan = scan_sessions(repo)
        assert len(scan["active_sessions"]) == 3

    def test_normal_mode_still_requires_lock(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-A", scope_allow=["src/**"])
        # No lock, no concurrent flag → should fail
        mgr = AgentSessionManager(repo, actor="agent-a")
        with pytest.raises(ExoError, match="LOCK_REQUIRED"):
            mgr.start(ticket_id="TICKET-A")

    def test_concurrent_session_payload(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-A", scope_allow=["src/**"])
        mgr = AgentSessionManager(repo, actor="agent-a")
        result = mgr.start(ticket_id="TICKET-A", concurrent=True)
        assert result["session"]["concurrent"] is True

    def test_concurrent_banner(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-A", scope_allow=["src/**"])
        mgr = AgentSessionManager(repo, actor="agent-a")
        result = mgr.start(ticket_id="TICKET-A", concurrent=True)
        assert "CONCURRENT SESSION" in result["exo_banner"]


# ── Concurrent Session Finish ───────────────────────────────────────


class TestConcurrentSessionFinish:
    def test_concurrent_finish_works(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-A", scope_allow=["src/**"])
        mgr = AgentSessionManager(repo, actor="agent-a")
        mgr.start(ticket_id="TICKET-A", concurrent=True)
        result = mgr.finish(
            summary="Concurrent session done",
            ticket_id="TICKET-A",
            set_status="keep",
            skip_check=True,
            break_glass_reason="concurrent session close",
        )
        assert result["session_id"]
        assert result["verify"] == "bypassed"

    def test_concurrent_finish_no_lock_noop(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-A", scope_allow=["src/**"])
        mgr = AgentSessionManager(repo, actor="agent-a")
        mgr.start(ticket_id="TICKET-A", concurrent=True)
        result = mgr.finish(
            summary="Done",
            ticket_id="TICKET-A",
            set_status="keep",
            skip_check=True,
            break_glass_reason="concurrent close",
            release_lock=True,
        )
        # No lock was held — release is a no-op
        assert result["released_lock"] is False

    def test_concurrent_finish_preserves_sibling(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TICKET-A", owner="agent-a", scope_allow=["src/**"])
        _create_ticket(repo, "TICKET-B", scope_allow=["tests/**"])

        mgr_a = AgentSessionManager(repo, actor="agent-a")
        mgr_a.start(ticket_id="TICKET-A")

        mgr_b = AgentSessionManager(repo, actor="agent-b")
        mgr_b.start(ticket_id="TICKET-B", concurrent=True)

        # Finish B — A should still be active
        mgr_b.finish(
            summary="B done",
            ticket_id="TICKET-B",
            set_status="keep",
            skip_check=True,
            break_glass_reason="concurrent close",
        )

        assert mgr_a.get_active() is not None
        assert mgr_b.get_active() is None


# ── Scan Sessions with Concurrent ───────────────────────────────────


class TestScanSessionsConcurrent:
    def test_scan_shows_concurrent_sessions(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TICKET-A", owner="agent-a", scope_allow=["src/**"])
        _create_ticket(repo, "TICKET-B", scope_allow=["tests/**"])

        mgr_a = AgentSessionManager(repo, actor="agent-a")
        mgr_a.start(ticket_id="TICKET-A")

        mgr_b = AgentSessionManager(repo, actor="agent-b")
        mgr_b.start(ticket_id="TICKET-B", concurrent=True)

        scan = scan_sessions(repo)
        actors = {s["actor"] for s in scan["active_sessions"]}
        assert "agent-a" in actors
        assert "agent-b" in actors

    def test_concurrent_field_in_active_session(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-A", scope_allow=["src/**"])
        mgr = AgentSessionManager(repo, actor="agent-a")
        mgr.start(ticket_id="TICKET-A", concurrent=True)

        # Read the active session file directly
        active_path = repo / SESSION_CACHE_DIR / "agent-a.active.json"
        data = json.loads(active_path.read_text(encoding="utf-8"))
        assert data["concurrent"] is True


# ── Agent Handoff ──────────────────────────────────────────────────


class TestHandoff:
    def test_handoff_finishes_session(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TICKET-A", owner="agent-a")
        mgr = AgentSessionManager(repo, actor="agent-a")
        mgr.start(ticket_id="TICKET-A")
        assert mgr.get_active() is not None

        mgr.handoff(
            to_actor="agent-b",
            ticket_id="TICKET-A",
            summary="Done with API, need tests",
        )
        assert mgr.get_active() is None

    def test_handoff_writes_record(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TICKET-A", owner="agent-a")
        mgr = AgentSessionManager(repo, actor="agent-a")
        mgr.start(ticket_id="TICKET-A")

        mgr.handoff(
            to_actor="agent-b",
            ticket_id="TICKET-A",
            summary="Implemented endpoints",
            reason="Need testing expertise",
            next_steps="Write integration tests",
        )

        handoff_path = repo / f"{HANDOFF_PREFIX}TICKET-A.json"
        assert handoff_path.exists()
        record = json.loads(handoff_path.read_text(encoding="utf-8"))
        assert record["from_actor"] == "agent-a"
        assert record["to_actor"] == "agent-b"
        assert record["ticket_id"] == "TICKET-A"
        assert record["summary"] == "Implemented endpoints"
        assert record["reason"] == "Need testing expertise"
        assert record["next_steps"] == "Write integration tests"

    def test_handoff_releases_lock(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TICKET-A", owner="agent-a")
        mgr = AgentSessionManager(repo, actor="agent-a")
        mgr.start(ticket_id="TICKET-A")

        result = mgr.handoff(
            to_actor="agent-b",
            ticket_id="TICKET-A",
            summary="Done",
            release_lock=True,
        )
        assert result["released_lock"] is True
        assert tickets.load_lock(repo) is None

    def test_handoff_keeps_lock(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TICKET-A", owner="agent-a")
        mgr = AgentSessionManager(repo, actor="agent-a")
        mgr.start(ticket_id="TICKET-A")

        result = mgr.handoff(
            to_actor="agent-b",
            ticket_id="TICKET-A",
            summary="Done",
            release_lock=False,
        )
        assert result["released_lock"] is False
        lock = tickets.load_lock(repo)
        assert lock is not None
        assert lock["ticket_id"] == "TICKET-A"

    def test_handoff_no_active_session_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-A")
        mgr = AgentSessionManager(repo, actor="agent-a")

        with pytest.raises(ExoError, match="SESSION_NOT_ACTIVE"):
            mgr.handoff(
                to_actor="agent-b",
                ticket_id="TICKET-A",
                summary="Done",
            )

    def test_handoff_wrong_ticket_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TICKET-A", owner="agent-a")
        _create_ticket(repo, "TICKET-B")
        mgr = AgentSessionManager(repo, actor="agent-a")
        mgr.start(ticket_id="TICKET-A")

        with pytest.raises(ExoError, match="SESSION_TICKET_MISMATCH"):
            mgr.handoff(
                to_actor="agent-b",
                ticket_id="TICKET-B",
                summary="Done",
            )

    def test_handoff_consumed_on_start(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TICKET-A", owner="agent-a")
        mgr_a = AgentSessionManager(repo, actor="agent-a")
        mgr_a.start(ticket_id="TICKET-A")
        mgr_a.handoff(
            to_actor="agent-b",
            ticket_id="TICKET-A",
            summary="Done with API",
            release_lock=True,
        )

        # Handoff record exists before TO starts
        handoff_path = repo / f"{HANDOFF_PREFIX}TICKET-A.json"
        assert handoff_path.exists()

        # TO agent starts — handoff should be consumed
        tickets.acquire_lock(repo, "TICKET-A", owner="agent-b", role="developer")
        mgr_b = AgentSessionManager(repo, actor="agent-b")
        mgr_b.start(ticket_id="TICKET-A")
        assert not handoff_path.exists()

    def test_handoff_in_bootstrap(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TICKET-A", owner="agent-a")
        mgr_a = AgentSessionManager(repo, actor="agent-a")
        mgr_a.start(ticket_id="TICKET-A")
        mgr_a.handoff(
            to_actor="agent-b",
            ticket_id="TICKET-A",
            summary="Built the API layer",
            reason="Needs test specialist",
            next_steps="Write unit tests",
            release_lock=True,
        )

        tickets.acquire_lock(repo, "TICKET-A", owner="agent-b", role="developer")
        mgr_b = AgentSessionManager(repo, actor="agent-b")
        result = mgr_b.start(ticket_id="TICKET-A")
        bootstrap = result.get("bootstrap_prompt", "")
        assert "Handoff Context" in bootstrap
        assert "agent-a" in bootstrap
        assert "Built the API layer" in bootstrap
        assert "Needs test specialist" in bootstrap
        assert "Write unit tests" in bootstrap

    def test_handoff_payload_has_record(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TICKET-A", owner="agent-a")
        mgr_a = AgentSessionManager(repo, actor="agent-a")
        mgr_a.start(ticket_id="TICKET-A")
        mgr_a.handoff(
            to_actor="agent-b",
            ticket_id="TICKET-A",
            summary="Done",
            release_lock=True,
        )

        tickets.acquire_lock(repo, "TICKET-A", owner="agent-b", role="developer")
        mgr_b = AgentSessionManager(repo, actor="agent-b")
        mgr_b.start(ticket_id="TICKET-A")
        active = mgr_b.get_active()
        assert active is not None
        assert active["handoff_from"] is not None
        assert active["handoff_from"]["from_actor"] == "agent-a"

    def test_handoff_return_shape(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TICKET-A", owner="agent-a")
        mgr = AgentSessionManager(repo, actor="agent-a")
        mgr.start(ticket_id="TICKET-A")

        result = mgr.handoff(
            to_actor="agent-b",
            ticket_id="TICKET-A",
            summary="Done",
        )
        assert "handoff_id" in result
        assert "from_actor" in result
        assert "to_actor" in result
        assert "from_session_id" in result
        assert "released_lock" in result
        assert result["from_actor"] == "agent-a"
        assert result["to_actor"] == "agent-b"
