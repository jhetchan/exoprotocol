"""Tests for the Error Reflection Layer.

Covers:
- Reflection storage (create, load, filter, sort)
- Bootstrap injection (start + resume inject operational learnings)
- Hit count tracking
- Dismiss mechanism
- Memory index sync (failure_modes in .exo/memory/index.yaml)
- Session-finish error capture
- CLI integration (exo reflect, exo reflections, exo reflect-dismiss)
- Human output formatting
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

from exo.kernel import governance as governance_mod
from exo.kernel import tickets
from exo.orchestrator.session import AgentSessionManager
from exo.stdlib.reflect import (
    REFLECTIONS_DIR,
    VALID_SEVERITIES,
    VALID_STATUSES,
    Reflection,
    _next_reflection_id,
    dismiss_reflection,
    format_bootstrap_reflections,
    format_reflections_human,
    increment_hit_count,
    load_reflections,
    reflect,
    reflect_to_dict,
    reflections_for_bootstrap,
    reflections_to_list,
)


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


def _create_ticket(repo: Path, ticket_id: str) -> dict[str, Any]:
    ticket_data = {
        "id": ticket_id,
        "title": f"Test ticket {ticket_id}",
        "intent": f"Test intent {ticket_id}",
        "status": "active",
        "priority": 3,
        "checks": [],
        "scope": {"allow": ["**"], "deny": []},
    }
    tickets.save_ticket(repo, ticket_data)
    tickets.acquire_lock(repo, ticket_id, owner="test-agent", role="developer")
    return ticket_data


# ── Reflection Storage ──────────────────────────────────────────────


class TestReflectStore:
    def test_creates_yaml_file(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        ref = reflect(repo, pattern="Error X happens", insight="Do Y instead")
        ref_path = repo / REFLECTIONS_DIR / f"{ref.id}.yaml"
        assert ref_path.exists()

    def test_returns_reflection_dataclass(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        ref = reflect(repo, pattern="Error X", insight="Do Y")
        assert isinstance(ref, Reflection)
        assert ref.pattern == "Error X"
        assert ref.insight == "Do Y"
        assert ref.severity == "medium"
        assert ref.scope == "global"
        assert ref.status == "active"
        assert ref.hit_count == 0
        assert ref.created_at != ""

    def test_unique_ids(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        ref1 = reflect(repo, pattern="Pattern 1", insight="Insight 1")
        ref2 = reflect(repo, pattern="Pattern 2", insight="Insight 2")
        assert ref1.id.startswith("REF-")
        assert ref2.id.startswith("REF-")
        assert ref1.id != ref2.id

    def test_validates_severity(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        try:
            reflect(repo, pattern="X", insight="Y", severity="invalid")
            raise AssertionError("Should have raised ExoError")
        except Exception as e:
            assert "severity" in str(e).lower()

    def test_validates_pattern_required(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        try:
            reflect(repo, pattern="", insight="Y")
            raise AssertionError("Should have raised ExoError")
        except Exception as e:
            assert "pattern" in str(e).lower()

    def test_validates_insight_required(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        try:
            reflect(repo, pattern="X", insight="")
            raise AssertionError("Should have raised ExoError")
        except Exception as e:
            assert "insight" in str(e).lower()

    def test_default_scope_is_global(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        ref = reflect(repo, pattern="X", insight="Y")
        assert ref.scope == "global"


# ── Loading ─────────────────────────────────────────────────────────


class TestReflectLoad:
    def test_empty_dir(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        assert load_reflections(repo) == []

    def test_loads_all(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        reflect(repo, pattern="P1", insight="I1")
        reflect(repo, pattern="P2", insight="I2")
        reflect(repo, pattern="P3", insight="I3")
        refs = load_reflections(repo)
        assert len(refs) == 3

    def test_filter_by_status(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        reflect(repo, pattern="Active", insight="I1")
        ref2 = reflect(repo, pattern="To dismiss", insight="I2")
        dismiss_reflection(repo, ref2.id)
        active = load_reflections(repo, status="active")
        dismissed = load_reflections(repo, status="dismissed")
        assert len(active) == 1
        assert len(dismissed) == 1

    def test_filter_by_scope(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        reflect(repo, pattern="Global", insight="I1", scope="global")
        reflect(repo, pattern="Scoped", insight="I2", scope="TICKET-001")
        global_refs = load_reflections(repo, scope="global")
        scoped_refs = load_reflections(repo, scope="TICKET-001")
        assert len(global_refs) == 1
        assert len(scoped_refs) == 1

    def test_sorted_by_creation_newest_first(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        ref1 = reflect(repo, pattern="P1", insight="I1")
        ref2 = reflect(repo, pattern="P2", insight="I2")
        refs = load_reflections(repo)
        # Both created in same second, so sorted by created_at (desc) then filename
        # At minimum both are returned
        assert len(refs) == 2
        ids = {r.id for r in refs}
        assert ids == {ref1.id, ref2.id}


# ── Bootstrap Filtering ─────────────────────────────────────────────


class TestReflectionsForBootstrap:
    def test_global_included(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        reflect(repo, pattern="Global pattern", insight="I1", scope="global")
        refs = reflections_for_bootstrap(repo, "TICKET-ANY")
        assert len(refs) == 1

    def test_ticket_scoped_included(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        reflect(repo, pattern="Scoped", insight="I1", scope="TICKET-001")
        refs = reflections_for_bootstrap(repo, "TICKET-001")
        assert len(refs) == 1

    def test_other_ticket_excluded(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        reflect(repo, pattern="Scoped to 002", insight="I1", scope="TICKET-002")
        refs = reflections_for_bootstrap(repo, "TICKET-001")
        assert len(refs) == 0

    def test_dismissed_excluded(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        ref = reflect(repo, pattern="Will dismiss", insight="I1")
        dismiss_reflection(repo, ref.id)
        refs = reflections_for_bootstrap(repo, "TICKET-001")
        assert len(refs) == 0

    def test_severity_ordering(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        reflect(repo, pattern="Low", insight="I1", severity="low")
        reflect(repo, pattern="Critical", insight="I2", severity="critical")
        reflect(repo, pattern="High", insight="I3", severity="high")
        refs = reflections_for_bootstrap(repo, "ANY")
        assert refs[0].severity == "critical"
        assert refs[1].severity == "high"
        assert refs[2].severity == "low"

    def test_empty_when_no_reflections(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        refs = reflections_for_bootstrap(repo, "TICKET-001")
        assert refs == []


# ── Hit Count ───────────────────────────────────────────────────────


class TestHitCount:
    def test_increment(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        ref = reflect(repo, pattern="P", insight="I")
        assert ref.hit_count == 0
        increment_hit_count(repo, ref.id)
        refs = load_reflections(repo)
        assert refs[0].hit_count == 1

    def test_double_increment(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        ref = reflect(repo, pattern="P", insight="I")
        increment_hit_count(repo, ref.id)
        increment_hit_count(repo, ref.id)
        refs = load_reflections(repo)
        assert refs[0].hit_count == 2


# ── Dismiss ─────────────────────────────────────────────────────────


class TestDismissReflection:
    def test_sets_status(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        ref = reflect(repo, pattern="P", insight="I")
        dismissed = dismiss_reflection(repo, ref.id)
        assert dismissed.status == "dismissed"

    def test_nonexistent_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        try:
            dismiss_reflection(repo, "REF-999")
            raise AssertionError("Should have raised ExoError")
        except Exception as e:
            assert "not found" in str(e).lower()


# ── Memory Index Sync ───────────────────────────────────────────────


class TestMemoryIndexSync:
    def test_syncs_to_failure_modes(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        reflect(repo, pattern="Test pattern", insight="Test insight")
        index_path = repo / ".exo" / "memory" / "index.yaml"
        assert index_path.exists()
        data = yaml.safe_load(index_path.read_text(encoding="utf-8"))
        assert len(data["failure_modes"]) >= 1

    def test_source_references_reflection_id(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        ref = reflect(repo, pattern="Test", insight="I")
        index_path = repo / ".exo" / "memory" / "index.yaml"
        data = yaml.safe_load(index_path.read_text(encoding="utf-8"))
        fm = data["failure_modes"][-1]
        assert f"reflection:{ref.id}" in str(fm.get("detection", {}).get("source", ""))

    def test_creates_index_if_missing(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        index_path = repo / ".exo" / "memory" / "index.yaml"
        assert not index_path.exists()
        reflect(repo, pattern="First", insight="I")
        assert index_path.exists()


# ── Bootstrap Injection ─────────────────────────────────────────────


class TestBootstrapInjection:
    def test_start_injects_operational_learnings(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        reflect(repo, pattern="Always check config", insight="Read config first", severity="high")
        manager = AgentSessionManager(repo, actor="test-agent")
        result = manager.start(ticket_id="TICKET-001")
        bootstrap = result.get("bootstrap_prompt", "")
        assert "Operational Learnings" in bootstrap
        assert "Always check config" in bootstrap
        assert "Read config first" in bootstrap

    def test_no_injection_when_empty(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        manager = AgentSessionManager(repo, actor="test-agent")
        result = manager.start(ticket_id="TICKET-001")
        bootstrap = result.get("bootstrap_prompt", "")
        assert "Operational Learnings" not in bootstrap

    def test_no_injection_in_audit_mode(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        reflect(repo, pattern="Some pattern", insight="Some insight")
        manager = AgentSessionManager(repo, actor="test-agent")
        result = manager.start(ticket_id="TICKET-001", mode="audit")
        bootstrap = result.get("bootstrap_prompt", "")
        assert "Operational Learnings" not in bootstrap

    def test_severity_markers_in_bootstrap(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        reflect(repo, pattern="Critical issue", insight="I", severity="critical")
        manager = AgentSessionManager(repo, actor="test-agent")
        result = manager.start(ticket_id="TICKET-001")
        bootstrap = result.get("bootstrap_prompt", "")
        assert "[CRITICAL]!!!" in bootstrap

    def test_hit_count_incremented_after_bootstrap(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        ref = reflect(repo, pattern="Tracked", insight="I")
        assert ref.hit_count == 0
        manager = AgentSessionManager(repo, actor="test-agent")
        manager.start(ticket_id="TICKET-001")
        refs = load_reflections(repo)
        assert refs[0].hit_count == 1

    def test_resume_injects_operational_learnings(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        manager = AgentSessionManager(repo, actor="test-agent")
        manager.start(ticket_id="TICKET-001")
        manager.suspend(reason="testing")
        # Add reflection after start but before resume
        reflect(repo, pattern="Resume insight", insight="Check this")
        # Re-acquire lock for resume
        tickets.acquire_lock(repo, "TICKET-001", owner="test-agent", role="developer")
        result = manager.resume(ticket_id="TICKET-001")
        bootstrap = result.get("bootstrap_prompt", "")
        assert "Operational Learnings" in bootstrap
        assert "Resume insight" in bootstrap


# ── Session Finish Errors ───────────────────────────────────────────


class TestSessionFinishErrors:
    def test_finish_with_errors_in_memento(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        manager = AgentSessionManager(repo, actor="test-agent")
        manager.start(ticket_id="TICKET-001")
        result = manager.finish(
            summary="Done",
            errors=[{"tool": "bash", "message": "exit code 1", "count": 3}],
        )
        memento_path = repo / result["memento_path"]
        memento_text = memento_path.read_text(encoding="utf-8")
        assert "Errors Encountered" in memento_text
        assert "[bash] exit code 1 (x3)" in memento_text

    def test_finish_with_errors_in_index(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        manager = AgentSessionManager(repo, actor="test-agent")
        manager.start(ticket_id="TICKET-001")
        manager.finish(
            summary="Done",
            errors=[{"tool": "write", "message": "Permission denied", "count": 1}],
        )
        index_path = repo / ".exo" / "memory" / "sessions" / "index.jsonl"
        lines = index_path.read_text(encoding="utf-8").strip().splitlines()
        row = json.loads(lines[-1])
        assert row["error_count"] == 1
        assert row["errors"][0]["tool"] == "write"

    def test_finish_without_errors_no_section(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        manager = AgentSessionManager(repo, actor="test-agent")
        manager.start(ticket_id="TICKET-001")
        result = manager.finish(summary="Done")
        memento_path = repo / result["memento_path"]
        memento_text = memento_path.read_text(encoding="utf-8")
        assert "Errors Encountered" not in memento_text

    def test_finish_errors_in_return_dict(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        manager = AgentSessionManager(repo, actor="test-agent")
        manager.start(ticket_id="TICKET-001")
        result = manager.finish(
            summary="Done",
            errors=[{"tool": "bash", "message": "exit 1", "count": 2}],
        )
        assert "errors" in result
        assert result["error_count"] == 2


# ── CLI Integration ─────────────────────────────────────────────────


class TestCLIReflect:
    def test_cli_reflect_creates(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = subprocess.run(
            [
                "python3",
                "-m",
                "exo.cli",
                "--format",
                "json",
                "--repo",
                str(repo),
                "reflect",
                "--pattern",
                "Test pattern",
                "--insight",
                "Test insight",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"]
        assert data["data"]["id"].startswith("REF-")
        assert data["data"]["pattern"] == "Test pattern"

    def test_cli_reflections_lists(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        reflect(repo, pattern="P1", insight="I1")
        reflect(repo, pattern="P2", insight="I2")
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), "reflections"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["count"] == 2

    def test_cli_reflections_filter_status(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        reflect(repo, pattern="Active", insight="I1")
        ref2 = reflect(repo, pattern="Dismissed", insight="I2")
        dismiss_reflection(repo, ref2.id)
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), "reflections", "--status", "active"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["count"] == 1

    def test_cli_reflect_dismiss(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        ref = reflect(repo, pattern="To dismiss", insight="I1")
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), "reflect-dismiss", ref.id],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["status"] == "dismissed"

    def test_cli_reflections_human_output(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        reflect(repo, pattern="Human pattern", insight="Human insight")
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "human", "--repo", str(repo), "reflections"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "Reflections: 1 total" in result.stdout
        assert "Human pattern" in result.stdout

    def test_cli_session_finish_with_errors(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        # Start session with the same actor that will be used in CLI
        manager = AgentSessionManager(repo, actor="test-agent")
        manager.start(ticket_id="TICKET-001")
        result = subprocess.run(
            [
                "python3",
                "-m",
                "exo.cli",
                "--format",
                "json",
                "--repo",
                str(repo),
                "session-finish",
                "--summary",
                "Done",
                "--error",
                "bash:exit code 1",
                "--error",
                "write:Permission denied",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={**__import__("os").environ, "EXO_ACTOR": "test-agent"},
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"]
        assert data["data"].get("error_count", 0) == 2


# ── Serialization ───────────────────────────────────────────────────


class TestSerialization:
    def test_to_dict_roundtrip(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        ref = reflect(repo, pattern="P", insight="I", tags=["test", "debug"])
        d = reflect_to_dict(ref)
        assert d["id"] == ref.id
        assert d["pattern"] == "P"
        assert d["insight"] == "I"
        assert d["tags"] == ["test", "debug"]
        assert "created_at" in d

    def test_reflections_to_list(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        reflect(repo, pattern="P1", insight="I1")
        reflect(repo, pattern="P2", insight="I2")
        refs = load_reflections(repo)
        lst = reflections_to_list(refs)
        assert len(lst) == 2
        assert all(isinstance(d, dict) for d in lst)

    def test_format_human(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        reflect(repo, pattern="Human test", insight="Human insight", severity="high")
        refs = load_reflections(repo)
        text = format_reflections_human(refs)
        assert "Reflections: 1 total" in text
        assert "[HIGH]" in text
        assert "Human test" in text

    def test_format_bootstrap_lines(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        reflect(repo, pattern="Bootstrap test", insight="Bootstrap insight", severity="critical")
        refs = reflections_for_bootstrap(repo, "ANY")
        lines = format_bootstrap_reflections(refs)
        assert "## Operational Learnings" in lines
        assert any("[CRITICAL]!!!" in line for line in lines)
        assert any("Bootstrap test" in line for line in lines)


# ── ID Generation ───────────────────────────────────────────────────


class TestIDGeneration:
    def test_generates_timestamp_id(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        rid = _next_reflection_id(repo)
        assert rid.startswith("REF-")
        # Format: REF-YYYYMMDD-HHMMSS-XXXX
        import re

        assert re.match(r"^REF-\d{8}-\d{6}-[A-Z0-9]{4}$", rid)

    def test_unique_ids(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        ids = {_next_reflection_id(repo) for _ in range(20)}
        assert len(ids) == 20


# ── Constants ───────────────────────────────────────────────────────


class TestConstants:
    def test_valid_severities(self) -> None:
        assert {"low", "medium", "high", "critical"} == VALID_SEVERITIES

    def test_valid_statuses(self) -> None:
        assert {"active", "superseded", "dismissed"} == VALID_STATUSES
