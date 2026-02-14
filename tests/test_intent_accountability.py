"""Tests for the Intent Accountability Layer.

Covers:
- Ticket schema extension (kind, brain_dump, boundary, success_condition, risk)
- Intent hierarchy validation (task → epic → intent chain)
- Drift detection / reconciliation
- Intent timeline builder
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from exo.kernel import governance as governance_mod
from exo.kernel import tickets as tickets_mod
from exo.kernel.types import TICKET_KINDS, INTENT_RISKS
from exo.stdlib.reconcile import (
    BudgetUsage,
    DriftReport,
    reconcile_session,
    drift_report_to_dict,
    format_drift_section,
    _check_scope_compliance,
    _check_boundary_violations,
)
from exo.stdlib.timeline import build_intent_timeline, format_timeline_human


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


def _seed_intent(repo: Path, intent_id: str = "INTENT-001", **overrides: Any) -> dict[str, Any]:
    ticket = {
        "id": intent_id,
        "kind": "intent",
        "title": "Add user auth",
        "brain_dump": "I want to add basic user authentication with login and logout",
        "boundary": "only touch src/auth/, do not refactor existing endpoints",
        "success_condition": "login returns 200 with valid creds, 401 with invalid",
        "risk": "medium",
        "status": "todo",
        "scope": {"allow": ["src/auth/**"], "deny": []},
        "budgets": {"max_files_changed": 8, "max_loc_changed": 300},
        **overrides,
    }
    tickets_mod.save_ticket(repo, ticket)
    return ticket


def _seed_epic(
    repo: Path, epic_id: str = "TICKET-001", parent_id: str = "INTENT-001", **overrides: Any
) -> dict[str, Any]:
    ticket = {
        "id": epic_id,
        "kind": "epic",
        "title": "Auth implementation epic",
        "parent_id": parent_id,
        "status": "todo",
        "children": [],
        **overrides,
    }
    tickets_mod.save_ticket(repo, ticket)
    return ticket


def _seed_task(
    repo: Path, task_id: str = "TICKET-002", parent_id: str = "TICKET-001", **overrides: Any
) -> dict[str, Any]:
    ticket = {
        "id": task_id,
        "kind": "task",
        "title": "Implement login endpoint",
        "parent_id": parent_id,
        "status": "todo",
        "scope": {"allow": ["src/auth/**"], "deny": []},
        "budgets": {"max_files_changed": 5, "max_loc_changed": 200},
        **overrides,
    }
    tickets_mod.save_ticket(repo, ticket)
    return ticket


# ──────────────────────────────────────────────
# Phase A: Schema Extension
# ──────────────────────────────────────────────


class TestSchemaExtension:
    def test_normalize_ticket_adds_intent_fields(self, tmp_path: Path) -> None:
        normalized = tickets_mod.normalize_ticket({"id": "TICKET-100", "title": "test"})
        assert normalized["kind"] == "task"
        assert normalized["brain_dump"] == ""
        assert normalized["boundary"] == ""
        assert normalized["success_condition"] == ""
        assert normalized["risk"] == "medium"
        assert normalized["children"] == []

    def test_normalize_ticket_preserves_intent_fields(self, tmp_path: Path) -> None:
        normalized = tickets_mod.normalize_ticket(
            {
                "id": "INTENT-001",
                "kind": "intent",
                "brain_dump": "raw brain dump",
                "boundary": "do not touch tests/",
                "success_condition": "all tests pass",
                "risk": "high",
                "children": ["TICKET-001"],
            }
        )
        assert normalized["kind"] == "intent"
        assert normalized["brain_dump"] == "raw brain dump"
        assert normalized["boundary"] == "do not touch tests/"
        assert normalized["success_condition"] == "all tests pass"
        assert normalized["risk"] == "high"
        assert normalized["children"] == ["TICKET-001"]

    def test_normalize_ticket_invalid_kind_defaults_to_task(self) -> None:
        normalized = tickets_mod.normalize_ticket({"id": "T1", "kind": "garbage"})
        assert normalized["kind"] == "task"

    def test_normalize_ticket_invalid_risk_defaults_to_medium(self) -> None:
        normalized = tickets_mod.normalize_ticket({"id": "T1", "risk": "extreme"})
        assert normalized["risk"] == "medium"

    def test_ticket_kinds_and_risks_constants(self) -> None:
        assert TICKET_KINDS == {"intent", "epic", "task"}
        assert INTENT_RISKS == {"low", "medium", "high"}

    def test_intent_id_pattern_recognized(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_intent(repo)
        ticket = tickets_mod.load_ticket(repo, "INTENT-001")
        assert ticket["id"] == "INTENT-001"
        assert ticket["kind"] == "intent"

    def test_next_intent_id(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        assert tickets_mod.next_intent_id(repo) == "INTENT-001"
        _seed_intent(repo, "INTENT-001")
        assert tickets_mod.next_intent_id(repo) == "INTENT-002"

    def test_validate_ticket_rejects_invalid_kind(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        gov = governance_mod.load_governance(repo)
        ticket = {
            "id": "TICKET-100",
            "intent": "test",
            "kind": "bad_kind",
            "scope": {"allow": ["**"]},
            "ttl_hours": 1,
            "created_at": tickets_mod.now_iso(),
            "expires_at": "2099-01-01T00:00:00+00:00",
            "nonce": "abc",
        }
        status = tickets_mod.validate_ticket(gov, ticket)
        assert any("ticket.kind" in r for r in status.reasons)

    def test_validate_ticket_rejects_invalid_risk(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        gov = governance_mod.load_governance(repo)
        ticket = {
            "id": "TICKET-100",
            "intent": "test",
            "risk": "extreme",
            "scope": {"allow": ["**"]},
            "ttl_hours": 1,
            "created_at": tickets_mod.now_iso(),
            "expires_at": "2099-01-01T00:00:00+00:00",
            "nonce": "abc",
        }
        status = tickets_mod.validate_ticket(gov, ticket)
        assert any("ticket.risk" in r for r in status.reasons)

    def test_validate_ticket_accepts_valid_kind_and_risk(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        gov = governance_mod.load_governance(repo)
        ticket = {
            "id": "TICKET-100",
            "intent": "test",
            "kind": "epic",
            "risk": "high",
            "scope": {"allow": ["**"]},
            "ttl_hours": 1,
            "created_at": tickets_mod.now_iso(),
            "expires_at": "2099-01-01T00:00:00+00:00",
            "nonce": "abc",
        }
        status = tickets_mod.validate_ticket(gov, ticket)
        assert not any("ticket.kind" in r for r in status.reasons)
        assert not any("ticket.risk" in r for r in status.reasons)


# ──────────────────────────────────────────────
# Phase B: Intent Hierarchy Validation
# ──────────────────────────────────────────────


class TestIntentHierarchy:
    def test_intent_root_is_valid(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        intent = _seed_intent(repo)
        reasons = tickets_mod.validate_intent_hierarchy(repo, intent)
        assert reasons == []

    def test_intent_without_brain_dump_warns(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        intent = _seed_intent(repo, brain_dump="")
        loaded = tickets_mod.load_ticket(repo, "INTENT-001")
        reasons = tickets_mod.validate_intent_hierarchy(repo, loaded)
        assert any("brain_dump" in r for r in reasons)

    def test_epic_with_valid_parent(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_intent(repo)
        _seed_epic(repo, parent_id="INTENT-001")
        epic = tickets_mod.load_ticket(repo, "TICKET-001")
        reasons = tickets_mod.validate_intent_hierarchy(repo, epic)
        assert reasons == []

    def test_epic_without_parent_is_invalid(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_epic(repo, parent_id=None)
        epic = tickets_mod.load_ticket(repo, "TICKET-001")
        reasons = tickets_mod.validate_intent_hierarchy(repo, epic)
        assert any("parent_id" in r for r in reasons)

    def test_task_chains_to_intent_via_epic(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_intent(repo)
        _seed_epic(repo, parent_id="INTENT-001")
        _seed_task(repo, parent_id="TICKET-001")
        task = tickets_mod.load_ticket(repo, "TICKET-002")
        reasons = tickets_mod.validate_intent_hierarchy(repo, task)
        assert reasons == []

    def test_task_without_parent_is_invalid(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_task(repo, parent_id=None)
        task = tickets_mod.load_ticket(repo, "TICKET-002")
        reasons = tickets_mod.validate_intent_hierarchy(repo, task)
        assert any("parent_id" in r or "parent chain" in r for r in reasons)

    def test_task_with_broken_chain_is_invalid(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        # Task points to epic that points to nonexistent intent
        _seed_epic(repo, parent_id="INTENT-999")
        _seed_task(repo, parent_id="TICKET-001")
        task = tickets_mod.load_ticket(repo, "TICKET-002")
        reasons = tickets_mod.validate_intent_hierarchy(repo, task)
        assert any("parent chain" in r for r in reasons)

    def test_governance_tickets_are_exempt(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        gov_ticket = {"id": "GOV-001", "kind": "task", "title": "Gov ticket", "status": "todo"}
        tickets_mod.save_ticket(repo, gov_ticket)
        loaded = tickets_mod.load_ticket(repo, "GOV-001")
        reasons = tickets_mod.validate_intent_hierarchy(repo, loaded)
        assert reasons == []

    def test_resolve_intent_root_finds_root(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_intent(repo)
        _seed_epic(repo, parent_id="INTENT-001")
        _seed_task(repo, parent_id="TICKET-001")
        task = tickets_mod.load_ticket(repo, "TICKET-002")
        root = tickets_mod.resolve_intent_root(repo, task)
        assert root is not None
        assert root["id"] == "INTENT-001"
        assert root["kind"] == "intent"

    def test_resolve_intent_root_returns_none_for_orphan(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_task(repo, parent_id=None)
        task = tickets_mod.load_ticket(repo, "TICKET-002")
        root = tickets_mod.resolve_intent_root(repo, task)
        assert root is None


# ──────────────────────────────────────────────
# Phase C: Drift Detection / Reconciliation
# ──────────────────────────────────────────────


class TestDriftDetection:
    def test_scope_compliance_all_in_scope(self, tmp_path: Path) -> None:
        repo = tmp_path
        (repo / "src" / "auth").mkdir(parents=True)
        (repo / "src" / "auth" / "login.py").write_text("code\n")
        scope = {"allow": ["src/auth/**"], "deny": []}
        compliance, out_of_scope = _check_scope_compliance(["src/auth/login.py"], scope, repo)
        assert compliance == 1.0
        assert out_of_scope == []

    def test_scope_compliance_partial(self, tmp_path: Path) -> None:
        repo = tmp_path
        (repo / "src" / "auth").mkdir(parents=True)
        (repo / "src" / "auth" / "login.py").write_text("code\n")
        (repo / "docs").mkdir(parents=True)
        (repo / "docs" / "README.md").write_text("docs\n")
        scope = {"allow": ["src/auth/**"], "deny": []}
        compliance, out_of_scope = _check_scope_compliance(["src/auth/login.py", "docs/README.md"], scope, repo)
        assert compliance == 0.5
        assert out_of_scope == ["docs/README.md"]

    def test_scope_compliance_empty_changes(self, tmp_path: Path) -> None:
        compliance, out_of_scope = _check_scope_compliance([], {"allow": ["**"]}, tmp_path)
        assert compliance == 1.0
        assert out_of_scope == []

    def test_boundary_violations_detected(self, tmp_path: Path) -> None:
        repo = tmp_path
        (repo / "tests").mkdir()
        (repo / "tests" / "test_auth.py").write_text("test\n")
        violations = _check_boundary_violations(
            ["tests/test_auth.py"],
            "do not touch tests/**",
            repo,
        )
        assert "tests/test_auth.py" in violations

    def test_boundary_violations_no_patterns(self, tmp_path: Path) -> None:
        violations = _check_boundary_violations(
            ["src/auth/login.py"],
            "only touch auth module, be careful",
            tmp_path,
        )
        # No glob-like tokens in boundary text
        assert violations == []

    def test_drift_report_to_dict(self) -> None:
        report = DriftReport(
            scope_compliance=0.8,
            budget_files=BudgetUsage(used=5, max=10, ratio=0.5),
            budget_loc=BudgetUsage(used=100, max=400, ratio=0.25),
            out_of_scope_files=["docs/README.md"],
            boundary_violations=[],
            drift_score=0.2,
            changed_files=["src/auth/login.py", "docs/README.md"],
            total_loc_changed=100,
        )
        d = drift_report_to_dict(report)
        assert d["drift_score"] == 0.2
        assert d["scope_compliance"] == 0.8
        assert d["budget_files"]["used"] == 5

    def test_format_drift_section(self) -> None:
        report = DriftReport(
            scope_compliance=0.92,
            budget_files=BudgetUsage(used=8, max=12, ratio=0.667),
            budget_loc=BudgetUsage(used=245, max=400, ratio=0.613),
            out_of_scope_files=["docs/README.md"],
            boundary_violations=[],
            drift_score=0.23,
            changed_files=["a.py"] * 12,
            total_loc_changed=245,
            intent_root_id="INTENT-001",
        )
        text = format_drift_section(report)
        assert "drift_score: 0.23" in text
        assert "intent_root: INTENT-001" in text
        assert "boundary_violations: []" in text

    def test_reconcile_session_with_git(self, tmp_path: Path) -> None:
        """Full reconciliation against a git repo with actual changes."""
        repo = tmp_path
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True)

        # Create base commit
        (repo / "README.md").write_text("hello\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)

        # Create work branch with changes
        subprocess.run(["git", "checkout", "-b", "work"], cwd=str(repo), capture_output=True)
        (repo / "src" / "auth").mkdir(parents=True)
        (repo / "src" / "auth" / "login.py").write_text("def login(): pass\n")
        (repo / "docs").mkdir(parents=True)
        (repo / "docs" / "api.md").write_text("API docs\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "add auth"], cwd=str(repo), capture_output=True)

        ticket = {
            "id": "TICKET-001",
            "kind": "task",
            "scope": {"allow": ["src/auth/**"], "deny": []},
            "budgets": {"max_files_changed": 5, "max_loc_changed": 200},
            "boundary": "",
        }

        report = reconcile_session(repo, ticket, git_base="main")
        assert isinstance(report, DriftReport)
        assert report.drift_score >= 0.0
        assert report.drift_score <= 1.0
        assert len(report.changed_files) == 2
        assert "docs/api.md" in report.out_of_scope_files
        assert report.scope_compliance == 0.5  # 1 of 2 files in scope


# ──────────────────────────────────────────────
# Phase D: Intent Timeline
# ──────────────────────────────────────────────


class TestIntentTimeline:
    def test_empty_timeline(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        timeline = build_intent_timeline(repo)
        assert timeline["intents"] == []
        assert timeline["orphan_tickets"] == []
        assert timeline["summary"]["total_intents"] == 0

    def test_timeline_with_intent_and_descendants(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_intent(repo)
        _seed_epic(repo, parent_id="INTENT-001")
        _seed_task(repo, task_id="TICKET-002", parent_id="TICKET-001")

        # Update intent to list children
        intent = tickets_mod.load_ticket(repo, "INTENT-001")
        intent["children"] = ["TICKET-001"]
        tickets_mod.save_ticket(repo, intent)

        timeline = build_intent_timeline(repo)
        assert len(timeline["intents"]) == 1
        assert timeline["intents"][0]["id"] == "INTENT-001"
        assert timeline["intents"][0]["descendant_count"] >= 1
        assert timeline["summary"]["total_intents"] == 1

    def test_timeline_detects_orphan_tickets(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_task(repo, task_id="TICKET-010", parent_id=None)
        timeline = build_intent_timeline(repo)
        assert len(timeline["orphan_tickets"]) == 1
        assert timeline["orphan_tickets"][0]["id"] == "TICKET-010"

    def test_timeline_human_formatting(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_intent(repo)
        timeline = build_intent_timeline(repo)
        text = format_timeline_human(timeline)
        assert "INTENT-001" in text
        assert "intent" in text
        assert "Add user auth" in text

    def test_timeline_excludes_governance_tickets(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        gov = {"id": "GOV-001", "kind": "task", "title": "Gov ticket", "status": "todo"}
        tickets_mod.save_ticket(repo, gov)
        timeline = build_intent_timeline(repo)
        # GOV tickets should not appear in intents or orphans
        all_ids = [i["id"] for i in timeline["intents"]] + [o["id"] for o in timeline["orphan_tickets"]]
        assert "GOV-001" not in all_ids

    def test_timeline_with_session_data(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_intent(repo)

        # Write a fake session index entry
        index_dir = repo / ".exo" / "memory" / "sessions"
        index_dir.mkdir(parents=True, exist_ok=True)
        row = {
            "session_id": "SES-20260210-001",
            "actor": "agent:test",
            "ticket_id": "INTENT-001",
            "vendor": "anthropic",
            "model": "claude-opus",
            "started_at": "2026-02-10T10:00:00+00:00",
            "finished_at": "2026-02-10T12:00:00+00:00",
            "verify": "passed",
            "drift_score": 0.15,
        }
        (index_dir / "index.jsonl").write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")

        timeline = build_intent_timeline(repo)
        assert len(timeline["intents"]) == 1
        sessions = timeline["intents"][0]["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["drift_score"] == 0.15
        assert timeline["intents"][0]["drift_avg"] == 0.15


# ──────────────────────────────────────────────
# Phase E: Intent/Ticket Creation CLI
# ──────────────────────────────────────────────

from exo.cli import main as cli_main


class TestIntentCreate:
    def test_intent_create_via_cli(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        rc = cli_main(
            [
                "--repo",
                str(repo),
                "--format",
                "json",
                "intent-create",
                "Build auth system",
                "--brain-dump",
                "I want login and logout",
                "--boundary",
                "only touch src/auth/",
                "--success-condition",
                "login returns 200",
                "--risk",
                "high",
            ]
        )
        assert rc == 0
        # Verify the intent ticket was created
        ticket = tickets_mod.load_ticket(repo, "INTENT-001")
        assert ticket["kind"] == "intent"
        assert ticket["brain_dump"] == "I want login and logout"
        assert ticket["boundary"] == "only touch src/auth/"
        assert ticket["success_condition"] == "login returns 200"
        assert ticket["risk"] == "high"
        assert ticket["title"] == "Build auth system"

    def test_intent_create_auto_increments_id(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_intent(repo, intent_id="INTENT-001")
        rc = cli_main(
            [
                "--repo",
                str(repo),
                "--format",
                "json",
                "intent-create",
                "Second intent",
                "--brain-dump",
                "Another brain dump",
            ]
        )
        assert rc == 0
        ticket = tickets_mod.load_ticket(repo, "INTENT-002")
        assert ticket["kind"] == "intent"

    def test_intent_create_with_scope(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        rc = cli_main(
            [
                "--repo",
                str(repo),
                "--format",
                "json",
                "intent-create",
                "Scoped intent",
                "--brain-dump",
                "Only allow certain files",
                "--scope-allow",
                "src/**",
                "--scope-deny",
                "src/vendor/**",
                "--max-files",
                "5",
                "--max-loc",
                "200",
            ]
        )
        assert rc == 0
        ticket = tickets_mod.load_ticket(repo, "INTENT-001")
        assert ticket["scope"]["allow"] == ["src/**"]
        assert ticket["scope"]["deny"] == ["src/vendor/**"]
        assert ticket["budgets"]["max_files_changed"] == 5
        assert ticket["budgets"]["max_loc_changed"] == 200


class TestTicketCreate:
    def test_ticket_create_task_under_intent(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_intent(repo, intent_id="INTENT-001")
        rc = cli_main(
            [
                "--repo",
                str(repo),
                "--format",
                "json",
                "ticket-create",
                "Implement login endpoint",
                "--kind",
                "task",
                "--parent",
                "INTENT-001",
            ]
        )
        assert rc == 0
        ticket = tickets_mod.load_ticket(repo, "TICKET-001")
        assert ticket["kind"] == "task"
        assert ticket["parent_id"] == "INTENT-001"
        # Parent should have child wired
        parent = tickets_mod.load_ticket(repo, "INTENT-001")
        assert "TICKET-001" in parent.get("children", [])

    def test_ticket_create_epic_under_intent(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_intent(repo, intent_id="INTENT-001")
        rc = cli_main(
            [
                "--repo",
                str(repo),
                "--format",
                "json",
                "ticket-create",
                "Auth epic",
                "--kind",
                "epic",
                "--parent",
                "INTENT-001",
            ]
        )
        assert rc == 0
        ticket = tickets_mod.load_ticket(repo, "TICKET-001-EPIC")
        assert ticket["kind"] == "epic"
        assert ticket["parent_id"] == "INTENT-001"

    def test_ticket_create_task_under_epic(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_intent(repo, intent_id="INTENT-001")
        _seed_epic(repo, epic_id="TICKET-001-EPIC", parent_id="INTENT-001")
        rc = cli_main(
            [
                "--repo",
                str(repo),
                "--format",
                "json",
                "ticket-create",
                "Login handler",
                "--kind",
                "task",
                "--parent",
                "TICKET-001-EPIC",
            ]
        )
        assert rc == 0
        ticket = tickets_mod.load_ticket(repo, "TICKET-002")
        assert ticket["parent_id"] == "TICKET-001-EPIC"

    def test_ticket_create_rejects_epic_under_epic(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_intent(repo, intent_id="INTENT-001")
        _seed_epic(repo, epic_id="TICKET-001-EPIC", parent_id="INTENT-001")
        rc = cli_main(
            [
                "--repo",
                str(repo),
                "--format",
                "json",
                "ticket-create",
                "Nested epic",
                "--kind",
                "epic",
                "--parent",
                "TICKET-001-EPIC",
            ]
        )
        assert rc == 1  # Should fail — epic parent must be intent

    def test_ticket_create_rejects_task_under_task(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_intent(repo, intent_id="INTENT-001")
        _seed_task(repo, task_id="TICKET-001", parent_id="INTENT-001")
        rc = cli_main(
            [
                "--repo",
                str(repo),
                "--format",
                "json",
                "ticket-create",
                "Sub task",
                "--kind",
                "task",
                "--parent",
                "TICKET-001",
            ]
        )
        assert rc == 1  # Should fail — task parent must be intent or epic

    def test_ticket_create_rejects_missing_parent(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        rc = cli_main(
            [
                "--repo",
                str(repo),
                "--format",
                "json",
                "ticket-create",
                "Orphan task",
                "--kind",
                "task",
                "--parent",
                "INTENT-999",
            ]
        )
        assert rc == 1  # Parent doesn't exist

    def test_full_hierarchy_creation(self, tmp_path: Path) -> None:
        """Create intent → epic → task via CLI and validate the chain."""
        repo = _bootstrap_repo(tmp_path)

        # 1. Create intent
        cli_main(
            [
                "--repo",
                str(repo),
                "--format",
                "json",
                "intent-create",
                "Build payments",
                "--brain-dump",
                "Users need to pay for stuff",
                "--boundary",
                "only src/payments/",
                "--success-condition",
                "Stripe checkout works",
            ]
        )

        # 2. Create epic under intent
        cli_main(
            [
                "--repo",
                str(repo),
                "--format",
                "json",
                "ticket-create",
                "Payment integration epic",
                "--kind",
                "epic",
                "--parent",
                "INTENT-001",
            ]
        )

        # 3. Create task under epic
        cli_main(
            [
                "--repo",
                str(repo),
                "--format",
                "json",
                "ticket-create",
                "Add Stripe webhook handler",
                "--kind",
                "task",
                "--parent",
                "TICKET-001-EPIC",
            ]
        )

        # Validate hierarchy
        task = tickets_mod.load_ticket(repo, "TICKET-002")
        reasons = tickets_mod.validate_intent_hierarchy(repo, task)
        assert reasons == []

        # Resolve intent root from task
        root = tickets_mod.resolve_intent_root(repo, task)
        assert root is not None
        assert root["id"] == "INTENT-001"

        # Timeline shows the intent with epic as direct descendant
        timeline = build_intent_timeline(repo)
        assert len(timeline["intents"]) == 1
        assert timeline["intents"][0]["id"] == "INTENT-001"
        assert timeline["intents"][0]["descendant_count"] >= 1
        # Epic should have task nested in its children
        epic_desc = timeline["intents"][0]["descendants"][0]
        assert epic_desc["id"] == "TICKET-001-EPIC"
        assert len(epic_desc["children"]) == 1
        assert epic_desc["children"][0]["id"] == "TICKET-002"


# ──────────────────────────────────────────────
# Phase F: Audit Session (Lazy Auditor Defense)
# ──────────────────────────────────────────────

from exo.orchestrator import AgentSessionManager


def _setup_session_repo(tmp_path: Path, ticket_id: str = "TICKET-001") -> Path:
    """Bootstrap repo with ticket + lock, ready for session lifecycle."""
    repo = _bootstrap_repo(tmp_path)
    _seed_intent(repo, intent_id="INTENT-001")
    _seed_task(repo, task_id=ticket_id, parent_id="INTENT-001")
    tickets_mod.acquire_lock(repo, ticket_id, owner="test-actor")
    return repo


class TestAuditSession:
    def test_audit_session_starts_with_mode(self, tmp_path: Path) -> None:
        repo = _setup_session_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="anthropic",
            model="claude-opus-4",
            mode="audit",
        )
        session = data["session"]
        assert session["mode"] == "audit"
        assert data["reused"] is False

    def test_audit_session_context_isolation(self, tmp_path: Path) -> None:
        """Audit bootstrap should deny .exo/cache/** and .exo/memory/**."""
        repo = _setup_session_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="anthropic",
            model="claude-opus-4",
            mode="audit",
        )
        bootstrap = data["bootstrap_prompt"]
        assert ".exo/cache/**" in bootstrap
        assert ".exo/memory/**" in bootstrap
        # Prior session memento should NOT appear in audit mode
        assert "## Prior Session Memento" not in bootstrap

    def test_audit_session_adversarial_directives(self, tmp_path: Path) -> None:
        """Audit bootstrap should contain adversarial audit directives."""
        repo = _setup_session_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="anthropic",
            model="claude-opus-4",
            mode="audit",
        )
        bootstrap = data["bootstrap_prompt"]
        assert "## Audit Directives" in bootstrap
        assert "Red Team Auditor" in bootstrap
        assert "PROOF" in bootstrap

    def test_audit_session_custom_persona(self, tmp_path: Path) -> None:
        """If .exo/audit_persona.md exists, its content is injected."""
        repo = _setup_session_repo(tmp_path)
        persona_text = "You are a quant auditor. Find lookahead bias or fail."
        (repo / ".exo" / "audit_persona.md").write_text(persona_text, encoding="utf-8")
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="anthropic",
            model="claude-opus-4",
            mode="audit",
        )
        bootstrap = data["bootstrap_prompt"]
        assert "quant auditor" in bootstrap
        assert "lookahead bias" in bootstrap

    def test_audit_session_writing_session_lookup(self, tmp_path: Path) -> None:
        """Audit session records the writing session's model for mismatch detection."""
        repo = _setup_session_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        # First: run a writing session
        mgr.start(
            ticket_id="TICKET-001",
            vendor="anthropic",
            model="claude-sonnet-4",
            mode="work",
        )
        mgr.finish(
            summary="Implemented login endpoint",
            ticket_id="TICKET-001",
            set_status="keep",
            skip_check=True,
            break_glass_reason="test",
            release_lock=False,
        )
        # Now start audit session
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="audit",
        )
        session = data["session"]
        ws = session.get("writing_session")
        assert ws is not None
        assert ws["model"] == "claude-sonnet-4"
        assert ws["vendor"] == "anthropic"

    def test_audit_finish_warns_no_artifacts(self, tmp_path: Path) -> None:
        """Finishing an audit session with no artifacts triggers a warning."""
        repo = _setup_session_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        mgr.start(
            ticket_id="TICKET-001",
            vendor="anthropic",
            model="claude-opus-4",
            mode="audit",
        )
        result = mgr.finish(
            summary="Checked the code, looks fine",
            ticket_id="TICKET-001",
            set_status="keep",
            skip_check=True,
            break_glass_reason="test",
            release_lock=False,
            artifacts=[],
        )
        assert "audit_warnings" in result
        warnings = result["audit_warnings"]
        assert any("no artifacts" in w for w in warnings)

    def test_audit_finish_warns_same_model(self, tmp_path: Path) -> None:
        """Finishing an audit session with same model as writer triggers a warning."""
        repo = _setup_session_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        # Writing session
        mgr.start(
            ticket_id="TICKET-001",
            vendor="anthropic",
            model="claude-sonnet-4",
            mode="work",
        )
        mgr.finish(
            summary="Built feature",
            ticket_id="TICKET-001",
            set_status="keep",
            skip_check=True,
            break_glass_reason="test",
            release_lock=False,
        )
        # Audit session with SAME model
        mgr.start(
            ticket_id="TICKET-001",
            vendor="anthropic",
            model="claude-sonnet-4",
            mode="audit",
        )
        result = mgr.finish(
            summary="Audited the code",
            ticket_id="TICKET-001",
            set_status="keep",
            skip_check=True,
            break_glass_reason="test",
            release_lock=False,
            artifacts=["test_audit_drift.py"],
        )
        assert "audit_warnings" in result
        warnings = result["audit_warnings"]
        assert any("matches writing model" in w for w in warnings)

    def test_audit_finish_no_warnings_when_clean(self, tmp_path: Path) -> None:
        """Clean audit: different model + artifacts = no warnings."""
        repo = _setup_session_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        # Writing session
        mgr.start(
            ticket_id="TICKET-001",
            vendor="anthropic",
            model="claude-sonnet-4",
            mode="work",
        )
        mgr.finish(
            summary="Built feature",
            ticket_id="TICKET-001",
            set_status="keep",
            skip_check=True,
            break_glass_reason="test",
            release_lock=False,
        )
        # Audit with DIFFERENT model + artifacts
        mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="audit",
        )
        result = mgr.finish(
            summary="Found no issues, wrote proof",
            ticket_id="TICKET-001",
            set_status="keep",
            skip_check=True,
            break_glass_reason="test",
            release_lock=False,
            artifacts=["test_audit_no_drift.py"],
        )
        assert "audit_warnings" not in result

    def test_audit_via_cli(self, tmp_path: Path, monkeypatch: Any) -> None:
        """Test session-audit CLI command."""
        repo = _setup_session_repo(tmp_path)
        monkeypatch.setenv("EXO_ACTOR", "test-actor")
        rc = cli_main(
            [
                "--repo",
                str(repo),
                "--format",
                "json",
                "session-audit",
                "--ticket-id",
                "TICKET-001",
                "--vendor",
                "openai",
                "--model",
                "o1-preview",
            ]
        )
        assert rc == 0

    def test_audit_mode_invalid_rejected(self, tmp_path: Path) -> None:
        """Invalid mode is rejected."""
        repo = _setup_session_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        try:
            mgr.start(
                ticket_id="TICKET-001",
                vendor="anthropic",
                model="test",
                mode="invalid",
            )
            assert False, "Should have raised ExoError"
        except Exception as e:
            assert "SESSION_MODE_INVALID" in str(e)


# ---------- Phase G: Exo Mode Banner ----------
from exo.orchestrator.session import _exo_banner, EXO_PROTOCOL_VERSION


class TestExoBanner:
    """Tests for the ExoProtocol governance banner strip."""

    def test_banner_start_work_mode(self) -> None:
        """Work mode start banner contains key governance indicators."""
        banner = _exo_banner(
            event="start",
            mode="work",
            ticket_id="TICKET-001",
            actor="human",
            model="claude-opus-4",
            session_id="SES-TEST",
        )
        assert "EXO GOVERNED SESSION" in banner
        assert "ExoProtocol" in banner
        assert EXO_PROTOCOL_VERSION in banner
        assert "mode: work" in banner
        assert "TICKET-001" in banner
        assert "human" in banner
        assert "claude-opus-4" in banner
        # Box drawing characters
        assert "\u2554" in banner  # ╔
        assert "\u255a" in banner  # ╚

    def test_banner_start_audit_mode(self) -> None:
        """Audit mode start banner says AUDIT, not GOVERNED."""
        banner = _exo_banner(
            event="start",
            mode="audit",
            ticket_id="TICKET-002",
            actor="auditor",
            model="o1-preview",
        )
        assert "EXO AUDIT SESSION" in banner
        assert "mode: audit" in banner
        assert "TICKET-002" in banner

    def test_banner_finish_includes_verify(self) -> None:
        """Finish banner shows verify status."""
        banner = _exo_banner(
            event="finish",
            mode="work",
            ticket_id="TICKET-001",
            verify="passed",
        )
        assert "EXO SESSION COMPLETE" in banner
        assert "verify: passed" in banner
        assert "TICKET-001" in banner

    def test_banner_finish_includes_drift(self) -> None:
        """Finish banner shows drift score when present."""
        banner = _exo_banner(
            event="finish",
            mode="work",
            ticket_id="TICKET-001",
            verify="passed",
            drift_score=0.42,
        )
        assert "drift: 0.42" in banner

    def test_banner_finish_audit_with_warnings(self) -> None:
        """Audit finish banner shows warnings."""
        banner = _exo_banner(
            event="finish",
            mode="audit",
            ticket_id="TICKET-001",
            verify="passed",
            audit_warnings=["no artifacts produced"],
        )
        assert "EXO AUDIT COMPLETE" in banner
        assert "no artifacts produced" in banner

    def test_banner_resume(self) -> None:
        """Resume banner says RESUMED."""
        banner = _exo_banner(
            event="resume",
            ticket_id="TICKET-001",
            actor="human",
            model="claude-opus-4",
        )
        assert "EXO SESSION RESUMED" in banner
        assert "TICKET-001" in banner

    def test_banner_in_bootstrap_prompt(self, tmp_path: Path) -> None:
        """Session start bootstrap prompt begins with the banner."""
        repo = _setup_session_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="anthropic",
            model="claude-opus-4",
        )
        bootstrap = data["bootstrap_prompt"]
        # Banner should be at the very top
        assert bootstrap.startswith("\u2554")  # ╔
        assert "EXO GOVERNED SESSION" in bootstrap
        assert "ExoProtocol" in bootstrap

    def test_banner_in_return_dict(self, tmp_path: Path) -> None:
        """Session start returns exo_banner field."""
        repo = _setup_session_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="anthropic",
            model="claude-opus-4",
        )
        assert "exo_banner" in data
        assert "EXO GOVERNED SESSION" in data["exo_banner"]

    def test_banner_in_finish_return(self, tmp_path: Path) -> None:
        """Session finish returns exo_banner field."""
        repo = _setup_session_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        mgr.start(
            ticket_id="TICKET-001",
            vendor="anthropic",
            model="claude-opus-4",
        )
        result = mgr.finish(
            summary="test complete",
            ticket_id="TICKET-001",
        )
        assert "exo_banner" in result
        assert "EXO SESSION COMPLETE" in result["exo_banner"]

    def test_banner_model_hidden_when_unknown(self) -> None:
        """Model line is omitted when model is 'unknown'."""
        banner = _exo_banner(
            event="start",
            mode="work",
            ticket_id="TICKET-001",
            actor="human",
            model="unknown",
        )
        assert "model:" not in banner
