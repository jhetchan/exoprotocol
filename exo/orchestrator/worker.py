from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable

from exo.control.syscalls import KernelSyscalls
from exo.kernel.errors import ExoError

WorkerExecutor = Callable[[dict[str, Any]], dict[str, Any] | None]
_ALLOWED_RESULT_STATUSES = {"OK", "FAIL", "RETRYABLE_FAIL", "CANCELED"}


class DistributedWorker:
    """Ledger-driven worker for distributed Layer-3 execution."""

    def __init__(
        self,
        root: Path | str,
        *,
        actor: str = "agent:worker",
        topic_id: str | None = None,
        cursor_path: Path | str | None = None,
        use_cursor: bool = True,
    ) -> None:
        self.root = Path(root).resolve()
        self.actor = actor
        self.topic_id = topic_id or f"repo:{self.root.as_posix()}"
        self._syscalls = KernelSyscalls(self.root, actor=actor)
        self.use_cursor = use_cursor
        if cursor_path is not None:
            raw_cursor_path = Path(cursor_path)
            if not raw_cursor_path.is_absolute():
                raw_cursor_path = self.root / raw_cursor_path
            self.cursor_path = raw_cursor_path.resolve()
        else:
            self.cursor_path = self._default_cursor_path()

    def _default_cursor_path(self) -> Path:
        token = re.sub(r"[^A-Za-z0-9._-]+", "-", self.actor).strip(".-") or "worker"
        return self.root / ".exo" / "cache" / "orchestrator" / f"{token}.cursor"

    def _read_cursor(self) -> str | None:
        if not self.use_cursor:
            return None
        if not self.cursor_path.exists():
            return None
        value = self.cursor_path.read_text(encoding="utf-8").strip()
        return value or None

    def _write_cursor(self, cursor: str | None) -> None:
        if not self.use_cursor:
            return
        if not isinstance(cursor, str) or not cursor.strip():
            return
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_text(cursor.strip() + "\n", encoding="utf-8")

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
        artifact_refs = [str(item) for item in refs if isinstance(item, str) and item.strip()] if isinstance(refs, list) else []
        details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
        return {
            "status": status,
            "artifact_refs": artifact_refs,
            "details": details,
        }

    def _resolve_decision(self, intent_id: str) -> tuple[str, dict[str, Any], bool]:
        existing = self._syscalls.read(
            None,
            {
                "typeFilter": "DecisionRecorded",
                "intent_id": intent_id,
                "limit": 200,
            },
        )
        if existing:
            row = dict(existing[-1])
            decision_id = str(row.get("decision_id", "")).strip()
            if decision_id:
                return decision_id, row, False

        decision_id = self._syscalls.check(intent_id, context_refs=[])
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
                details={"intent_id": intent_id, "decision_id": decision_id},
                blocked=True,
            )
        return decision_id, dict(rows[-1]), True

    def _intent_has_result(self, intent_id: str) -> bool:
        rows = self._syscalls.read(
            None,
            {
                "typeFilter": "ExecutionResult",
                "intent_id": intent_id,
                "limit": 1,
            },
        )
        return bool(rows)

    def _build_request(self, *, intent: dict[str, Any], decision: dict[str, Any], cursor: str) -> dict[str, Any]:
        metadata = intent.get("metadata") if isinstance(intent.get("metadata"), dict) else {}
        action = metadata.get("action") if isinstance(metadata.get("action"), dict) else {}
        reasons = decision.get("reasons") if isinstance(decision.get("reasons"), list) else []
        return {
            "cursor": cursor,
            "intent_id": str(intent.get("intent_id", "")),
            "topic_id": str(intent.get("topic_id", "")),
            "intent": str(metadata.get("intent", "")),
            "action": {
                "kind": str(action.get("kind", "read_file")),
                "target": action.get("target"),
                "params": action.get("params") if isinstance(action.get("params"), dict) else {},
                "mode": str(action.get("mode", "execute")),
            },
            "metadata": metadata,
            "decision": {
                "decision_id": str(decision.get("decision_id", "")),
                "outcome": str(decision.get("outcome", "")).upper(),
                "reasons": [str(item) for item in reasons if isinstance(item, str)],
            },
        }

    def poll_once(
        self,
        *,
        executor: WorkerExecutor | None = None,
        since_cursor: str | None = None,
        limit: int = 100,
        persist_cursor: bool = True,
    ) -> dict[str, Any]:
        start_cursor = since_cursor if isinstance(since_cursor, str) and since_cursor.strip() else self._read_cursor()
        stream = self._syscalls.subscribe(topic_id=self.topic_id, since_cursor=start_cursor, limit=max(int(limit), 1))
        events = stream.get("events") if isinstance(stream.get("events"), list) else []
        next_cursor_raw = stream.get("next_cursor")
        next_cursor = str(next_cursor_raw) if isinstance(next_cursor_raw, str) and next_cursor_raw.strip() else start_cursor

        processed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []

        for item in events:
            if not isinstance(item, dict):
                continue
            cursor = str(item.get("cursor", ""))
            record = item.get("record") if isinstance(item.get("record"), dict) else {}
            if str(record.get("record_type")) != "IntentSubmitted":
                continue

            intent_id = str(record.get("intent_id", "")).strip()
            if not intent_id:
                failures.append(
                    {
                        "cursor": cursor,
                        "code": "ORCHESTRATOR_INTENT_ID_MISSING",
                        "message": "IntentSubmitted record missing intent_id",
                    }
                )
                continue

            if self._intent_has_result(intent_id):
                skipped.append(
                    {
                        "cursor": cursor,
                        "intent_id": intent_id,
                        "reason": "already_executed",
                    }
                )
                continue

            try:
                decision_id, decision_row, decision_created = self._resolve_decision(intent_id)
            except ExoError as err:
                failures.append(
                    {
                        "cursor": cursor,
                        "intent_id": intent_id,
                        "code": err.code,
                        "message": err.message,
                        "decision_created": False,
                    }
                )
                continue

            outcome = str(decision_row.get("outcome", "")).upper()
            if outcome not in {"ALLOW", "SANDBOX"}:
                skipped.append(
                    {
                        "cursor": cursor,
                        "intent_id": intent_id,
                        "decision_id": decision_id,
                        "decision_created": decision_created,
                        "reason": f"decision_{outcome.lower() if outcome else 'unknown'}",
                    }
                )
                continue

            idem_key = f"intent:{intent_id}:decision:{decision_id}"
            try:
                effect_id = self._syscalls.begin(decision_id, executor_ref=self.actor, idem_key=idem_key)
            except ExoError as err:
                if err.code in {"IDEMPOTENCY_KEY_COLLISION", "EFFECT_ID_COLLISION"}:
                    skipped.append(
                        {
                            "cursor": cursor,
                            "intent_id": intent_id,
                            "decision_id": decision_id,
                            "decision_created": decision_created,
                            "reason": "already_claimed",
                        }
                    )
                    continue
                failures.append(
                    {
                        "cursor": cursor,
                        "intent_id": intent_id,
                        "decision_id": decision_id,
                        "decision_created": decision_created,
                        "code": err.code,
                        "message": err.message,
                    }
                )
                continue

            request = self._build_request(intent=record, decision=decision_row, cursor=cursor)
            try:
                raw_result = (
                    executor(request)
                    if executor
                    else {
                        "status": "OK",
                        "artifact_refs": [f"intent://{intent_id}"],
                        "details": {"executor": "noop", "actor": self.actor},
                    }
                )
                result = self._normalize_executor_result(raw_result)
            except Exception as exc:  # noqa: BLE001
                try:
                    self._syscalls.commit(effect_id, status="FAIL", artifact_refs=[])
                except ExoError as commit_err:
                    failures.append(
                        {
                            "cursor": cursor,
                            "intent_id": intent_id,
                            "decision_id": decision_id,
                            "effect_id": effect_id,
                            "decision_created": decision_created,
                            "code": commit_err.code,
                            "message": commit_err.message,
                        }
                    )
                failures.append(
                    {
                        "cursor": cursor,
                        "intent_id": intent_id,
                        "decision_id": decision_id,
                        "effect_id": effect_id,
                        "decision_created": decision_created,
                        "code": "ORCHESTRATOR_EXECUTOR_FAILED",
                        "message": str(exc),
                    }
                )
                continue

            try:
                self._syscalls.commit(effect_id, status=result["status"], artifact_refs=result["artifact_refs"])
            except ExoError as err:
                failures.append(
                    {
                        "cursor": cursor,
                        "intent_id": intent_id,
                        "decision_id": decision_id,
                        "effect_id": effect_id,
                        "decision_created": decision_created,
                        "code": err.code,
                        "message": err.message,
                    }
                )
                continue
            processed.append(
                {
                    "cursor": cursor,
                    "intent_id": intent_id,
                    "decision_id": decision_id,
                    "effect_id": effect_id,
                    "decision_created": decision_created,
                    "status": result["status"],
                    "artifact_refs": result["artifact_refs"],
                    "details": result["details"],
                }
            )

        if persist_cursor:
            self._write_cursor(next_cursor)

        return {
            "topic_id": self.topic_id,
            "actor": self.actor,
            "cursor_file": str(self.cursor_path),
            "since_cursor": start_cursor,
            "next_cursor": next_cursor,
            "events_seen": len(events),
            "processed_count": len(processed),
            "skipped_count": len(skipped),
            "failed_count": len(failures),
            "processed": processed,
            "skipped": skipped,
            "failures": failures,
        }

    def run_loop(
        self,
        *,
        executor: WorkerExecutor | None = None,
        iterations: int | None = 1,
        sleep_seconds: float = 1.0,
        since_cursor: str | None = None,
        limit: int = 100,
        persist_cursor: bool = True,
        stop_when_idle: bool = False,
    ) -> dict[str, Any]:
        remaining = iterations if (iterations is None or iterations > 0) else None
        cursor = since_cursor if isinstance(since_cursor, str) and since_cursor.strip() else self._read_cursor()

        snapshots: list[dict[str, Any]] = []
        total_processed = 0
        total_skipped = 0
        total_failed = 0

        while True:
            snapshot = self.poll_once(
                executor=executor,
                since_cursor=cursor,
                limit=limit,
                persist_cursor=False,
            )
            snapshots.append(snapshot)
            total_processed += int(snapshot.get("processed_count", 0) or 0)
            total_skipped += int(snapshot.get("skipped_count", 0) or 0)
            total_failed += int(snapshot.get("failed_count", 0) or 0)

            cursor_next = snapshot.get("next_cursor")
            cursor = str(cursor_next) if isinstance(cursor_next, str) and cursor_next.strip() else cursor

            if remaining is not None:
                remaining -= 1
                if remaining <= 0:
                    break

            if stop_when_idle and int(snapshot.get("events_seen", 0) or 0) == 0:
                break

            if sleep_seconds > 0:
                time.sleep(float(sleep_seconds))

        if persist_cursor:
            self._write_cursor(cursor)

        return {
            "topic_id": self.topic_id,
            "actor": self.actor,
            "cursor_file": str(self.cursor_path),
            "iterations": len(snapshots),
            "since_cursor": since_cursor,
            "next_cursor": cursor,
            "processed_count": total_processed,
            "skipped_count": total_skipped,
            "failed_count": total_failed,
            "runs": snapshots,
        }
