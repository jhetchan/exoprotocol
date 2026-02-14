from __future__ import annotations

import json
from pathlib import Path

import pytest

from exo.kernel import governance as governance_mod
from exo.kernel import ledger as ledger_mod
from exo.kernel.errors import ExoError
from exo.orchestrator import Orchestrator, OrchestratorTask


def _policy_block(payload: dict[str, object]) -> str:
    return "```yaml exo-policy\n" + json.dumps(payload, ensure_ascii=True, indent=2) + "\n```\n"


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


def test_orchestrator_run_task_routes_through_kernel_checks(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    orchestrator = Orchestrator(repo, actor="agent:test")

    task = OrchestratorTask(
        task_id="TASK-001",
        intent="Write README",
        action_kind="write_file",
        target="README.md",
        scope_allow=["README.md"],
    )
    result = orchestrator.run_task(
        task,
        executor=lambda _task: {
            "status": "OK",
            "artifact_refs": ["artifact://task-001"],
            "details": {"executor": "unit"},
        },
    )

    assert result["ok"] is True
    assert result["decision"]["outcome"] == "ALLOW"
    assert result["intent_id"] == "TASK-001"

    decision_rows = ledger_mod.read_records(repo, record_type="DecisionRecorded", ref_id=result["decision_id"], limit=1)
    assert len(decision_rows) == 1
    assert decision_rows[0]["outcome"] == "ALLOW"

    execution_rows = ledger_mod.read_records(repo, record_type="ExecutionResult", ref_id=result["effect_id"], limit=1)
    assert len(execution_rows) == 1
    assert execution_rows[0]["status"] == "OK"


def test_orchestrator_blocks_denied_task_before_execution(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    orchestrator = Orchestrator(repo, actor="agent:test")

    denied_task = OrchestratorTask(
        task_id="TASK-002",
        intent="Mutate advisory memory",
        action_kind="write_file",
        target=".exo/memory/index.yaml",
        scope_allow=[".exo/**"],
    )

    with pytest.raises(ExoError) as denied_err:
        orchestrator.run_task(denied_task)

    assert denied_err.value.code == "ORCHESTRATOR_DECISION_DENIED"
    assert denied_err.value.details
    assert denied_err.value.details.get("outcome") == "DENY"

    begun_rows = ledger_mod.read_records(repo, record_type="ExecutionBegun", limit=10)
    assert begun_rows == []
