"""Tests for ticket/intent ID collision prevention.

Covers:
- _id_guard file locking
- allocate_ticket_id atomic reservation
- allocate_intent_id atomic reservation
- Concurrent allocation produces unique IDs
- Epic suffix handled correctly
- Placeholder files written and overwritable by save_ticket
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from exo.kernel import governance as governance_mod
from exo.kernel import tickets as tickets_mod


def _policy_block(rule: dict[str, Any]) -> str:
    return f"\n```yaml exo-policy\n{json.dumps(rule)}\n```\n"


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
    return repo


# ---------------------------------------------------------------------------
# allocate_ticket_id
# ---------------------------------------------------------------------------
class TestAllocateTicketId:
    def test_first_allocation_returns_001(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        tid = tickets_mod.allocate_ticket_id(repo)
        assert tid == "TICKET-001"

    def test_placeholder_file_created(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        tid = tickets_mod.allocate_ticket_id(repo)
        path = tickets_mod.ticket_path(repo, tid)
        assert path.exists()

    def test_sequential_allocations_unique(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        ids = [tickets_mod.allocate_ticket_id(repo) for _ in range(5)]
        assert len(set(ids)) == 5
        assert ids == ["TICKET-001", "TICKET-002", "TICKET-003", "TICKET-004", "TICKET-005"]

    def test_save_ticket_overwrites_placeholder(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        tid = tickets_mod.allocate_ticket_id(repo)
        # Placeholder has minimal data
        placeholder = tickets_mod.load_ticket(repo, tid)
        assert placeholder.get("title") is None or placeholder.get("title") == ""
        # Full save overwrites
        tickets_mod.save_ticket(repo, {
            "id": tid,
            "title": "Real ticket",
            "intent": "Do something real",
            "kind": "task",
            "status": "todo",
        })
        loaded = tickets_mod.load_ticket(repo, tid)
        assert loaded["title"] == "Real ticket"

    def test_epic_allocation_has_suffix(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        tid = tickets_mod.allocate_ticket_id(repo, kind="epic")
        assert tid.endswith("-EPIC")
        assert tid == "TICKET-001-EPIC"

    def test_epic_and_task_no_collision(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        epic = tickets_mod.allocate_ticket_id(repo, kind="epic")
        task = tickets_mod.allocate_ticket_id(repo)
        assert epic == "TICKET-001-EPIC"
        assert task == "TICKET-002"

    def test_skips_existing_numbers(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        # Pre-create TICKET-001 manually
        tickets_mod.save_ticket(repo, {
            "id": "TICKET-001",
            "title": "Existing",
            "intent": "Existing",
            "status": "todo",
        })
        tid = tickets_mod.allocate_ticket_id(repo)
        assert tid == "TICKET-002"


# ---------------------------------------------------------------------------
# allocate_intent_id
# ---------------------------------------------------------------------------
class TestAllocateIntentId:
    def test_first_allocation_returns_001(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        iid = tickets_mod.allocate_intent_id(repo)
        assert iid == "INTENT-001"

    def test_placeholder_file_created(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        iid = tickets_mod.allocate_intent_id(repo)
        path = tickets_mod.ticket_path(repo, iid)
        assert path.exists()

    def test_sequential_allocations_unique(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        ids = [tickets_mod.allocate_intent_id(repo) for _ in range(5)]
        assert len(set(ids)) == 5
        assert ids == ["INTENT-001", "INTENT-002", "INTENT-003", "INTENT-004", "INTENT-005"]

    def test_save_ticket_overwrites_placeholder(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        iid = tickets_mod.allocate_intent_id(repo)
        tickets_mod.save_ticket(repo, {
            "id": iid,
            "title": "Real intent",
            "intent": "The real deal",
            "kind": "intent",
            "brain_dump": "User wants auth",
            "status": "todo",
        })
        loaded = tickets_mod.load_ticket(repo, iid)
        assert loaded["brain_dump"] == "User wants auth"


# ---------------------------------------------------------------------------
# Concurrent allocation (thread-based)
# ---------------------------------------------------------------------------
class TestConcurrentAllocation:
    def test_concurrent_ticket_ids_unique(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        results: list[str] = []
        errors: list[Exception] = []

        def allocate():
            try:
                tid = tickets_mod.allocate_ticket_id(repo)
                results.append(tid)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=allocate) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during allocation: {errors}"
        assert len(results) == 10
        assert len(set(results)) == 10, f"Duplicate IDs: {results}"

    def test_concurrent_intent_ids_unique(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        results: list[str] = []
        errors: list[Exception] = []

        def allocate():
            try:
                iid = tickets_mod.allocate_intent_id(repo)
                results.append(iid)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=allocate) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during allocation: {errors}"
        assert len(results) == 10
        assert len(set(results)) == 10, f"Duplicate IDs: {results}"

    def test_concurrent_mixed_ticket_and_intent(self, tmp_path: Path) -> None:
        """Ticket and intent IDs use the same lock, so mixed allocation is safe."""
        repo = _bootstrap_repo(tmp_path)
        ticket_results: list[str] = []
        intent_results: list[str] = []
        errors: list[Exception] = []

        def alloc_ticket():
            try:
                ticket_results.append(tickets_mod.allocate_ticket_id(repo))
            except Exception as exc:
                errors.append(exc)

        def alloc_intent():
            try:
                intent_results.append(tickets_mod.allocate_intent_id(repo))
            except Exception as exc:
                errors.append(exc)

        threads = []
        for _ in range(5):
            threads.append(threading.Thread(target=alloc_ticket))
            threads.append(threading.Thread(target=alloc_intent))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(set(ticket_results)) == 5, f"Duplicate ticket IDs: {ticket_results}"
        assert len(set(intent_results)) == 5, f"Duplicate intent IDs: {intent_results}"


# ---------------------------------------------------------------------------
# _id_guard file lock
# ---------------------------------------------------------------------------
class TestIdGuard:
    def test_guard_file_created(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        tickets_mod.allocate_ticket_id(repo)
        guard = repo / tickets_mod.ID_GUARD_FILE
        assert guard.exists()

    def test_guard_directory_auto_created(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        locks_dir = repo / ".exo" / "locks"
        if locks_dir.exists():
            import shutil
            shutil.rmtree(locks_dir)
        # Should auto-create
        tid = tickets_mod.allocate_ticket_id(repo)
        assert tid == "TICKET-001"
        assert (repo / tickets_mod.ID_GUARD_FILE).exists()


# ---------------------------------------------------------------------------
# next_ticket_id / next_intent_id still work (backward compat peek)
# ---------------------------------------------------------------------------
class TestPeekFunctionsStillWork:
    def test_next_ticket_id_peek(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        assert tickets_mod.next_ticket_id(repo) == "TICKET-001"
        tickets_mod.allocate_ticket_id(repo)
        assert tickets_mod.next_ticket_id(repo) == "TICKET-002"

    def test_next_intent_id_peek(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        assert tickets_mod.next_intent_id(repo) == "INTENT-001"
        tickets_mod.allocate_intent_id(repo)
        assert tickets_mod.next_intent_id(repo) == "INTENT-002"
