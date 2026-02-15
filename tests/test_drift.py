"""Tests for the Composite Governance Drift Detection.

Covers:
- Governance integrity check (constitution hash vs lock)
- Adapter freshness check (governance hash in adapter files)
- Feature traceability integration
- Requirement traceability integration
- Session health check
- Composite drift report (overall verdict)
- Skip flags
- CLI integration (exo drift)
- Human output formatting
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from exo.kernel import governance as governance_mod
from exo.stdlib.drift import (
    _check_adapters,
    _check_features,
    _check_governance,
    _check_requirements,
    _check_sessions,
    drift,
    drift_to_dict,
    fleet_drift,
    format_drift_human,
    format_fleet_drift_human,
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


def _write_features_yaml(repo: Path, features: list[dict[str, Any]]) -> None:
    import yaml

    (repo / ".exo" / "features.yaml").write_text(
        yaml.dump({"features": features}, default_flow_style=False),
        encoding="utf-8",
    )


def _write_requirements_yaml(repo: Path, requirements: list[dict[str, Any]]) -> None:
    import yaml

    (repo / ".exo" / "requirements.yaml").write_text(
        yaml.dump({"requirements": requirements}, default_flow_style=False),
        encoding="utf-8",
    )


def _write_source_file(repo: Path, rel_path: str, content: str) -> None:
    filepath = repo / rel_path
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(content, encoding="utf-8")


def _write_adapter(repo: Path, filename: str, governance_hash: str) -> None:
    """Write a mock adapter file with a governance hash marker."""
    filepath = repo / filename
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(
        f"# Governance hash: {governance_hash}\n# Auto-generated\n",
        encoding="utf-8",
    )


# ── Governance Integrity ─────────────────────────────────────────────


class TestCheckGovernance:
    def test_pass_when_hashes_match(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        section = _check_governance(repo)
        assert section.status == "pass"
        assert section.name == "governance"
        assert "matches" in section.summary

    def test_fail_when_constitution_modified(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        # Modify constitution after compiling lock
        (repo / ".exo" / "CONSTITUTION.md").write_text(
            "# Modified Constitution\n\n"
            + _policy_block(
                {
                    "id": "RULE-NEW-001",
                    "type": "filesystem_deny",
                    "patterns": ["*.secret"],
                    "actions": ["read"],
                    "message": "Modified rule",
                }
            ),
            encoding="utf-8",
        )
        section = _check_governance(repo)
        assert section.status == "fail"
        assert section.errors >= 1

    def test_skip_when_no_constitution(self, tmp_path: Path) -> None:
        repo = tmp_path
        (repo / ".exo").mkdir(parents=True, exist_ok=True)
        section = _check_governance(repo)
        assert section.status == "skip"

    def test_fail_when_no_lock(self, tmp_path: Path) -> None:
        repo = tmp_path
        exo_dir = repo / ".exo"
        exo_dir.mkdir(parents=True, exist_ok=True)
        (exo_dir / "CONSTITUTION.md").write_text("# Test\n", encoding="utf-8")
        section = _check_governance(repo)
        assert section.status == "fail"
        assert section.errors >= 1


# ── Adapter Freshness ────────────────────────────────────────────────


class TestCheckAdapters:
    def test_pass_when_hashes_match(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        lock = governance_mod.load_governance_lock(repo)
        source_hash = lock["source_hash"]
        # Write adapter with matching hash (first 16 chars)
        _write_adapter(repo, "CLAUDE.md", source_hash[:16])
        section = _check_adapters(repo)
        assert section.status == "pass"

    def test_fail_when_hash_mismatches(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_adapter(repo, "CLAUDE.md", "deadbeef12345678")
        section = _check_adapters(repo)
        assert section.status == "fail"
        assert section.errors >= 1
        assert "stale" in section.summary

    def test_skip_when_no_lock(self, tmp_path: Path) -> None:
        repo = tmp_path
        (repo / ".exo").mkdir(parents=True, exist_ok=True)
        section = _check_adapters(repo)
        assert section.status == "skip"

    def test_skip_when_no_adapters(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        section = _check_adapters(repo)
        assert section.status == "skip"
        assert "no adapter files" in section.summary

    def test_mixed_fresh_and_stale(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        lock = governance_mod.load_governance_lock(repo)
        source_hash = lock["source_hash"]
        _write_adapter(repo, "CLAUDE.md", source_hash[:16])
        _write_adapter(repo, ".cursorrules", "deadbeef12345678")
        section = _check_adapters(repo)
        assert section.status == "fail"
        assert "stale" in section.details
        assert "fresh" in section.details


# ── Feature Traceability ─────────────────────────────────────────────


class TestCheckFeatures:
    def test_skip_when_no_manifest(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        section = _check_features(repo)
        assert section.status == "skip"

    def test_pass_clean_trace(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "auth", "status": "active"}])
        _write_source_file(repo, "src/auth.py", "# @feature: auth\ndef login(): pass\n")
        section = _check_features(repo)
        assert section.status == "pass"
        assert section.errors == 0

    def test_fail_with_violations(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "auth", "status": "active"}])
        _write_source_file(repo, "src/bad.py", "# @feature: nonexistent\n")
        section = _check_features(repo)
        assert section.status == "fail"
        assert section.errors >= 1


# ── Requirement Traceability ─────────────────────────────────────────


class TestCheckRequirements:
    def test_skip_when_no_manifest(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        section = _check_requirements(repo)
        assert section.status == "skip"

    def test_pass_clean_trace(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(repo, [{"id": "REQ-001", "title": "Auth"}])
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\ndef login(): pass\n")
        section = _check_requirements(repo)
        assert section.status == "pass"
        assert section.errors == 0

    def test_fail_with_violations(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(repo, [{"id": "REQ-001", "title": "Auth"}])
        _write_source_file(repo, "src/bad.py", "# @req: REQ-999\n")
        section = _check_requirements(repo)
        assert section.status == "fail"
        assert section.errors >= 1


# ── Session Health ───────────────────────────────────────────────────


class TestCheckSessions:
    def test_pass_when_no_sessions(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        section = _check_sessions(repo)
        assert section.status == "pass"
        assert "no active" in section.summary


# ── Composite Drift ─────────────────────────────────────────────────


class TestDrift:
    def test_all_pass(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = drift(repo)
        assert report.passed
        assert report.overall == "pass"
        assert len(report.sections) == 6  # governance, adapters, features, requirements, coherence, sessions

    def test_fail_when_governance_drifted(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        # Modify constitution after compiling lock
        (repo / ".exo" / "CONSTITUTION.md").write_text(
            "# Modified\n\n"
            + _policy_block(
                {
                    "id": "RULE-NEW-001",
                    "type": "filesystem_deny",
                    "patterns": ["*.secret"],
                    "actions": ["read"],
                    "message": "New rule",
                }
            ),
            encoding="utf-8",
        )
        report = drift(repo)
        assert not report.passed
        gov_section = next(s for s in report.sections if s.name == "governance")
        assert gov_section.status == "fail"

    def test_fail_when_features_have_violations(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "auth", "status": "active"}])
        _write_source_file(repo, "src/bad.py", "# @feature: nonexistent\n")
        report = drift(repo)
        assert not report.passed
        feat_section = next(s for s in report.sections if s.name == "features")
        assert feat_section.status == "fail"

    def test_fail_when_requirements_have_violations(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(repo, [{"id": "REQ-001", "title": "Auth"}])
        _write_source_file(repo, "src/bad.py", "# @req: REQ-999\n")
        report = drift(repo)
        assert not report.passed
        req_section = next(s for s in report.sections if s.name == "requirements")
        assert req_section.status == "fail"

    def test_skip_flags(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = drift(
            repo,
            skip_adapters=True,
            skip_features=True,
            skip_requirements=True,
            skip_sessions=True,
        )
        assert len(report.sections) == 2  # governance + coherence (coherence skips without config)
        assert report.sections[0].name == "governance"

    def test_total_errors_and_warnings(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "auth", "status": "active"}])
        _write_source_file(repo, "src/bad.py", "# @feature: ghost\n")
        report = drift(repo)
        assert report.total_errors >= 1

    def test_mixed_pass_and_skip(self, tmp_path: Path) -> None:
        """Skipped sections don't cause overall failure."""
        repo = _bootstrap_repo(tmp_path)
        # No features.yaml, no requirements.yaml — those will skip
        report = drift(repo)
        assert report.passed
        statuses = {s.name: s.status for s in report.sections}
        assert statuses["governance"] == "pass"
        assert statuses["features"] == "skip"
        assert statuses["requirements"] == "skip"


# ── Report Output ────────────────────────────────────────────────────


class TestDriftReport:
    def test_to_dict_structure(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = drift(repo)
        d = drift_to_dict(report)
        assert d["passed"] is True
        assert d["overall"] == "pass"
        assert "total_errors" in d
        assert "total_warnings" in d
        assert "sections" in d
        assert d["section_count"] == 6
        assert "checked_at" in d

    def test_to_dict_with_failure(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        (repo / ".exo" / "CONSTITUTION.md").write_text("# Modified\n", encoding="utf-8")
        report = drift(repo)
        d = drift_to_dict(report)
        assert d["passed"] is False
        assert d["total_errors"] >= 1

    def test_human_format_pass(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = drift(repo)
        text = format_drift_human(report)
        assert "Governance Drift: PASS" in text
        assert "[OK]" in text

    def test_human_format_fail(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        (repo / ".exo" / "CONSTITUTION.md").write_text("# Modified\n", encoding="utf-8")
        report = drift(repo)
        text = format_drift_human(report)
        assert "Governance Drift: FAIL" in text
        assert "[FAIL]" in text

    def test_human_format_skip(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = drift(repo)
        text = format_drift_human(report)
        assert "[SKIP]" in text  # features and requirements should skip


# ── CLI Integration ──────────────────────────────────────────────────


class TestCLIDrift:
    def test_json_output(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), "drift"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"]
        assert data["data"]["passed"]
        assert data["data"]["section_count"] == 6

    def test_human_output(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "human", "--repo", str(repo), "drift"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "Governance Drift: PASS" in result.stdout

    def test_skip_flags(self, tmp_path: Path) -> None:
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
                "drift",
                "--skip-adapters",
                "--skip-features",
                "--skip-requirements",
                "--skip-sessions",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["section_count"] == 2

    def test_fail_exit_code_on_drift(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "auth", "status": "active"}])
        _write_source_file(repo, "src/bad.py", "# @feature: ghost\n")
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), "drift"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # exo drift returns ok=True but with passed=False (report is the data)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert not data["data"]["passed"]

    def test_stale_hours_flag(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), "drift", "--stale-hours", "24"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"]


# ── Fleet-Level Drift Aggregation ────────────────────────────────


def _write_index_entries(repo: Path, entries: list[dict[str, Any]]) -> None:
    index_path = repo / ".exo" / "memory" / "sessions" / "index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with open(index_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


class TestFleetDrift:
    def test_empty_repo(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        data = fleet_drift(repo)
        assert data["agent_count"] == 0
        assert data["avg_drift"] == 0.0

    def test_with_finished_sessions(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        entries = [
            {"session_id": "SES-A", "actor": "agent:a", "ticket_id": "T1", "drift_score": 0.2},
            {"session_id": "SES-B", "actor": "agent:b", "ticket_id": "T2", "drift_score": 0.4},
        ]
        _write_index_entries(repo, entries)
        data = fleet_drift(repo)
        assert data["finished_count"] == 2
        assert data["avg_drift"] == 0.3
        assert data["max_drift"] == 0.4

    def test_high_drift_flagged(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        entries = [
            {"session_id": "SES-A", "actor": "agent:a", "ticket_id": "T1", "drift_score": 0.85},
            {"session_id": "SES-B", "actor": "agent:b", "ticket_id": "T2", "drift_score": 0.3},
        ]
        _write_index_entries(repo, entries)
        data = fleet_drift(repo)
        assert data["high_drift_count"] == 1

    def test_include_finished_limits(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        entries = [
            {"session_id": f"SES-{i}", "actor": "agent:a", "ticket_id": "T1", "drift_score": 0.1} for i in range(20)
        ]
        _write_index_entries(repo, entries)
        data = fleet_drift(repo, include_finished=5)
        assert data["finished_count"] == 5

    def test_no_index_file(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        data = fleet_drift(repo)
        assert data["finished_count"] == 0

    def test_active_session_has_no_drift_score(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        from exo.kernel import tickets as tkt

        tkt.save_ticket(
            repo,
            {
                "id": "TKT-1",
                "title": "T",
                "intent": "T",
                "priority": 2,
                "labels": [],
                "type": "feature",
                "status": "todo",
            },
        )
        tkt.acquire_lock(repo, "TKT-1", owner="agent:test", role="developer")
        from exo.orchestrator.session import AgentSessionManager

        mgr = AgentSessionManager(repo, actor="agent:test")
        mgr.start(ticket_id="TKT-1")

        data = fleet_drift(repo)
        assert data["active_count"] == 1
        active_agents = [a for a in data["agents"] if a["state"] == "active"]
        assert len(active_agents) == 1
        assert active_agents[0]["drift_score"] is None

    def test_checked_at_present(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        data = fleet_drift(repo)
        assert "checked_at" in data


class TestFormatFleetDriftHuman:
    def test_shows_session_count(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        entries = [
            {"session_id": "SES-A", "actor": "agent:a", "ticket_id": "T1", "drift_score": 0.2},
        ]
        _write_index_entries(repo, entries)
        data = fleet_drift(repo)
        human = format_fleet_drift_human(data)
        assert "1 session(s)" in human

    def test_shows_drift_scores(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        entries = [
            {"session_id": "SES-A", "actor": "agent:a", "ticket_id": "T1", "drift_score": 0.35},
        ]
        _write_index_entries(repo, entries)
        data = fleet_drift(repo)
        human = format_fleet_drift_human(data)
        assert "avg drift:" in human
        assert "0.35" in human

    def test_shows_high_drift_alert(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        entries = [
            {"session_id": "SES-A", "actor": "agent:a", "ticket_id": "T1", "drift_score": 0.9},
        ]
        _write_index_entries(repo, entries)
        data = fleet_drift(repo)
        human = format_fleet_drift_human(data)
        assert "HIGH DRIFT" in human
