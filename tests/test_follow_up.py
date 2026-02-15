"""Tests for the chain reaction follow-up ticket engine.

Covers:
- FollowUpTicket and FollowUpReport dataclasses
- Detection rules: uncovered_code, unbound_feature, high drift, uncovered_req, tool awareness
- Ticket creation: parent linkage, dedup, max cap
- Serialization and human formatting
- CLI integration
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from exo.kernel import governance as governance_mod
from exo.kernel import tickets as tickets_mod
from exo.stdlib.follow_up import (
    FollowUpReport,
    FollowUpTicket,
    create_follow_ups,
    detect_follow_ups,
    follow_up_to_dict,
    format_follow_ups_human,
    report_to_dict,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    # Create tickets dir
    (exo_dir / "tickets").mkdir(parents=True, exist_ok=True)
    return repo


@dataclass
class MockViolation:
    kind: str
    feature_id: str = ""
    file: str = ""
    line: int | None = None
    message: str = ""
    severity: str = "warning"


@dataclass
class MockTraceReport:
    violations: list[MockViolation]
    passed: bool = True


@dataclass
class MockReqTraceReport:
    violations: list[MockViolation]
    passed: bool = True


# ===========================================================================
# TestFollowUpTicket
# ===========================================================================


class TestFollowUpTicket:
    def test_create_follow_up_ticket(self) -> None:
        fu = FollowUpTicket(
            title="Fix this",
            kind="task",
            rationale="Because it's broken",
            labels=("governance",),
            source="test",
            severity="medium",
        )
        assert fu.title == "Fix this"
        assert fu.kind == "task"
        assert fu.severity == "medium"

    def test_frozen(self) -> None:
        fu = FollowUpTicket(title="T", kind="task", rationale="R", labels=(), source="test", severity="low")
        try:
            fu.title = "new"  # type: ignore[misc]
            raise AssertionError("should be frozen")
        except AttributeError:
            pass

    def test_fields(self) -> None:
        fu = FollowUpTicket(
            title="A",
            kind="epic",
            rationale="B",
            labels=("a", "b"),
            source="drift_detection",
            severity="high",
        )
        assert fu.labels == ("a", "b")
        assert fu.source == "drift_detection"


# ===========================================================================
# TestDetectFollowUps
# ===========================================================================


class TestDetectFollowUps:
    def test_no_inputs_no_follow_ups(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = detect_follow_ups(repo)
        assert result == []

    def test_uncovered_code_triggers_follow_up(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        mock_trace = MockTraceReport(violations=[MockViolation(kind="uncovered_code", file="src/orphan.py")])
        result = detect_follow_ups(repo, trace_report=mock_trace)
        assert len(result) == 1
        assert result[0].source == "feature_trace"
        assert "uncovered" in result[0].title.lower()

    def test_unbound_feature_triggers_follow_up(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        mock_trace = MockTraceReport(violations=[MockViolation(kind="unbound_feature", feature_id="auth")])
        result = detect_follow_ups(repo, trace_report=mock_trace)
        assert len(result) == 1
        assert "unbound" in result[0].title.lower()

    def test_high_drift_triggers_follow_up(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = detect_follow_ups(repo, drift_data={"drift_score": 0.85})
        assert len(result) == 1
        assert result[0].severity == "high"
        assert "drift" in result[0].title.lower()

    def test_low_drift_no_follow_up(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = detect_follow_ups(repo, drift_data={"drift_score": 0.3})
        assert result == []

    def test_uncovered_req_triggers_follow_up(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        mock_req = MockReqTraceReport(violations=[MockViolation(kind="uncovered_req")])
        result = detect_follow_ups(repo, req_trace_report=mock_req)
        assert len(result) == 1
        assert "req" in result[0].title.lower()

    def test_tools_created_not_used_triggers_follow_up(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = detect_follow_ups(
            repo,
            tools_summary={"tools_created": 2, "tools_used": 0, "total_tools": 5},
        )
        assert len(result) == 1
        assert result[0].severity == "low"
        assert "tool" in result[0].title.lower()

    def test_tools_used_no_follow_up(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = detect_follow_ups(
            repo,
            tools_summary={"tools_created": 1, "tools_used": 1, "total_tools": 5},
        )
        assert result == []

    def test_combined_triggers(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        mock_trace = MockTraceReport(
            violations=[
                MockViolation(kind="uncovered_code"),
                MockViolation(kind="unbound_feature"),
            ]
        )
        result = detect_follow_ups(
            repo,
            trace_report=mock_trace,
            drift_data={"drift_score": 0.9},
        )
        assert len(result) == 3  # uncovered + unbound + drift


# ===========================================================================
# TestCreateFollowUps
# ===========================================================================


class TestCreateFollowUps:
    def test_basic_creation(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        # Create parent ticket
        parent_id = tickets_mod.allocate_ticket_id(repo, kind="task")
        tickets_mod.save_ticket(repo, {"id": parent_id, "title": "Parent task"})

        fu_list = [
            FollowUpTicket(
                title="Fix uncovered code",
                kind="task",
                rationale="3 uncovered files",
                labels=("governance",),
                source="feature_trace",
                severity="medium",
            )
        ]
        report = create_follow_ups(repo, parent_ticket_id=parent_id, follow_ups=fu_list)
        assert len(report.created_ids) == 1
        assert report.skipped == 0

        # Verify ticket was actually created
        created = tickets_mod.load_ticket(repo, report.created_ids[0])
        assert created["parent_id"] == parent_id
        assert created["title"] == "Fix uncovered code"

    def test_parent_linkage(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        parent_id = tickets_mod.allocate_ticket_id(repo, kind="task")
        tickets_mod.save_ticket(repo, {"id": parent_id, "title": "Parent"})

        fu_list = [
            FollowUpTicket(
                title="Follow-up task",
                kind="task",
                rationale="R",
                labels=(),
                source="test",
                severity="low",
            )
        ]
        report = create_follow_ups(repo, parent_ticket_id=parent_id, follow_ups=fu_list)
        created = tickets_mod.load_ticket(repo, report.created_ids[0])
        assert created["parent_id"] == parent_id

    def test_dedup_skips_existing(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        parent_id = tickets_mod.allocate_ticket_id(repo, kind="task")
        tickets_mod.save_ticket(repo, {"id": parent_id, "title": "Parent"})

        # Create an existing ticket with same title under same parent
        dup_id = tickets_mod.allocate_ticket_id(repo, kind="task")
        tickets_mod.save_ticket(
            repo,
            {
                "id": dup_id,
                "title": "Fix uncovered code",
                "parent_id": parent_id,
                "status": "todo",
            },
        )

        fu_list = [
            FollowUpTicket(
                title="Fix uncovered code",
                kind="task",
                rationale="R",
                labels=(),
                source="test",
                severity="medium",
            )
        ]
        report = create_follow_ups(repo, parent_ticket_id=parent_id, follow_ups=fu_list)
        assert len(report.created_ids) == 0
        assert report.skipped == 1

    def test_max_cap(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        parent_id = tickets_mod.allocate_ticket_id(repo, kind="task")
        tickets_mod.save_ticket(repo, {"id": parent_id, "title": "Parent"})

        fu_list = [
            FollowUpTicket(
                title=f"Task {i}",
                kind="task",
                rationale="R",
                labels=(),
                source="test",
                severity="medium",
            )
            for i in range(10)
        ]
        report = create_follow_ups(repo, parent_ticket_id=parent_id, follow_ups=fu_list, max_per_session=3)
        assert len(report.created_ids) == 3
        assert report.skipped >= 1

    def test_priority_from_severity(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        parent_id = tickets_mod.allocate_ticket_id(repo, kind="task")
        tickets_mod.save_ticket(repo, {"id": parent_id, "title": "Parent"})

        fu_list = [
            FollowUpTicket(
                title="High prio",
                kind="task",
                rationale="R",
                labels=(),
                source="test",
                severity="high",
            ),
            FollowUpTicket(
                title="Low prio",
                kind="task",
                rationale="R",
                labels=(),
                source="test",
                severity="low",
            ),
        ]
        report = create_follow_ups(repo, parent_ticket_id=parent_id, follow_ups=fu_list)
        high_ticket = tickets_mod.load_ticket(repo, report.created_ids[0])
        low_ticket = tickets_mod.load_ticket(repo, report.created_ids[1])
        assert high_ticket["priority"] == 2  # high → p2
        assert low_ticket["priority"] == 4  # low → p4


# ===========================================================================
# TestFollowUpReport
# ===========================================================================


class TestFollowUpReport:
    def test_report_to_dict(self) -> None:
        fu = FollowUpTicket(title="T", kind="task", rationale="R", labels=("a",), source="test", severity="medium")
        report = FollowUpReport(detected=(fu,), created_ids=("TKT-001",), skipped=0)
        d = report_to_dict(report)
        assert d["detected_count"] == 1
        assert d["created_count"] == 1
        assert d["created_ids"] == ["TKT-001"]
        assert d["detected"][0]["title"] == "T"

    def test_follow_up_to_dict(self) -> None:
        fu = FollowUpTicket(
            title="Fix", kind="task", rationale="Because", labels=("gov",), source="trace", severity="high"
        )
        d = follow_up_to_dict(fu)
        assert d["title"] == "Fix"
        assert d["labels"] == ["gov"]
        assert d["severity"] == "high"

    def test_format_human_empty(self) -> None:
        report = FollowUpReport(detected=(), created_ids=(), skipped=0)
        text = format_follow_ups_human(report)
        assert "none" in text.lower()

    def test_format_human_with_detections(self) -> None:
        fu1 = FollowUpTicket(title="Fix A", kind="task", rationale="R1", labels=(), source="test", severity="high")
        fu2 = FollowUpTicket(title="Fix B", kind="task", rationale="R2", labels=(), source="test", severity="low")
        report = FollowUpReport(
            detected=(fu1, fu2),
            created_ids=("TKT-001", "TKT-002"),
            skipped=0,
        )
        text = format_follow_ups_human(report)
        assert "2 detected" in text
        assert "[HIGH]" in text
        assert "[LOW]" in text
        assert "TKT-001" in text

    def test_format_human_with_skipped(self) -> None:
        fu = FollowUpTicket(title="T", kind="task", rationale="R", labels=(), source="test", severity="medium")
        report = FollowUpReport(detected=(fu,), created_ids=(), skipped=1)
        text = format_follow_ups_human(report)
        assert "skipped" in text.lower()


# ===========================================================================
# TestCLI
# ===========================================================================


class TestCLIFollowUps:
    def _run_exo(self, repo: Path, *args: str) -> dict[str, Any]:
        import subprocess

        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"exo failed: {result.stderr}")
        return json.loads(result.stdout)

    def test_follow_ups_empty(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        # No features.yaml → no follow-ups
        result = self._run_exo(repo, "follow-ups")
        assert result["ok"] is True
        assert result["data"]["count"] == 0

    def test_follow_ups_with_uncovered(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        # Write features.yaml + uncovered source file
        import yaml

        features_path = repo / ".exo" / "features.yaml"
        features_path.write_text(
            yaml.dump({"features": [{"id": "core", "status": "active", "files": ["src/core.py"]}]}),
            encoding="utf-8",
        )
        src_dir = repo / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "core.py").write_text("# covered\n", encoding="utf-8")
        (src_dir / "orphan.py").write_text("def orphan(): pass\n", encoding="utf-8")

        result = self._run_exo(repo, "follow-ups")
        assert result["ok"] is True
        assert result["data"]["count"] >= 1
        sources = {fu["source"] for fu in result["data"]["follow_ups"]}
        assert "feature_trace" in sources

    def test_follow_ups_with_ticket_id(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = self._run_exo(repo, "follow-ups", "--ticket-id", "TKT-001")
        assert result["ok"] is True
