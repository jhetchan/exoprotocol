from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from exo.control.syscalls import KernelSyscalls
from exo.kernel import governance as governance_mod
from exo.kernel import ledger as ledger_mod
from exo.kernel import tickets as tickets_mod
from exo.kernel.errors import ExoError
from exo.orchestrator import AgentSessionManager, DistributedWorker
from exo.orchestrator.session import cleanup_sessions, scan_sessions


def _policy_block(payload: dict[str, object]) -> str:
    return "```yaml exo-policy\n" + json.dumps(payload, ensure_ascii=True, indent=2) + "\n```\n"


def _bootstrap_repo(tmp_path: Path) -> Path:
    repo = tmp_path
    exo_dir = repo / ".exo"
    exo_dir.mkdir(parents=True, exist_ok=True)
    constitution = (
        "# Test Constitution\n\n"
        + _policy_block(
            {
                "id": "RULE-SEC-001",
                "type": "filesystem_deny",
                "patterns": ["**/.env*"],
                "actions": ["read", "write"],
                "message": "Secret deny",
            }
        )
    )
    (exo_dir / "CONSTITUTION.md").write_text(constitution, encoding="utf-8")
    governance_mod.compile_constitution(repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    return repo


def _seed_ticket(repo: Path, ticket_id: str = "TICKET-111") -> None:
    tickets_mod.save_ticket(
        repo,
        {
            "id": ticket_id,
            "type": "feature",
            "title": "Session lifecycle test ticket",
            "status": "active",
            "priority": 4,
            "scope": {"allow": ["README.md", ".exo/**"], "deny": []},
            "checks": [],
        },
    )


def _submit_read_intent(repo: Path, intent_id: str) -> str:
    api = KernelSyscalls(repo, actor="agent:submitter")
    return api.submit(
        {
            "intent_id": intent_id,
            "intent": "Read README",
            "topic": "repo:default",
            "ttl_hours": 1,
            "scope": {"allow": ["README.md"], "deny": []},
            "action": {"kind": "read_file", "target": "README.md", "params": {}, "mode": "execute"},
            "max_attempts": 1,
        }
    )


def test_session_start_writes_bootstrap_and_active_state(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    _seed_ticket(repo)
    tickets_mod.acquire_lock(repo, "TICKET-111", owner="agent:codex", role="developer", duration_hours=1)

    manager = AgentSessionManager(repo, actor="agent:codex")
    out = manager.start(
        ticket_id="TICKET-111",
        vendor="openai",
        model="gpt-5",
        context_window_tokens=200000,
        task="execute ticket work",
    )

    assert out["reused"] is False
    assert out["session"]["ticket_id"] == "TICKET-111"
    assert out["session"]["vendor"] == "openai"
    assert out["session"]["model"] == "gpt-5"
    assert "bootstrap_prompt" in out

    active = manager.get_active()
    assert active is not None
    assert active["status"] == "active"

    bootstrap_path = repo / out["bootstrap_path"]
    assert bootstrap_path.exists()
    content = bootstrap_path.read_text(encoding="utf-8")
    assert "Exo Agent Session Bootstrap" in content
    assert "TICKET-111" in content


def test_session_finish_writes_memento_updates_ticket_and_releases_lock(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    _seed_ticket(repo)
    tickets_mod.acquire_lock(repo, "TICKET-111", owner="agent:codex", role="developer", duration_hours=1)

    manager = AgentSessionManager(repo, actor="agent:codex")
    _ = manager.start(ticket_id="TICKET-111", vendor="openai", model="gpt-5")

    result = manager.finish(
        summary="Completed core implementation and prepared handoff.",
        set_status="review",
        artifacts=["artifact://unit-tests"],
        next_step="Peer review and merge",
    )

    assert result["ticket_status"] == "review"
    assert result["released_lock"] is True
    assert (repo / result["memento_path"]).exists()
    assert manager.get_active() is None
    assert tickets_mod.load_lock(repo) is None

    updated_ticket = tickets_mod.load_ticket(repo, "TICKET-111")
    assert updated_ticket["status"] == "review"

    index_path = repo / result["session_index_path"]
    assert index_path.exists()
    lines = [line.strip() for line in index_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) >= 1


def test_session_finish_skip_check_requires_break_glass_reason(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    _seed_ticket(repo)
    tickets_mod.acquire_lock(repo, "TICKET-111", owner="agent:codex", role="developer", duration_hours=1)

    manager = AgentSessionManager(repo, actor="agent:codex")
    _ = manager.start(ticket_id="TICKET-111")

    with pytest.raises(ExoError) as err:
        manager.finish(summary="done", set_status="review", skip_check=True)
    assert err.value.code == "SESSION_BREAK_GLASS_REQUIRED"


def test_worker_require_session_gate(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    _seed_ticket(repo, ticket_id="TICKET-112")
    tickets_mod.acquire_lock(repo, "TICKET-112", owner="agent:worker", role="developer", duration_hours=1)
    _submit_read_intent(repo, "INT-SESSION-001")

    worker = DistributedWorker(repo, actor="agent:worker", use_cursor=False, require_session=True)
    with pytest.raises(ExoError) as err:
        worker.poll_once(persist_cursor=False)
    assert err.value.code == "SESSION_NOT_ACTIVE"

    manager = AgentSessionManager(repo, actor="agent:worker")
    _ = manager.start(ticket_id="TICKET-112", vendor="anthropic", model="claude-code")
    polled = worker.poll_once(persist_cursor=False)
    assert polled["processed_count"] == 1

    execution_result_rows = ledger_mod.read_records(repo, record_type="ExecutionResult", intent_id="INT-SESSION-001", limit=10)
    assert len(execution_result_rows) == 1


def test_session_suspend_snapshots_and_releases_lock(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    _seed_ticket(repo)
    tickets_mod.acquire_lock(repo, "TICKET-111", owner="agent:codex", role="developer", duration_hours=1)

    manager = AgentSessionManager(repo, actor="agent:codex")
    _ = manager.start(ticket_id="TICKET-111", vendor="openai", model="gpt-5", task="implement feature")

    result = manager.suspend(reason="Context window exhausted, handing off.")

    assert result["ticket_id"] == "TICKET-111"
    assert result["released_lock"] is True
    assert result["previous_ticket_status"] == "active"
    assert "suspended_path" in result

    assert manager.get_active() is None
    assert tickets_mod.load_lock(repo) is None

    ticket = tickets_mod.load_ticket(repo, "TICKET-111")
    assert ticket["status"] == "paused"

    suspended_path = repo / result["suspended_path"]
    assert suspended_path.exists()
    suspended_data = json.loads(suspended_path.read_text(encoding="utf-8"))
    assert suspended_data["status"] == "suspended"
    assert suspended_data["suspend_reason"] == "Context window exhausted, handing off."


def test_session_resume_restores_active_state_and_reacquires_lock(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    _seed_ticket(repo)
    tickets_mod.acquire_lock(repo, "TICKET-111", owner="agent:codex", role="developer", duration_hours=1)

    manager = AgentSessionManager(repo, actor="agent:codex")
    start_out = manager.start(ticket_id="TICKET-111", vendor="anthropic", model="claude-code", task="build feature")
    session_id = start_out["session"]["session_id"]

    manager.suspend(reason="Rate limited, will resume later.")

    assert manager.get_active() is None
    assert tickets_mod.load_lock(repo) is None
    assert tickets_mod.load_ticket(repo, "TICKET-111")["status"] == "paused"

    resume_out = manager.resume()

    assert resume_out["ticket_id"] == "TICKET-111"
    assert resume_out["acquired_lock"] is True
    assert resume_out["session"]["session_id"] == session_id
    assert resume_out["session"]["status"] == "active"

    active = manager.get_active()
    assert active is not None
    assert active["status"] == "active"
    assert active["resumed_at"] is not None

    assert tickets_mod.load_ticket(repo, "TICKET-111")["status"] == "active"
    assert tickets_mod.load_lock(repo) is not None

    bootstrap_path = repo / resume_out["bootstrap_path"]
    assert bootstrap_path.exists()
    content = bootstrap_path.read_text(encoding="utf-8")
    assert "Resumed" in content
    assert "Rate limited" in content

    suspended_path = repo / ".exo/memory/suspended/agent-codex.suspended.json"
    assert not suspended_path.exists()


def test_session_suspend_requires_active_session(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    manager = AgentSessionManager(repo, actor="agent:codex")

    with pytest.raises(ExoError) as err:
        manager.suspend(reason="no session")
    assert err.value.code == "SESSION_NOT_ACTIVE"


def test_session_resume_requires_suspended_session(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    manager = AgentSessionManager(repo, actor="agent:codex")

    with pytest.raises(ExoError) as err:
        manager.resume()
    assert err.value.code == "SESSION_NOT_SUSPENDED"


def test_session_resume_blocks_when_already_active(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    _seed_ticket(repo)
    tickets_mod.acquire_lock(repo, "TICKET-111", owner="agent:codex", role="developer", duration_hours=1)

    manager = AgentSessionManager(repo, actor="agent:codex")
    manager.start(ticket_id="TICKET-111", vendor="openai", model="gpt-5")
    manager.suspend(reason="pausing")

    tickets_mod.acquire_lock(repo, "TICKET-111", owner="agent:codex", role="developer", duration_hours=1)
    manager.start(ticket_id="TICKET-111", vendor="openai", model="gpt-5")

    with pytest.raises(ExoError) as err:
        manager.resume()
    assert err.value.code == "SESSION_ALREADY_ACTIVE"


def test_session_suspend_then_finish_after_resume(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    _seed_ticket(repo)
    tickets_mod.acquire_lock(repo, "TICKET-111", owner="agent:codex", role="developer", duration_hours=1)

    manager = AgentSessionManager(repo, actor="agent:codex")
    manager.start(ticket_id="TICKET-111", vendor="anthropic", model="claude-code")

    manager.suspend(reason="Context limit reached.")
    manager.resume()

    result = manager.finish(
        summary="Completed after resuming from suspension.",
        set_status="review",
    )

    assert result["ticket_status"] == "review"
    assert result["released_lock"] is True
    assert manager.get_active() is None
    assert tickets_mod.load_ticket(repo, "TICKET-111")["status"] == "review"


# --- Crash recovery / stale session tests ---


def _write_stale_active_session(repo: Path, actor: str, ticket_id: str, hours_ago: float = 72) -> Path:
    """Simulate a crashed process by writing an active session file with old timestamps."""
    cache_dir = repo / ".exo/cache/sessions"
    cache_dir.mkdir(parents=True, exist_ok=True)
    token = actor.replace(":", "-").replace(" ", "-")
    path = cache_dir / f"{token}.active.json"
    started_at = (datetime.now().astimezone() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    payload = {
        "session_id": f"SES-STALE-{token}",
        "status": "active",
        "actor": actor,
        "vendor": "test",
        "model": "test",
        "ticket_id": ticket_id,
        "started_at": started_at,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def test_scan_sessions_detects_stale_active(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    _seed_ticket(repo)
    _write_stale_active_session(repo, "agent:crashed", "TICKET-111", hours_ago=72)

    result = scan_sessions(repo, stale_hours=48)

    assert len(result["active_sessions"]) == 1
    assert len(result["stale_sessions"]) == 1
    assert result["stale_sessions"][0]["actor"] == "agent:crashed"
    assert result["stale_sessions"][0]["stale"] is True
    assert result["stale_sessions"][0]["age_hours"] >= 48


def test_cleanup_removes_stale_sessions(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    _seed_ticket(repo)
    tickets_mod.acquire_lock(repo, "TICKET-111", owner="agent:crashed", role="developer", duration_hours=1)

    stale_path = _write_stale_active_session(repo, "agent:crashed", "TICKET-111", hours_ago=72)
    assert stale_path.exists()

    result = cleanup_sessions(repo, stale_hours=48, actor="system")

    assert result["removed_count"] == 1
    assert not stale_path.exists()

    ticket = tickets_mod.load_ticket(repo, "TICKET-111")
    assert ticket["status"] == "todo"


def test_cleanup_skips_fresh_sessions(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    _seed_ticket(repo)
    tickets_mod.acquire_lock(repo, "TICKET-111", owner="agent:codex", role="developer", duration_hours=1)

    manager = AgentSessionManager(repo, actor="agent:codex")
    manager.start(ticket_id="TICKET-111", vendor="test", model="test")

    result = cleanup_sessions(repo, stale_hours=48, actor="system")

    assert result["removed_count"] == 0
    assert manager.get_active() is not None


def test_cleanup_force_removes_all_sessions(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    _seed_ticket(repo)
    tickets_mod.acquire_lock(repo, "TICKET-111", owner="agent:codex", role="developer", duration_hours=1)

    manager = AgentSessionManager(repo, actor="agent:codex")
    manager.start(ticket_id="TICKET-111", vendor="test", model="test")

    result = cleanup_sessions(repo, stale_hours=48, force=True, actor="system")

    assert result["removed_count"] == 1
    assert result["force"] is True
    assert manager.get_active() is None


def test_cleanup_releases_orphaned_lock(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    _seed_ticket(repo)
    tickets_mod.acquire_lock(repo, "TICKET-111", owner="agent:crashed", role="developer", duration_hours=1)
    _write_stale_active_session(repo, "agent:crashed", "TICKET-111", hours_ago=72)

    result = cleanup_sessions(repo, stale_hours=48, release_lock=True, actor="system")

    assert result["removed_count"] == 1
    assert result["released_lock"] is True
    assert tickets_mod.load_lock(repo) is None


def test_stale_session_evicted_on_start(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    _seed_ticket(repo)
    tickets_mod.acquire_lock(repo, "TICKET-111", owner="agent:codex", role="developer", duration_hours=1)

    _write_stale_active_session(repo, "agent:codex", "TICKET-111", hours_ago=72)

    manager = AgentSessionManager(repo, actor="agent:codex")
    out = manager.start(ticket_id="TICKET-111", vendor="test", model="test")

    assert out["reused"] is False
    assert out["session"]["status"] == "active"
