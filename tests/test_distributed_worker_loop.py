from __future__ import annotations

import json
from pathlib import Path

from exo.control.syscalls import KernelSyscalls
from exo.kernel import governance as governance_mod
from exo.kernel import ledger as ledger_mod
from exo.orchestrator import DistributedWorker


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


def test_worker_poll_executes_pending_intent_once(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    intent_id = _submit_read_intent(repo, "INT-WORKER-001")

    worker = DistributedWorker(repo, actor="agent:worker-a", use_cursor=False)
    result = worker.poll_once(limit=50, persist_cursor=False)

    assert result["processed_count"] == 1
    assert result["failed_count"] == 0
    assert result["processed"][0]["intent_id"] == intent_id
    assert result["processed"][0]["status"] == "OK"

    decision_rows = ledger_mod.read_records(repo, record_type="DecisionRecorded", intent_id=intent_id, limit=10)
    execution_begun_rows = ledger_mod.read_records(repo, record_type="ExecutionBegun", intent_id=intent_id, limit=10)
    execution_result_rows = ledger_mod.read_records(repo, record_type="ExecutionResult", intent_id=intent_id, limit=10)
    assert len(decision_rows) == 1
    assert len(execution_begun_rows) == 1
    assert len(execution_result_rows) == 1


def test_worker_poll_skips_intent_when_already_executed(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    intent_id = _submit_read_intent(repo, "INT-WORKER-002")

    first_worker = DistributedWorker(repo, actor="agent:worker-a", use_cursor=False)
    first = first_worker.poll_once(limit=50, persist_cursor=False)
    assert first["processed_count"] == 1

    second_worker = DistributedWorker(repo, actor="agent:worker-b", use_cursor=False)
    second = second_worker.poll_once(limit=50, persist_cursor=False)

    reasons = {str(item.get("reason")) for item in second.get("skipped", []) if isinstance(item, dict)}
    assert second["processed_count"] == 0
    assert "already_executed" in reasons

    execution_begun_rows = ledger_mod.read_records(repo, record_type="ExecutionBegun", intent_id=intent_id, limit=10)
    execution_result_rows = ledger_mod.read_records(repo, record_type="ExecutionResult", intent_id=intent_id, limit=10)
    assert len(execution_begun_rows) == 1
    assert len(execution_result_rows) == 1


def test_syscall_check_reuses_existing_decision_for_same_intent(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    intent_id = _submit_read_intent(repo, "INT-WORKER-003")
    api = KernelSyscalls(repo, actor="agent:checker")

    first_decision_id = api.check(intent_id, context_refs=[])
    second_decision_id = api.check(intent_id, context_refs=[])

    assert first_decision_id == second_decision_id

    decision_rows = ledger_mod.read_records(repo, record_type="DecisionRecorded", intent_id=intent_id, limit=10)
    assert len(decision_rows) == 1
