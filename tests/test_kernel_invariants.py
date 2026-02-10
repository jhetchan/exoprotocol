from __future__ import annotations

import json
from pathlib import Path

import pytest

import exo.kernel as kernel_api
from exo.kernel import (
    append_audit,
    check_action,
    load_governance,
    mint_ticket,
    open_session,
    seal_result,
    validate_ticket,
    verify_governance,
)
from exo.kernel import governance as governance_mod
from exo.kernel import ledger as ledger_mod
from exo.kernel import tickets as tickets_mod
from exo.kernel.errors import ExoError
from exo.kernel.types import to_dict
from exo.kernel.version import KERNEL_NAME, KERNEL_VERSION
from exo.control.syscalls import KernelSyscalls
from exo.stdlib.engine import KernelEngine


def _policy_block(payload: dict[str, object]) -> str:
    return "```yaml exo-policy\n" + json.dumps(payload, ensure_ascii=True, indent=2) + "\n```\n"


def _bootstrap_repo(tmp_path: Path, *, require_lock: bool = True, kernel_deny: bool = True) -> Path:
    repo = tmp_path
    exo_dir = repo / ".exo"
    exo_dir.mkdir(parents=True, exist_ok=True)

    blocks = [
        _policy_block(
            {
                "id": "RULE-SEC-001",
                "type": "filesystem_deny",
                "patterns": ["**/.env*"],
                "actions": ["read", "write"],
                "message": "Secret deny",
            }
        )
    ]
    if require_lock:
        blocks.append(
            _policy_block(
                {
                    "id": "RULE-LOCK-001",
                    "type": "require_lock",
                    "message": "Lock required",
                }
            )
        )
    if kernel_deny:
        blocks.append(
            _policy_block(
                {
                    "id": "RULE-KRN-001",
                    "type": "filesystem_deny",
                    "patterns": ["exo/kernel/**"],
                    "actions": ["write", "delete"],
                    "message": "Blocked by RULE-KRN-001",
                }
            )
        )

    constitution = "# Test Constitution\n\n" + "\n".join(blocks)
    (exo_dir / "CONSTITUTION.md").write_text(constitution, encoding="utf-8")
    governance_mod.compile_constitution(repo)
    return repo


def test_governance_lock_contains_kernel_metadata_and_verifies(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)

    lock_path = repo / ".exo" / "governance.lock.json"
    lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert lock_data["kernel"]["name"] == KERNEL_NAME
    assert lock_data["kernel"]["version"] == KERNEL_VERSION

    report = verify_governance(load_governance(repo))
    assert report.valid is True
    assert report.reasons == []


def test_verify_integrity_fails_on_incompatible_kernel_version(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)
    lock_path = repo / ".exo" / "governance.lock.json"
    lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
    lock_data["kernel"]["version"] = "2.0.0"
    lock_path.write_text(json.dumps(lock_data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    with pytest.raises(ExoError) as err:
        governance_mod.verify_integrity(repo)
    assert err.value.code == "KERNEL_VERSION_UNSUPPORTED"


def test_check_action_denies_kernel_path_and_missing_lock(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path, require_lock=True, kernel_deny=True)
    gov = load_governance(repo)
    session = open_session(repo, "human:test")

    ticket = {
        "id": "TICKET-001",
        "title": "Kernel boundary test",
        "scope": {"allow": ["exo/**"], "deny": []},
        "created_at": "2026-02-10T00:00:00+00:00",
    }
    tickets_mod.save_ticket(repo, ticket)
    loaded_ticket = tickets_mod.load_ticket(repo, "TICKET-001")

    tickets_mod.acquire_lock(repo, "TICKET-001")
    deny_kernel = check_action(
        gov,
        session,
        loaded_ticket,
        {"kind": "write_file", "target": "exo/kernel/utils.py", "params": {}},
    )
    assert deny_kernel.status == "DENY"
    assert "RULE-KRN-001" in deny_kernel.reasons[0]
    tickets_mod.release_lock(repo, "TICKET-001")

    deny_lock = check_action(
        gov,
        session,
        loaded_ticket,
        {"kind": "write_file", "target": "exo/stdlib/dispatch.py", "params": {}},
    )
    assert deny_lock.status == "DENY"
    assert deny_lock.reasons[0].startswith("lock required:")


def test_check_action_denies_memory_index_mutation(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path, require_lock=False, kernel_deny=False)
    gov = load_governance(repo)
    session = open_session(repo, "human:test")

    ticket = {
        "id": "TICKET-002",
        "title": "Memory boundary test",
        "scope": {"allow": [".exo/**"], "deny": []},
        "created_at": "2026-02-10T00:00:00+00:00",
    }
    tickets_mod.save_ticket(repo, ticket)
    loaded_ticket = tickets_mod.load_ticket(repo, "TICKET-002")

    decision = check_action(
        gov,
        session,
        loaded_ticket,
        {"kind": "write_file", "target": ".exo/memory/index.yaml", "params": {}},
    )
    assert decision.status == "DENY"
    assert decision.reasons
    assert "Layer-4 memory is advisory and read-only" in decision.reasons[0]


def test_do_blocks_memory_writes_during_governed_execution(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path, require_lock=False, kernel_deny=False)
    (repo / ".exo" / "config.yaml").write_text('{"git_controls":{"enabled":false}}\n', encoding="utf-8")

    ticket = {
        "id": "TICKET-010",
        "title": "Attempt memory mutation in do flow",
        "status": "todo",
        "priority": 5,
        "scope": {"allow": [".exo/**"], "deny": []},
        "budgets": {"max_files_changed": 5, "max_loc_changed": 200},
        "checks": [],
        "created_at": "2026-02-10T00:00:00+00:00",
    }
    tickets_mod.save_ticket(repo, ticket)
    tickets_mod.acquire_lock(repo, "TICKET-010")

    patch_path = repo / ".exo" / "patches" / "memory-write.yaml"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(
        '{"patches":[{"path":".exo/memory/index.yaml","content":"version: 0.2\\n"}]}\n',
        encoding="utf-8",
    )

    engine = KernelEngine(repo=repo, actor="human:test", no_llm=True)
    with pytest.raises(ExoError) as do_err:
        engine.do("TICKET-010", patch_file=str(patch_path), mark_done=False)
    assert do_err.value.code == "MEMORY_MUTATION_FORBIDDEN"


def test_phase_a_ledger_records_from_mint_and_check(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path, require_lock=False, kernel_deny=False)
    gov = load_governance(repo)
    session = open_session(repo, "human:test")

    ticket = mint_ticket(session, "Create README", {"allow": ["README.md"], "deny": []}, 1)
    intent_records = ledger_mod.read_records(repo, record_type="IntentSubmitted", intent_id=ticket.id)
    assert len(intent_records) == 1
    intent_record = intent_records[0]
    assert intent_record["record_type"] == "IntentSubmitted"
    assert intent_record["intent_id"] == ticket.id
    assert intent_record["actor_id"] == "human:test"

    decision = check_action(
        gov,
        session,
        ticket,
        {"kind": "write_file", "target": "README.md", "params": {}},
    )
    assert decision.status == "ALLOW"
    assert "decision_id" in decision.constraints
    assert "decision_record" in decision.constraints

    decision_records = ledger_mod.read_records(repo, record_type="DecisionRecorded", intent_id=ticket.id)
    assert len(decision_records) == 1
    decision_record = decision_records[0]
    assert decision_record["record_type"] == "DecisionRecorded"
    assert decision_record["intent_id"] == ticket.id
    assert decision_record["outcome"] == "ALLOW"


def test_phase_b_execution_begin_idempotency_and_result_write_once(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path, require_lock=False, kernel_deny=False)
    gov = load_governance(repo)
    session = open_session(repo, "human:test")

    ticket = mint_ticket(session, "Write README", {"allow": ["README.md"], "deny": []}, 1)
    decision = check_action(
        gov,
        session,
        ticket,
        {"kind": "write_file", "target": "README.md", "params": {}},
    )
    assert decision.status == "ALLOW"
    decision_id = str(decision.constraints["decision_id"])

    begin_1 = ledger_mod.execution_begun(
        repo,
        effect_id="EFF-001",
        decision_id=decision_id,
        executor_ref="executor:local",
        idempotency_key="idem-001",
    )
    begin_2 = ledger_mod.execution_begun(
        repo,
        effect_id="EFF-001",
        decision_id=decision_id,
        executor_ref="executor:local",
        idempotency_key="idem-001",
    )
    assert begin_1.line == begin_2.line
    assert begin_1.record_hash == begin_2.record_hash

    with pytest.raises(ExoError) as idem_err:
        ledger_mod.execution_begun(
            repo,
            effect_id="EFF-002",
            decision_id=decision_id,
            executor_ref="executor:local",
            idempotency_key="idem-001",
        )
    assert idem_err.value.code == "IDEMPOTENCY_KEY_COLLISION"

    result_1 = ledger_mod.execution_result(
        repo,
        effect_id="EFF-001",
        status="OK",
        artifact_refs=["artifact://a"],
    )
    result_2 = ledger_mod.execution_result(
        repo,
        effect_id="EFF-001",
        status="OK",
        artifact_refs=["artifact://a"],
    )
    assert result_1.line == result_2.line
    assert result_1.record_hash == result_2.record_hash

    with pytest.raises(ExoError) as immutable_err:
        ledger_mod.execution_result(
            repo,
            effect_id="EFF-001",
            status="FAIL",
            artifact_refs=["artifact://a"],
        )
    assert immutable_err.value.code == "EXECUTION_RESULT_IMMUTABLE"

    with pytest.raises(ExoError) as missing_begin_err:
        ledger_mod.execution_result(
            repo,
            effect_id="EFF-404",
            status="OK",
            artifact_refs=[],
        )
    assert missing_begin_err.value.code == "EXECUTION_BEGIN_MISSING"


def test_phase_c_topic_head_cas_and_topic_reads(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path, require_lock=False, kernel_deny=False)
    gov = load_governance(repo)
    session = open_session(repo, "human:test")

    ticket = mint_ticket(session, "Write README", {"allow": ["README.md"], "deny": []}, 1)
    ticket_data = to_dict(ticket)
    topic_id = f"repo:{repo.resolve().as_posix()}"
    expected_head = f"line:{ticket_data['metadata']['intent_record']['line']}"
    assert ledger_mod.head(repo, topic_id) == expected_head

    decision = check_action(
        gov,
        session,
        ticket,
        {"kind": "write_file", "target": "README.md", "params": {}},
    )
    decision_id = str(decision.constraints["decision_id"])

    ledger_mod.execution_begun(
        repo,
        effect_id="EFF-100",
        decision_id=decision_id,
        executor_ref="executor:local",
        idempotency_key="idem-100",
    )
    ledger_mod.execution_result(
        repo,
        effect_id="EFF-100",
        status="OK",
        artifact_refs=["artifact://readme"],
    )

    topic_records = ledger_mod.read_records(repo, topic_id=topic_id, limit=20)
    assert [record["record_type"] for record in topic_records] == [
        "IntentSubmitted",
        "DecisionRecorded",
        "ExecutionBegun",
        "ExecutionResult",
    ]

    cas_failed = ledger_mod.cas_head(repo, topic_id, "line:999", "line:123")
    assert cas_failed["ok"] is False
    assert cas_failed["head"] == expected_head

    cas_ok = ledger_mod.cas_head(repo, topic_id, expected_head, "line:123")
    assert cas_ok == {"ok": True, "head": "line:123"}
    assert ledger_mod.head(repo, topic_id) == "line:123"

    read_from_cursor = ledger_mod.read_records(repo, topic_id=topic_id, since_cursor=expected_head, limit=20)
    assert [record["record_type"] for record in read_from_cursor] == [
        "DecisionRecorded",
        "ExecutionBegun",
        "ExecutionResult",
    ]


def test_phase_c_intent_causal_order_and_cycle_detection(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path, require_lock=False, kernel_deny=False)
    topic_id = "topic:causal"
    payload_hash_value = ledger_mod.payload_hash({"case": "causal"})

    ledger_mod.intent_submitted(
        repo,
        intent_id="INT-001",
        actor_id="human:test",
        topic_id=topic_id,
        payload_hash_value=payload_hash_value,
        parents=[],
    )
    ledger_mod.intent_submitted(
        repo,
        intent_id="INT-002",
        actor_id="human:test",
        topic_id=topic_id,
        payload_hash_value=payload_hash_value,
        parents=["INT-001"],
    )
    ledger_mod.intent_submitted(
        repo,
        intent_id="INT-003",
        actor_id="human:test",
        topic_id=topic_id,
        payload_hash_value=payload_hash_value,
        parents=["INT-001"],
    )
    ledger_mod.intent_submitted(
        repo,
        intent_id="INT-004",
        actor_id="human:test",
        topic_id=topic_id,
        payload_hash_value=payload_hash_value,
        parents=["INT-002", "INT-003"],
    )

    ordered = ledger_mod.intent_causal_order(repo, topic_id)
    assert ordered[0] == "INT-001"
    assert ordered[-1] == "INT-004"
    assert ordered.index("INT-002") < ordered.index("INT-004")
    assert ordered.index("INT-003") < ordered.index("INT-004")

    cycle_topic = "topic:cycle"
    ledger_mod.intent_submitted(
        repo,
        intent_id="INT-CYCLE-A",
        actor_id="human:test",
        topic_id=cycle_topic,
        payload_hash_value=payload_hash_value,
        parents=["INT-CYCLE-B"],
    )
    ledger_mod.intent_submitted(
        repo,
        intent_id="INT-CYCLE-B",
        actor_id="human:test",
        topic_id=cycle_topic,
        payload_hash_value=payload_hash_value,
        parents=["INT-CYCLE-A"],
    )

    with pytest.raises(ExoError) as cycle_err:
        ledger_mod.intent_causal_order(repo, cycle_topic)
    assert cycle_err.value.code == "INTENT_CAUSAL_CYCLE"


def test_phase_d_subscribe_cursor_and_ack_quorum(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path, require_lock=False, kernel_deny=False)
    gov = load_governance(repo)
    session = open_session(repo, "human:test")

    ticket = mint_ticket(session, "Write README", {"allow": ["README.md"], "deny": []}, 1)
    decision = check_action(
        gov,
        session,
        ticket,
        {"kind": "write_file", "target": "README.md", "params": {}},
    )
    decision_id = str(decision.constraints["decision_id"])
    ledger_mod.execution_begun(
        repo,
        effect_id="EFF-D-1",
        decision_id=decision_id,
        executor_ref="executor:local",
        idempotency_key="idem-d-1",
    )
    ledger_mod.execution_result(
        repo,
        effect_id="EFF-D-1",
        status="OK",
        artifact_refs=["artifact://d1"],
    )

    topic_id = f"repo:{repo.resolve().as_posix()}"
    batch_1 = ledger_mod.subscribe(repo, topic_id=topic_id, limit=2)
    assert batch_1["count"] == 2
    assert batch_1["next_cursor"] == "line:2"
    assert [event["record"]["record_type"] for event in batch_1["events"]] == [
        "IntentSubmitted",
        "DecisionRecorded",
    ]

    batch_2 = ledger_mod.subscribe(repo, topic_id=topic_id, since_cursor=batch_1["next_cursor"], limit=10)
    assert batch_2["count"] == 2
    assert batch_2["next_cursor"] == "line:4"
    assert [event["record"]["record_type"] for event in batch_2["events"]] == [
        "ExecutionBegun",
        "ExecutionResult",
    ]

    ack_1 = ledger_mod.acked(repo, actor_id="human:a", ref_id=decision_id)
    ack_1_replay = ledger_mod.acked(repo, actor_id="human:a", ref_id=decision_id)
    assert ack_1.line == ack_1_replay.line
    assert ack_1.record_hash == ack_1_replay.record_hash
    ledger_mod.acked(repo, actor_id="agent:b", ref_id=decision_id)

    quorum = ledger_mod.ack_status(repo, ref_id=decision_id, required=2)
    assert quorum["satisfied"] is True
    assert quorum["unique_ack_count"] == 2
    assert quorum["actors"] == ["human:a", "agent:b"]

    with pytest.raises(ExoError) as missing_ref_err:
        ledger_mod.acked(repo, actor_id="human:a", ref_id="DEC-404")
    assert missing_ref_err.value.code == "ACK_REF_NOT_FOUND"


def test_phase_e_override_and_policy_set_are_cap_gated_and_receipted(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path, require_lock=False, kernel_deny=False)
    session = open_session(repo, "human:test")

    ticket = mint_ticket(session, "Write README", {"allow": ["README.md"], "deny": []}, 1)
    intent_line = int(to_dict(ticket)["metadata"]["intent_record"]["line"])
    rationale_ref = f"line:{intent_line}"
    engine = KernelEngine(repo=repo, actor="human:ops")

    with pytest.raises(ExoError) as override_cap_err:
        engine.decide_override(ticket.id, override_cap="cap:invalid", rationale_ref=rationale_ref)
    assert override_cap_err.value.code == "CAPABILITY_DENIED"

    override_result = engine.decide_override(ticket.id, override_cap="cap:override", rationale_ref=rationale_ref)
    assert override_result["ok"] is True
    decision_id = str(override_result["data"]["decision_id"])
    decision_rows = ledger_mod.read_records(repo, record_type="DecisionRecorded", ref_id=decision_id, limit=1)
    assert len(decision_rows) == 1
    override_meta = decision_rows[0]["constraints"]["override"]
    assert override_meta["capability"] == "cap:override"
    assert override_result["data"]["receipt"]["audit_hashes"]
    assert override_result["data"]["receipt"]["kernel_version"] == KERNEL_VERSION

    governance_ticket = {
        "id": "GOV-001",
        "type": "governance",
        "title": "Governance update",
        "status": "todo",
        "priority": 5,
        "scope": {"allow": [".exo/**"], "deny": [".git/**"]},
        "created_at": "2026-02-10T00:00:00+00:00",
    }
    tickets_mod.save_ticket(repo, governance_ticket)
    tickets_mod.acquire_lock(repo, "GOV-001")

    with pytest.raises(ExoError) as policy_cap_err:
        engine.policy_set(policy_cap="cap:invalid", version="0.2")
    assert policy_cap_err.value.code == "CAPABILITY_DENIED"

    policy_result = engine.policy_set(policy_cap="cap:policy-set", version="0.2")
    assert policy_result["ok"] is True
    assert policy_result["data"]["policy_version"] == "0.2"
    assert policy_result["data"]["ticket_id"] == "GOV-001"
    assert policy_result["data"]["receipt"]["audit_hashes"]
    assert policy_result["data"]["receipt"]["kernel_version"] == KERNEL_VERSION

    lock_data = json.loads((repo / ".exo" / "governance.lock.json").read_text(encoding="utf-8"))
    assert lock_data["version"] == "0.2"
    assert verify_governance(load_governance(repo)).valid is True


def test_phase_f_cas_head_retry_and_conflict_surface(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path, require_lock=False, kernel_deny=False)
    session = open_session(repo, "human:test")
    topic_id = f"repo:{repo.resolve().as_posix()}"

    first = mint_ticket(session, "First intent", {"allow": ["README.md"], "deny": []}, 1)
    second = mint_ticket(session, "Second intent", {"allow": ["README.md"], "deny": []}, 1)
    first_line = int(to_dict(first)["metadata"]["intent_record"]["line"])
    second_line = int(to_dict(second)["metadata"]["intent_record"]["line"])
    assert ledger_mod.head(repo, topic_id) == f"line:{second_line}"

    retry_result = ledger_mod.cas_head_retry(
        repo,
        topic_id,
        f"line:{first_line}",
        "line:777",
        max_attempts=2,
    )
    assert retry_result["ok"] is True
    assert retry_result["attempts"] == 2
    assert retry_result["head"] == "line:777"
    assert len(retry_result["history"]) == 2

    engine = KernelEngine(repo=repo, actor="human:ops")
    with pytest.raises(ExoError) as cap_err:
        engine.cas_head(
            topic_id,
            expected_ref=f"line:{first_line}",
            new_ref="line:888",
            max_attempts=2,
            control_cap="cap:invalid",
        )
    assert cap_err.value.code == "CAPABILITY_DENIED"

    with pytest.raises(ExoError) as conflict_err:
        engine.cas_head(
            topic_id,
            expected_ref=f"line:{first_line}",
            new_ref="line:888",
            max_attempts=1,
            control_cap="cap:cas-head",
        )
    assert conflict_err.value.code == "CAS_HEAD_CONFLICT"
    assert conflict_err.value.details
    assert conflict_err.value.details.get("retry", {}).get("retryable") is True

    applied = engine.cas_head(
        topic_id,
        expected_ref=f"line:{first_line}",
        new_ref="line:889",
        max_attempts=3,
        control_cap="cap:cas-head",
    )
    assert applied["ok"] is True
    assert applied["data"]["head"] == "line:889"
    assert applied["data"]["attempts"] >= 2
    assert applied["data"]["receipt"]["kernel_version"] == KERNEL_VERSION


def test_phase_g_submit_intent_uses_optimistic_head_cas(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path, require_lock=False, kernel_deny=False)
    session = open_session(repo, "human:test")
    topic_id = f"repo:{repo.resolve().as_posix()}"

    first = mint_ticket(session, "First intent", {"allow": ["README.md"], "deny": []}, 1)
    first_record = ledger_mod.read_records(repo, record_type="IntentSubmitted", intent_id=first.id, limit=1)[0]
    first_cursor = f"line:{to_dict(first)['metadata']['intent_record']['line']}"

    stale_expected = "line:999"
    with pytest.raises(ExoError) as stale_err:
        ledger_mod.intent_submitted(
            repo,
            intent_id="INT-ST-001",
            actor_id="human:test",
            topic_id=topic_id,
            payload_hash_value=ledger_mod.payload_hash({"intent": "stale"}),
            expected_head=stale_expected,
            max_head_attempts=1,
        )
    assert stale_err.value.code == "CAS_HEAD_CONFLICT"
    assert stale_err.value.details
    assert stale_err.value.details.get("actual_ref") == first_cursor

    second = mint_ticket(session, "Second intent", {"allow": ["README.md"], "deny": []}, 1)
    second_record = ledger_mod.read_records(repo, record_type="IntentSubmitted", intent_id=second.id, limit=1)[0]
    assert first_record["parents"] == []
    assert first_record["metadata"]["head"]["expected_ref"] is None
    assert second_record["parents"] == [first.id]
    assert second_record["metadata"]["head"]["actual_ref"] == first_cursor


def test_phase_h_12_syscall_surface_happy_path(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path, require_lock=False, kernel_deny=False)
    topic_id = f"repo:{repo.resolve().as_posix()}"
    api = KernelSyscalls(repo, actor="human:test")

    intent_id = api.submit(
        {
            "topic": topic_id,
            "intent": "Create README",
            "scope": {"allow": ["README.md"], "deny": []},
            "action": {"kind": "write_file", "target": "README.md", "params": {}},
            "max_attempts": 2,
        }
    )

    decision_id = api.check(intent_id, context_refs=[])
    decision_rows = ledger_mod.read_records(repo, record_type="DecisionRecorded", ref_id=decision_id, limit=1)
    assert len(decision_rows) == 1
    assert decision_rows[0]["outcome"] == "ALLOW"

    effect_id = api.begin(decision_id, "executor:local", "idem-h-001")
    api.commit(effect_id, "OK", ["artifact://phase-h"])
    result_rows = ledger_mod.read_records(repo, record_type="ExecutionResult", ref_id=effect_id, limit=1)
    assert len(result_rows) == 1
    assert result_rows[0]["status"] == "OK"

    api.ack(decision_id, actor_cap="cap:ack")
    ack_rows = ledger_mod.read_records(repo, record_type="Acked", ref_id=decision_id, limit=1)
    assert len(ack_rows) == 1

    sub = api.subscribe(topic_id)
    assert sub["count"] >= 4
    assert api.read(topic_id, {"limit": 2})

    head_ref = api.head(topic_id)
    cas_result = api.cas_head(topic_id, head_ref, head_ref, control_cap="cap:cas-head")
    assert cas_result["ok"] is True

    api.escalate(intent_id, "manual_review", [decision_id])
    esc_rows = ledger_mod.read_records(repo, record_type="Escalated", intent_id=intent_id, limit=1)
    assert len(esc_rows) == 1

    override_id = api.decide_override(intent_id, "cap:override", rationale_ref=decision_id)
    override_rows = ledger_mod.read_records(repo, record_type="DecisionRecorded", ref_id=override_id, limit=1)
    assert len(override_rows) == 1

    governance_ticket = {
        "id": "GOV-001",
        "type": "governance",
        "title": "Governance update",
        "status": "todo",
        "priority": 5,
        "scope": {"allow": [".exo/**"], "deny": [".git/**"]},
        "created_at": "2026-02-10T00:00:00+00:00",
    }
    tickets_mod.save_ticket(repo, governance_ticket)
    tickets_mod.acquire_lock(repo, "GOV-001")
    version = api.policy_set(policy_bundle=None, policy_cap="cap:policy-set", version="0.2")
    assert version == "0.2"
    assert verify_governance(load_governance(repo)).valid is True


def test_phase_h_policy_set_requires_governance_lock(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path, require_lock=False, kernel_deny=False)
    api = KernelSyscalls(repo, actor="human:test")

    with pytest.raises(ExoError) as policy_err:
        api.policy_set(policy_bundle=None, policy_cap="cap:policy-set", version="0.2")
    assert policy_err.value.code == "LOCK_REQUIRED"


def test_phase_h_check_denies_memory_mutation_intent(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path, require_lock=False, kernel_deny=False)
    topic_id = f"repo:{repo.resolve().as_posix()}"
    api = KernelSyscalls(repo, actor="human:test")

    intent_id = api.submit(
        {
            "topic": topic_id,
            "intent": "Mutate memory index",
            "scope": {"allow": [".exo/**"], "deny": []},
            "action": {"kind": "write_file", "target": ".exo/memory/index.yaml", "params": {}},
        }
    )
    decision_id = api.check(intent_id, context_refs=[])
    decision_rows = ledger_mod.read_records(repo, record_type="DecisionRecorded", ref_id=decision_id, limit=1)
    assert len(decision_rows) == 1
    assert decision_rows[0]["outcome"] == "DENY"
    reasons = decision_rows[0].get("reasons") if isinstance(decision_rows[0].get("reasons"), list) else []
    assert reasons
    assert "Layer-4 memory is advisory and read-only" in str(reasons[0])


def test_ticket_validation_request_vs_persistent_semantics(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path, require_lock=False, kernel_deny=False)
    gov = load_governance(repo)

    persistent_ticket = {
        "id": "TICKET-777",
        "title": "Long-lived ticket",
        "scope": {"allow": ["**"], "deny": []},
        "created_at": "2026-02-10T00:00:00+00:00",
    }
    persistent_status = validate_ticket(gov, persistent_ticket)
    assert persistent_status.status == "VALID"

    session = open_session(repo, "human:test")
    request_ticket = mint_ticket(session, "Do a request action", {"allow": ["README.md"], "deny": []}, 1)
    request_status = validate_ticket(gov, request_ticket)
    assert request_status.status == "VALID"

    missing_expiry = to_dict(request_ticket)
    missing_expiry.pop("expires_at", None)
    missing_expiry_status = validate_ticket(gov, missing_expiry)
    assert missing_expiry_status.status == "INVALID"
    assert "ticket.expires_at must be RFC3339 date-time" in missing_expiry_status.reasons

    expired = to_dict(request_ticket)
    expired["expires_at"] = "2000-01-01T00:00:00+00:00"
    expired_status = validate_ticket(gov, expired)
    assert expired_status.status == "INVALID"
    assert "ticket is expired" in expired_status.reasons


def test_append_audit_and_receipt_embed_kernel_version(tmp_path: Path) -> None:
    repo = _bootstrap_repo(tmp_path)

    audit_ref = append_audit(
        repo,
        {
            "actor": "human:test",
            "action": "unit_test",
            "result": "ok",
        },
    )
    audit_path = repo / audit_ref.log_path
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[-1])
    assert payload["kernel_name"] == KERNEL_NAME
    assert payload["kernel_version"] == KERNEL_VERSION

    session = open_session(repo, "human:test")
    ticket = mint_ticket(session, "Emit receipt", {"allow": ["README.md"], "deny": []}, 1)
    receipt = seal_result(
        session,
        ticket,
        {"kind": "write_file", "target": "README.md", "params": {}, "mode": "execute"},
        {"decision": {"status": "ALLOW"}, "plan": {"steps": []}},
        [audit_ref],
    )
    assert receipt.kernel_name == KERNEL_NAME
    assert receipt.kernel_version == KERNEL_VERSION
    assert receipt.audit_hashes == [audit_ref.event_hash]


def test_kernel_public_api_surface_is_exactly_10_functions() -> None:
    expected = [
        "load_governance",
        "verify_governance",
        "open_session",
        "mint_ticket",
        "validate_ticket",
        "check_action",
        "resolve_requirements",
        "commit_plan",
        "append_audit",
        "seal_result",
    ]
    assert kernel_api.__all__ == expected
    assert len(kernel_api.__all__) == 10
    for name in expected:
        assert callable(getattr(kernel_api, name))
