# @feature:ledger
from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from .errors import ExoError
from .types import LedgerRef
from .utils import ensure_dir, now_iso, relative_posix, sha256_text
from .version import KERNEL_NAME, KERNEL_VERSION

LEDGER_LOG_PATH = Path(".exo/logs/ledger.log.jsonl")
LEDGER_HEADS_PATH = Path(".exo/logs/heads.json")
LEDGER_INTENT_LOCK_PATH = Path(".exo/logs/ledger.intent.lock")

try:
    import fcntl as _fcntl
except Exception:  # pragma: no cover - platform-specific
    _fcntl = None


RECORD_TYPES = {
    "IntentSubmitted",
    "DecisionRecorded",
    "ExecutionBegun",
    "ExecutionResult",
    "Escalated",
    "Acked",
}

DECISION_OUTCOMES = {"ALLOW", "DENY", "ESCALATE", "SANDBOX"}
EFFECT_STATUSES = {"OK", "FAIL", "RETRYABLE_FAIL", "CANCELED"}


def _stable_json(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def payload_hash(payload: Any) -> str:
    return sha256_text(_stable_json(payload))


def _is_non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_record_shape(record: dict[str, Any]) -> None:
    record_type = record.get("record_type")
    if not _is_non_empty_str(record_type):
        raise ExoError(code="LEDGER_RECORD_INVALID", message="record_type is required")
    if str(record_type) not in RECORD_TYPES:
        raise ExoError(
            code="LEDGER_RECORD_INVALID",
            message=f"Unsupported record_type: {record_type}",
        )

    rtype = str(record_type)
    if rtype == "IntentSubmitted":
        required = ["intent_id", "actor_id", "topic_id", "payload_hash", "parents"]
        for key in required:
            if key == "parents":
                if not isinstance(record.get(key), list):
                    raise ExoError(code="LEDGER_RECORD_INVALID", message="IntentSubmitted.parents must be a list")
            elif not _is_non_empty_str(record.get(key)):
                raise ExoError(code="LEDGER_RECORD_INVALID", message=f"IntentSubmitted.{key} is required")
        return

    if rtype == "DecisionRecorded":
        required = ["decision_id", "intent_id", "policy_version", "outcome", "reasons_hash"]
        for key in required:
            if not _is_non_empty_str(record.get(key)):
                raise ExoError(code="LEDGER_RECORD_INVALID", message=f"DecisionRecorded.{key} is required")
        outcome = str(record.get("outcome"))
        if outcome not in DECISION_OUTCOMES:
            raise ExoError(
                code="LEDGER_RECORD_INVALID",
                message=f"DecisionRecorded.outcome must be one of {sorted(DECISION_OUTCOMES)}",
            )
        return

    if rtype == "ExecutionBegun":
        required = ["effect_id", "decision_id", "executor_ref", "idempotency_key"]
        for key in required:
            if not _is_non_empty_str(record.get(key)):
                raise ExoError(code="LEDGER_RECORD_INVALID", message=f"ExecutionBegun.{key} is required")
        return

    if rtype == "ExecutionResult":
        required = ["effect_id", "status", "artifact_refs"]
        for key in required:
            if key == "artifact_refs":
                if not isinstance(record.get(key), list):
                    raise ExoError(code="LEDGER_RECORD_INVALID", message="ExecutionResult.artifact_refs must be a list")
            elif not _is_non_empty_str(record.get(key)):
                raise ExoError(code="LEDGER_RECORD_INVALID", message=f"ExecutionResult.{key} is required")
        status = str(record.get("status"))
        if status not in EFFECT_STATUSES:
            raise ExoError(
                code="LEDGER_RECORD_INVALID",
                message=f"ExecutionResult.status must be one of {sorted(EFFECT_STATUSES)}",
            )
        return

    if rtype == "Escalated":
        required = ["intent_id", "escalation_kind", "context_refs"]
        for key in required:
            if key == "context_refs":
                if not isinstance(record.get(key), list):
                    raise ExoError(code="LEDGER_RECORD_INVALID", message="Escalated.context_refs must be a list")
            elif not _is_non_empty_str(record.get(key)):
                raise ExoError(code="LEDGER_RECORD_INVALID", message=f"Escalated.{key} is required")
        return

    if rtype == "Acked":
        required = ["actor_id", "ref_id"]
        for key in required:
            if not _is_non_empty_str(record.get(key)):
                raise ExoError(code="LEDGER_RECORD_INVALID", message=f"Acked.{key} is required")
        return


def _extract_ref_id(record: dict[str, Any]) -> str | None:
    record_type = str(record.get("record_type", ""))
    if record_type == "DecisionRecorded":
        keys = ["decision_id", "intent_id"]
    elif record_type in {"ExecutionBegun", "ExecutionResult"}:
        keys = ["effect_id", "decision_id", "intent_id"]
    elif record_type == "Acked":
        keys = ["ref_id"]
    else:
        keys = ["intent_id", "decision_id", "effect_id", "ref_id"]

    for key in keys:
        value = record.get(key)
        if _is_non_empty_str(value):
            return str(value)
    return None


def _make_ref(repo: Path, line_no: int, payload: dict[str, Any]) -> LedgerRef:
    encoded = _stable_json(payload)
    return LedgerRef(
        log_path=relative_posix(repo / LEDGER_LOG_PATH, repo),
        line=line_no,
        record_hash=sha256_text(encoded),
        ts=str(payload.get("ts", "")),
        record_type=str(payload.get("record_type", "")),
        ref_id=_extract_ref_id(payload),
    )


def _iter_records_with_meta(repo: Path) -> list[tuple[int, dict[str, Any]]]:
    log_path = repo / LEDGER_LOG_PATH
    if not log_path.exists():
        return []

    out: list[tuple[int, dict[str, Any]]] = []
    with log_path.open("r", encoding="utf-8") as handle:
        for idx, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            out.append((idx, item))
    return out


def _line_cursor(line_no: int) -> str:
    return f"line:{line_no}"


def _parse_line_cursor(cursor: str | None) -> int | None:
    if not _is_non_empty_str(cursor):
        return None
    prefix = "line:"
    if not str(cursor).startswith(prefix):
        return None
    raw = str(cursor)[len(prefix) :]
    if not raw.isdigit():
        return None
    line_no = int(raw)
    return line_no if line_no > 0 else None


def _load_heads(repo: Path) -> dict[str, Any]:
    path = repo / LEDGER_HEADS_PATH
    if not path.exists():
        return {"version": 1, "topics": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "topics": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "topics": {}}
    topics = payload.get("topics")
    if not isinstance(topics, dict):
        payload["topics"] = {}
    return payload


def _write_heads(repo: Path, heads: dict[str, Any]) -> None:
    path = repo / LEDGER_HEADS_PATH
    ensure_dir(path.parent)
    payload = dict(heads)
    payload["version"] = 1
    if not isinstance(payload.get("topics"), dict):
        payload["topics"] = {}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def _update_topic_head_from_ref(repo: Path, *, topic_id: str, ref: LedgerRef) -> None:
    if not _is_non_empty_str(topic_id):
        return
    heads = _load_heads(repo)
    topics = heads.setdefault("topics", {})
    if not isinstance(topics, dict):
        topics = {}
        heads["topics"] = topics
    topics[topic_id] = {
        "ref": _line_cursor(ref.line),
        "record_type": ref.record_type,
        "ref_id": ref.ref_id,
        "record_hash": ref.record_hash,
        "ts": ref.ts,
    }
    _write_heads(repo, heads)


@contextlib.contextmanager
def _intent_submit_lock(repo: Path):
    lock_path = repo / LEDGER_INTENT_LOCK_PATH
    ensure_dir(lock_path.parent)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        yield
    finally:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        handle.close()


def _intent_id_from_cursor(repo: Path, cursor: str | None) -> str | None:
    line_no = _parse_line_cursor(cursor)
    if not isinstance(line_no, int):
        return None
    for line, item in _iter_records_with_meta(repo):
        if line != line_no:
            continue
        if item.get("record_type") != "IntentSubmitted":
            return None
        intent_id = item.get("intent_id")
        if _is_non_empty_str(intent_id):
            return str(intent_id)
        return None
    return None


def _current_topic_head(repo: Path, topic_id: str) -> str | None:
    heads = _load_heads(repo)
    topics = heads.get("topics")
    if isinstance(topics, dict):
        entry = topics.get(topic_id)
        if isinstance(entry, dict):
            ref = entry.get("ref")
            if _is_non_empty_str(ref):
                return str(ref)
    return None


def _find_latest_topic_head(repo: Path, topic_id: str) -> LedgerRef | None:
    for line_no, item in reversed(_iter_records_with_meta(repo)):
        if item.get("record_type") != "IntentSubmitted":
            continue
        if str(item.get("topic_id")) != topic_id:
            continue
        return _make_ref(repo, line_no, item)
    return None


def _build_topic_index(records: list[tuple[int, dict[str, Any]]]) -> dict[str, str]:
    index: dict[str, str] = {}
    for _line_no, item in records:
        if item.get("record_type") != "IntentSubmitted":
            continue
        intent_id = item.get("intent_id")
        topic_id = item.get("topic_id")
        if _is_non_empty_str(intent_id) and _is_non_empty_str(topic_id):
            index[str(intent_id)] = str(topic_id)
    return index


def _infer_related_intent_id(
    record: dict[str, Any],
    *,
    decision_to_intent: dict[str, str],
    effect_to_decision: dict[str, str],
) -> str | None:
    record_type = str(record.get("record_type", ""))
    if record_type in {"IntentSubmitted", "DecisionRecorded", "Escalated"}:
        value = record.get("intent_id")
        if _is_non_empty_str(value):
            return str(value)
        return None
    if record_type == "ExecutionBegun":
        decision_id = record.get("decision_id")
        if _is_non_empty_str(decision_id):
            return decision_to_intent.get(str(decision_id))
        return None
    if record_type == "ExecutionResult":
        effect_id = record.get("effect_id")
        if _is_non_empty_str(effect_id):
            decision_id = effect_to_decision.get(str(effect_id))
            if _is_non_empty_str(decision_id):
                return decision_to_intent.get(str(decision_id))
        return None
    if record_type == "Acked":
        ref_id = record.get("ref_id")
        if not _is_non_empty_str(ref_id):
            return None
        ref_value = str(ref_id)
        if ref_value in decision_to_intent:
            return decision_to_intent[ref_value]
        if ref_value in effect_to_decision:
            decision_id = effect_to_decision[ref_value]
            if decision_id in decision_to_intent:
                return decision_to_intent[decision_id]
        return None
    return None


def _record_exists_for_ref(records: list[tuple[int, dict[str, Any]]], ref_id: str) -> bool:
    line_ref = _parse_line_cursor(ref_id)
    if isinstance(line_ref, int):
        return any(line_no == line_ref for line_no, _ in records)
    return any(_extract_ref_id(item) == ref_id for _, item in records)


def _filtered_records_with_meta(
    records: list[tuple[int, dict[str, Any]]],
    *,
    record_type: str | None = None,
    intent_id: str | None = None,
    topic_id: str | None = None,
    ref_id: str | None = None,
    start_line: int = 1,
    limit: int = 200,
) -> list[tuple[int, dict[str, Any]]]:
    out: list[tuple[int, dict[str, Any]]] = []
    decision_to_intent: dict[str, str] = {}
    effect_to_decision: dict[str, str] = {}
    topic_index = _build_topic_index(records)

    for line_no, item in records:
        if item.get("record_type") == "DecisionRecorded":
            decision_value = item.get("decision_id")
            intent_value = item.get("intent_id")
            if _is_non_empty_str(decision_value) and _is_non_empty_str(intent_value):
                decision_to_intent[str(decision_value)] = str(intent_value)
        elif item.get("record_type") == "ExecutionBegun":
            effect_value = item.get("effect_id")
            decision_value = item.get("decision_id")
            if _is_non_empty_str(effect_value) and _is_non_empty_str(decision_value):
                effect_to_decision[str(effect_value)] = str(decision_value)

        related_intent_id = _infer_related_intent_id(
            item,
            decision_to_intent=decision_to_intent,
            effect_to_decision=effect_to_decision,
        )

        if line_no < start_line:
            continue
        if record_type and str(item.get("record_type")) != record_type:
            continue
        if intent_id and related_intent_id != intent_id:
            continue
        if ref_id and _extract_ref_id(item) != ref_id:
            continue
        if topic_id:
            if not _is_non_empty_str(related_intent_id):
                continue
            if topic_index.get(str(related_intent_id)) != topic_id:
                continue
        out.append((line_no, item))
        if len(out) >= limit:
            break

    return out


def append_record(root: Path | str, record: dict[str, Any]) -> LedgerRef:
    repo = Path(root).resolve()
    log_path = repo / LEDGER_LOG_PATH
    ensure_dir(log_path.parent)

    payload = dict(record)
    if not _is_non_empty_str(payload.get("ts")):
        payload["ts"] = now_iso()
    if not _is_non_empty_str(payload.get("kernel_name")):
        payload["kernel_name"] = KERNEL_NAME
    if not _is_non_empty_str(payload.get("kernel_version")):
        payload["kernel_version"] = KERNEL_VERSION

    _validate_record_shape(payload)

    encoded = _stable_json(payload)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.write("\n")

    with log_path.open("r", encoding="utf-8") as handle:
        line_no = sum(1 for _ in handle)

    return _make_ref(repo, line_no, payload)


def read_records(
    root: Path | str,
    *,
    record_type: str | None = None,
    intent_id: str | None = None,
    topic_id: str | None = None,
    ref_id: str | None = None,
    since_cursor: str | None = None,
    since_line: int = 1,
    limit: int = 200,
) -> list[dict[str, Any]]:
    repo = Path(root).resolve()
    records = _iter_records_with_meta(repo)
    if not records:
        return []

    cursor_line = _parse_line_cursor(since_cursor)
    start_line = since_line if since_line >= 1 else 1
    if isinstance(cursor_line, int):
        start_line = max(start_line, cursor_line + 1)
    if limit < 1:
        return []

    filtered = _filtered_records_with_meta(
        records,
        record_type=record_type,
        intent_id=intent_id,
        topic_id=topic_id,
        ref_id=ref_id,
        start_line=start_line,
        limit=limit,
    )
    return [item for _, item in filtered]


def subscribe(
    root: Path | str,
    *,
    topic_id: str | None = None,
    since_cursor: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    repo = Path(root).resolve()
    records = _iter_records_with_meta(repo)
    if limit < 1:
        limit = 1

    cursor_line = _parse_line_cursor(since_cursor)
    start_line = (cursor_line + 1) if isinstance(cursor_line, int) else 1
    filtered = _filtered_records_with_meta(
        records,
        topic_id=topic_id,
        start_line=start_line,
        limit=limit,
    )

    events = [
        {
            "cursor": _line_cursor(line_no),
            "record": item,
        }
        for line_no, item in filtered
    ]
    next_cursor = events[-1]["cursor"] if events else (since_cursor if _is_non_empty_str(since_cursor) else None)
    return {
        "events": events,
        "next_cursor": next_cursor,
        "count": len(events),
        "topic_id": topic_id,
    }


def head(root: Path | str, topic_id: str) -> str | None:
    repo = Path(root).resolve()
    current = _current_topic_head(repo, topic_id)
    if current:
        return current

    latest = _find_latest_topic_head(repo, topic_id)
    if not latest:
        return None
    _update_topic_head_from_ref(repo, topic_id=topic_id, ref=latest)
    return _line_cursor(latest.line)


def cas_head(root: Path | str, topic_id: str, expected_ref: str | None, new_ref: str | None) -> dict[str, Any]:
    repo = Path(root).resolve()
    with _intent_submit_lock(repo):
        return _cas_head_inner(repo, topic_id, expected_ref, new_ref)


def _cas_head_inner(repo: Path, topic_id: str, expected_ref: str | None, new_ref: str | None) -> dict[str, Any]:
    current = head(repo, topic_id)
    if current != expected_ref:
        return {"ok": False, "head": current}

    heads = _load_heads(repo)
    topics = heads.setdefault("topics", {})
    if not isinstance(topics, dict):
        topics = {}
        heads["topics"] = topics

    if _is_non_empty_str(new_ref):
        topics[topic_id] = {
            "ref": str(new_ref),
            "record_type": "HeadPointer",
            "ref_id": None,
            "record_hash": "",
            "ts": now_iso(),
        }
        next_head = str(new_ref)
    else:
        topics.pop(topic_id, None)
        next_head = None

    _write_heads(repo, heads)
    return {"ok": True, "head": next_head}


def cas_head_retry(
    root: Path | str,
    topic_id: str,
    expected_ref: str | None,
    new_ref: str | None,
    *,
    max_attempts: int = 1,
) -> dict[str, Any]:
    attempts_limit = max(int(max_attempts), 1)
    attempt = 0
    current_expected = expected_ref
    history: list[dict[str, Any]] = []

    while attempt < attempts_limit:
        attempt += 1
        result = cas_head(root, topic_id, current_expected, new_ref)
        history.append(
            {
                "attempt": attempt,
                "expected_ref": current_expected,
                "actual_ref": result.get("head"),
                "ok": bool(result.get("ok")),
            }
        )
        if bool(result.get("ok")):
            return {
                "ok": True,
                "head": result.get("head"),
                "attempts": attempt,
                "history": history,
            }

        current_expected = result.get("head")

    return {
        "ok": False,
        "head": current_expected,
        "attempts": attempt,
        "history": history,
        "retry": {
            "retryable": True,
            "strategy": "refresh_head_and_retry_with_latest",
            "suggested_expected_ref": current_expected,
        },
    }


def intent_causal_order(root: Path | str, topic_id: str) -> list[str]:
    repo = Path(root).resolve()
    records = _iter_records_with_meta(repo)

    intents: dict[str, dict[str, Any]] = {}
    for line_no, item in records:
        if item.get("record_type") != "IntentSubmitted":
            continue
        if str(item.get("topic_id")) != topic_id:
            continue

        intent_id = item.get("intent_id")
        if not _is_non_empty_str(intent_id):
            continue
        raw_parents = item.get("parents")
        parents = (
            [str(entry) for entry in raw_parents if _is_non_empty_str(entry)] if isinstance(raw_parents, list) else []
        )

        intents[str(intent_id)] = {
            "parents": parents,
            "line": line_no,
            "ts": str(item.get("ts", "")),
        }

    if not intents:
        return []

    indegree: dict[str, int] = {intent_id: 0 for intent_id in intents}
    children: dict[str, list[str]] = {intent_id: [] for intent_id in intents}
    for intent_id, meta in intents.items():
        for parent_id in meta["parents"]:
            if parent_id not in intents:
                continue
            indegree[intent_id] += 1
            children[parent_id].append(intent_id)

    def sort_key(intent_id: str) -> tuple[str, int, str]:
        meta = intents[intent_id]
        return (str(meta["ts"]), int(meta["line"]), intent_id)

    ready = sorted([intent_id for intent_id, degree in indegree.items() if degree == 0], key=sort_key)
    ordered: list[str] = []

    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for child_id in children[current]:
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                ready.append(child_id)
        ready.sort(key=sort_key)

    if len(ordered) != len(intents):
        unresolved = sorted([intent_id for intent_id, degree in indegree.items() if degree > 0])
        raise ExoError(
            code="INTENT_CAUSAL_CYCLE",
            message=f"Intent parent cycle detected in topic {topic_id}",
            details={"topic_id": topic_id, "unresolved": unresolved},
            blocked=True,
        )

    return ordered


def intent_submitted(
    root: Path | str,
    *,
    intent_id: str,
    actor_id: str,
    topic_id: str,
    payload_hash_value: str,
    parents: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    expected_head: str | None = None,
    max_head_attempts: int = 1,
) -> LedgerRef:
    repo = Path(root).resolve()
    attempts_limit = max(int(max_head_attempts), 1)
    attempt = 0
    current_expected = expected_head

    while attempt < attempts_limit:
        attempt += 1
        with _intent_submit_lock(repo):
            current_head = head(repo, topic_id)
            if current_expected is not None and current_head != current_expected:
                if attempt >= attempts_limit:
                    raise ExoError(
                        code="CAS_HEAD_CONFLICT",
                        message=f"Intent submission CAS conflict for topic {topic_id}",
                        details={
                            "topic_id": topic_id,
                            "expected_ref": current_expected,
                            "actual_ref": current_head,
                            "attempts": attempt,
                            "retry": {
                                "retryable": True,
                                "strategy": "refresh_head_and_retry_with_latest",
                                "suggested_expected_ref": current_head,
                            },
                        },
                        blocked=True,
                    )
                current_expected = current_head
                continue

            resolved_parents: list[str] = []
            if isinstance(parents, list):
                resolved_parents = [str(item) for item in parents if _is_non_empty_str(item)]
            if not resolved_parents:
                parent_intent = _intent_id_from_cursor(repo, current_head)
                if _is_non_empty_str(parent_intent):
                    resolved_parents = [str(parent_intent)]

            payload_metadata = dict(metadata or {})
            head_meta_raw = payload_metadata.get("head")
            head_meta = head_meta_raw if isinstance(head_meta_raw, dict) else {}
            head_meta = dict(head_meta)
            head_meta.setdefault("expected_ref", expected_head)
            head_meta["actual_ref"] = current_head
            head_meta["attempt"] = attempt
            payload_metadata["head"] = head_meta

            ref = append_record(
                repo,
                {
                    "record_type": "IntentSubmitted",
                    "intent_id": intent_id,
                    "actor_id": actor_id,
                    "topic_id": topic_id,
                    "payload_hash": payload_hash_value,
                    "parents": resolved_parents,
                    "metadata": payload_metadata,
                },
            )
            _update_topic_head_from_ref(repo, topic_id=topic_id, ref=ref)
            return ref

    raise ExoError(
        code="CAS_HEAD_CONFLICT",
        message=f"Intent submission CAS conflict for topic {topic_id}",
        details={
            "topic_id": topic_id,
            "expected_ref": expected_head,
            "attempts": attempts_limit,
        },
        blocked=True,
    )


def decision_recorded(
    root: Path | str,
    *,
    decision_id: str,
    intent_id: str,
    policy_version: str,
    outcome: str,
    reasons_hash: str,
    reasons: list[str] | None = None,
    constraints: dict[str, Any] | None = None,
) -> LedgerRef:
    return append_record(
        root,
        {
            "record_type": "DecisionRecorded",
            "decision_id": decision_id,
            "intent_id": intent_id,
            "policy_version": policy_version,
            "outcome": outcome,
            "reasons_hash": reasons_hash,
            "reasons": reasons or [],
            "constraints": constraints or {},
        },
    )


def execution_begun(
    root: Path | str,
    *,
    effect_id: str,
    decision_id: str,
    executor_ref: str,
    idempotency_key: str,
) -> LedgerRef:
    repo = Path(root).resolve()
    records = _iter_records_with_meta(repo)

    matched_decisions = [
        (line, item)
        for line, item in records
        if item.get("record_type") == "DecisionRecorded" and str(item.get("decision_id")) == decision_id
    ]
    if not matched_decisions:
        raise ExoError(
            code="EXECUTION_DECISION_MISSING",
            message=f"Decision not found for execution begin: {decision_id}",
            blocked=True,
        )
    _, latest_decision = matched_decisions[-1]
    decision_outcome = str(latest_decision.get("outcome", ""))
    if decision_outcome not in {"ALLOW", "SANDBOX"}:
        raise ExoError(
            code="EXECUTION_DECISION_NOT_EXECUTABLE",
            message=f"Decision outcome {decision_outcome} is not executable",
            details={"decision_id": decision_id, "outcome": decision_outcome},
            blocked=True,
        )

    existing_effect = [
        (line, item)
        for line, item in records
        if item.get("record_type") == "ExecutionBegun" and str(item.get("effect_id")) == effect_id
    ]
    if existing_effect:
        line_no, item = existing_effect[-1]
        same = (
            str(item.get("decision_id")) == decision_id
            and str(item.get("executor_ref")) == executor_ref
            and str(item.get("idempotency_key")) == idempotency_key
        )
        if same:
            return _make_ref(repo, line_no, item)
        raise ExoError(
            code="EFFECT_ID_COLLISION",
            message=f"Effect already exists with different parameters: {effect_id}",
            details={"effect_id": effect_id},
            blocked=True,
        )

    existing_idem = [
        (line, item)
        for line, item in records
        if item.get("record_type") == "ExecutionBegun"
        and str(item.get("decision_id")) == decision_id
        and str(item.get("idempotency_key")) == idempotency_key
    ]
    if existing_idem:
        line_no, item = existing_idem[-1]
        existing_effect_id = str(item.get("effect_id", ""))
        if existing_effect_id == effect_id and str(item.get("executor_ref")) == executor_ref:
            return _make_ref(repo, line_no, item)
        raise ExoError(
            code="IDEMPOTENCY_KEY_COLLISION",
            message=("Idempotency key already used for this decision with different execution parameters"),
            details={
                "decision_id": decision_id,
                "idempotency_key": idempotency_key,
                "existing_effect_id": existing_effect_id,
            },
            blocked=True,
        )

    return append_record(
        repo,
        {
            "record_type": "ExecutionBegun",
            "effect_id": effect_id,
            "decision_id": decision_id,
            "executor_ref": executor_ref,
            "idempotency_key": idempotency_key,
        },
    )


def execution_result(
    root: Path | str,
    *,
    effect_id: str,
    status: str,
    artifact_refs: list[str] | None = None,
) -> LedgerRef:
    repo = Path(root).resolve()
    records = _iter_records_with_meta(repo)
    has_begin = any(
        item.get("record_type") == "ExecutionBegun" and str(item.get("effect_id")) == effect_id for _, item in records
    )
    if not has_begin:
        raise ExoError(
            code="EXECUTION_BEGIN_MISSING",
            message=f"ExecutionBegun is required before ExecutionResult: {effect_id}",
            details={"effect_id": effect_id},
            blocked=True,
        )

    existing_results = [
        (line, item)
        for line, item in records
        if item.get("record_type") == "ExecutionResult" and str(item.get("effect_id")) == effect_id
    ]
    if existing_results:
        line_no, item = existing_results[-1]
        existing_status = str(item.get("status", ""))
        existing_artifacts = item.get("artifact_refs")
        if not isinstance(existing_artifacts, list):
            existing_artifacts = []
        requested_artifacts = artifact_refs or []

        if existing_status == status and existing_artifacts == requested_artifacts:
            return _make_ref(repo, line_no, item)
        raise ExoError(
            code="EXECUTION_RESULT_IMMUTABLE",
            message=f"ExecutionResult is write-once for effect_id {effect_id}",
            details={
                "effect_id": effect_id,
                "existing_status": existing_status,
                "requested_status": status,
            },
            blocked=True,
        )

    return append_record(
        repo,
        {
            "record_type": "ExecutionResult",
            "effect_id": effect_id,
            "status": status,
            "artifact_refs": artifact_refs or [],
        },
    )


def escalated(
    root: Path | str,
    *,
    intent_id: str,
    escalation_kind: str,
    context_refs: list[str] | None = None,
) -> LedgerRef:
    return append_record(
        root,
        {
            "record_type": "Escalated",
            "intent_id": intent_id,
            "escalation_kind": escalation_kind,
            "context_refs": context_refs or [],
        },
    )


def acked(root: Path | str, *, actor_id: str, ref_id: str) -> LedgerRef:
    repo = Path(root).resolve()
    records = _iter_records_with_meta(repo)

    if not _record_exists_for_ref(records, ref_id):
        raise ExoError(
            code="ACK_REF_NOT_FOUND",
            message=f"Ack reference not found in ledger: {ref_id}",
            details={"ref_id": ref_id},
            blocked=True,
        )

    existing = [
        (line, item)
        for line, item in records
        if item.get("record_type") == "Acked"
        and str(item.get("actor_id")) == actor_id
        and str(item.get("ref_id")) == ref_id
    ]
    if existing:
        line_no, item = existing[-1]
        return _make_ref(repo, line_no, item)

    return append_record(
        repo,
        {
            "record_type": "Acked",
            "actor_id": actor_id,
            "ref_id": ref_id,
        },
    )


def ack_status(root: Path | str, *, ref_id: str, required: int = 1) -> dict[str, Any]:
    repo = Path(root).resolve()
    records = _iter_records_with_meta(repo)

    if not _record_exists_for_ref(records, ref_id):
        raise ExoError(
            code="ACK_REF_NOT_FOUND",
            message=f"Ack reference not found in ledger: {ref_id}",
            details={"ref_id": ref_id},
            blocked=True,
        )

    total = 0
    unique: list[str] = []
    seen: set[str] = set()
    for _line_no, item in records:
        if item.get("record_type") != "Acked":
            continue
        if str(item.get("ref_id")) != ref_id:
            continue
        total += 1
        actor = str(item.get("actor_id", "")).strip()
        if actor and actor not in seen:
            seen.add(actor)
            unique.append(actor)

    required_count = max(int(required), 1)
    unique_count = len(unique)
    return {
        "ref_id": ref_id,
        "required": required_count,
        "ack_count": total,
        "unique_ack_count": unique_count,
        "actors": unique,
        "satisfied": unique_count >= required_count,
    }
