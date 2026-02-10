from __future__ import annotations

import json
from pathlib import Path

import pytest

from exo.control.syscalls import KernelSyscalls
from exo.kernel import governance as governance_mod
from exo.kernel import ledger as ledger_mod
from exo.kernel import tickets as tickets_mod
from exo.kernel.errors import ExoError
from exo.orchestrator import AgentSessionManager, DistributedWorker


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
            "topic": f"repo:{repo.resolve().as_posix()}",
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
