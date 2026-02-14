from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from exo.control.syscalls import KernelSyscalls
from exo.kernel.errors import ExoError
from exo.kernel.utils import default_topic_id

from .models import AgentRun, OrchestratorTask, Workflow

Executor = Callable[[OrchestratorTask], dict[str, Any]]
_ALLOWED_RESULT_STATUSES = {"OK", "FAIL", "RETRYABLE_FAIL", "CANCELED"}


class Orchestrator:
    """Layer-3 orchestration entrypoint.

    All task execution is routed through kernel submit/check/begin/commit calls.
    """

    def __init__(self, root: Path | str, *, actor: str = "agent:orchestrator") -> None:
        self.root = Path(root).resolve()
        self.actor = actor
        self._syscalls = KernelSyscalls(self.root, actor=actor)
        self._default_topic = default_topic_id(self.root)

    def _load_decision(self, decision_id: str) -> dict[str, Any]:
        rows = self._syscalls.read(
            decision_id,
            {
                "typeFilter": "DecisionRecorded",
                "limit": 1,
            },
        )
        if not rows:
            raise ExoError(
                code="ORCHESTRATOR_DECISION_MISSING",
                message=f"Decision record not found: {decision_id}",
                details={"decision_id": decision_id},
                blocked=True,
            )
        return dict(rows[-1])

    def _normalize_executor_result(self, value: dict[str, Any] | None) -> dict[str, Any]:
        raw = value if isinstance(value, dict) else {}
        status = str(raw.get("status", "OK")).upper()
        if status not in _ALLOWED_RESULT_STATUSES:
            raise ExoError(
                code="ORCHESTRATOR_STATUS_INVALID",
                message=f"Executor status must be one of {sorted(_ALLOWED_RESULT_STATUSES)}",
                details={"status": status},
                blocked=True,
            )

        refs = raw.get("artifact_refs")
        artifact_refs = (
            [str(item) for item in refs if isinstance(item, str) and item.strip()] if isinstance(refs, list) else []
        )
        details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
        return {
            "status": status,
            "artifact_refs": artifact_refs,
            "details": details,
        }

    def run_task(self, task: OrchestratorTask, *, executor: Executor | None = None) -> dict[str, Any]:
        payload = task.to_submit_payload(self._default_topic)
        intent_id = self._syscalls.submit(payload)
        decision_id = self._syscalls.check(intent_id, context_refs=[])
        decision_row = self._load_decision(decision_id)
        outcome = str(decision_row.get("outcome", "")).upper()
        reasons = decision_row.get("reasons") if isinstance(decision_row.get("reasons"), list) else []

        if outcome != "ALLOW":
            raise ExoError(
                code="ORCHESTRATOR_DECISION_DENIED",
                message=f"Kernel denied orchestrator task {task.task_id}",
                details={
                    "task_id": task.task_id,
                    "intent_id": intent_id,
                    "decision_id": decision_id,
                    "outcome": outcome or "UNKNOWN",
                    "reasons": reasons,
                },
                blocked=True,
            )

        idem_key = f"{task.task_id}:{datetime.now().astimezone().strftime('%Y%m%d%H%M%S')}:{uuid4().hex[:8]}"
        effect_id = self._syscalls.begin(decision_id, executor_ref=self.actor, idem_key=idem_key)

        try:
            exec_raw = executor(task) if executor else {"status": "OK", "artifact_refs": [], "details": {}}
            exec_result = self._normalize_executor_result(exec_raw)
        except Exception as exc:  # noqa: BLE001
            self._syscalls.commit(effect_id, status="FAIL", artifact_refs=[])
            raise ExoError(
                code="ORCHESTRATOR_EXECUTOR_FAILED",
                message=f"Executor failed for task {task.task_id}: {exc}",
                details={
                    "task_id": task.task_id,
                    "intent_id": intent_id,
                    "decision_id": decision_id,
                    "effect_id": effect_id,
                },
                blocked=True,
            ) from exc

        self._syscalls.commit(effect_id, status=exec_result["status"], artifact_refs=exec_result["artifact_refs"])
        return {
            "ok": True,
            "task_id": task.task_id,
            "intent_id": intent_id,
            "decision_id": decision_id,
            "effect_id": effect_id,
            "decision": {
                "outcome": outcome,
                "reasons": reasons,
            },
            "result": exec_result,
        }

    def run_workflow(self, workflow: Workflow, *, executor: Executor | None = None) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        total = len(workflow.tasks)
        for task in workflow.tasks:
            try:
                results.append(self.run_task(task, executor=executor))
            except ExoError as err:
                results.append({"ok": False, "task_id": task.task_id, "error": err.to_dict()})
                if workflow.stop_on_error:
                    break

        completed = sum(1 for item in results if bool(item.get("ok")))
        ok = completed == total
        return {
            "ok": ok,
            "workflow_id": workflow.workflow_id,
            "completed_tasks": completed,
            "total_tasks": total,
            "results": results,
        }

    def run_agent(self, run: AgentRun, *, executor: Executor | None = None) -> dict[str, Any]:
        workflow_result = self.run_workflow(run.workflow, executor=executor)
        return {
            "agent_id": run.agent_id,
            **workflow_result,
        }
