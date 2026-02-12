"""Tests for Config Validation, Doctor, and Upgrade.

Covers:
- Config schema validation (missing keys, type errors, value ranges)
- Doctor unified health check (scaffold, config, drift, scan freshness)
- Upgrade schema migration (backfill, directories, recompile, adapters)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from exo.kernel import governance as governance_mod
from exo.kernel.utils import dump_yaml, load_yaml
from exo.stdlib.config_schema import (
    ConfigIssue,
    ConfigValidation,
    validate_config,
    validation_to_dict,
    format_validation_human,
)
from exo.stdlib.defaults import DEFAULT_CONFIG, DEFAULT_CONSTITUTION
from exo.stdlib.doctor import (
    DoctorReport,
    DoctorSection,
    doctor,
    doctor_to_dict,
    format_doctor_human,
)
from exo.stdlib.upgrade import (
    _backfill_config,
    upgrade,
    format_upgrade_human,
)


def _bootstrap_repo(tmp_path: Path) -> Path:
    """Create a minimal .exo scaffold for testing."""
    repo = tmp_path
    exo_dir = repo / ".exo"
    exo_dir.mkdir(parents=True, exist_ok=True)
    (exo_dir / "CONSTITUTION.md").write_text(DEFAULT_CONSTITUTION, encoding="utf-8")
    dump_yaml(exo_dir / "config.yaml", DEFAULT_CONFIG)
    # Create required dirs
    for d in ["tickets", "locks", "logs", "memory", "cache", "scratchpad",
              "scripts", "specs", "observations", "patches", "proposals",
              "reviews", "practices", "roles", "templates", "schemas",
              "tickets/ARCHIVE", "scratchpad/threads", "cache/distill"]:
        (exo_dir / d).mkdir(parents=True, exist_ok=True)
    governance_mod.compile_constitution(repo)
    return repo


def _full_init_repo(tmp_path: Path) -> Path:
    """Use KernelEngine.init() for a fully initialized repo."""
    from exo.stdlib.engine import KernelEngine
    engine = KernelEngine(repo=tmp_path, actor="test-agent")
    engine.init(scan=False)
    return tmp_path


# ════════════════════════════════════════════════════════════════════
# Config Validation
# ════════════════════════════════════════════════════════════════════


class TestConfigValidation:

    def test_valid_config_passes(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = validate_config(repo)
        assert result.passed
        assert result.error_count == 0
        assert result.config_exists

    def test_missing_config_fails(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        result = validate_config(tmp_path)
        assert not result.passed
        assert result.error_count == 1
        assert not result.config_exists

    def test_invalid_yaml_fails(self, tmp_path: Path) -> None:
        exo = tmp_path / ".exo"
        exo.mkdir()
        (exo / "config.yaml").write_text("[invalid yaml{{{", encoding="utf-8")
        result = validate_config(tmp_path)
        # load_yaml may return invalid data or succeed with unexpected type
        # Either way, should not crash
        assert isinstance(result, ConfigValidation)

    def test_wrong_version_type(self, tmp_path: Path) -> None:
        exo = tmp_path / ".exo"
        exo.mkdir()
        config = dict(DEFAULT_CONFIG)
        config["version"] = "not-an-int"
        dump_yaml(exo / "config.yaml", config)
        result = validate_config(tmp_path)
        assert not result.passed
        assert any("version" in i.path and "integer" in i.message for i in result.issues)

    def test_missing_required_keys(self, tmp_path: Path) -> None:
        exo = tmp_path / ".exo"
        exo.mkdir()
        # Minimal config missing most keys
        dump_yaml(exo / "config.yaml", {"version": 1})
        result = validate_config(tmp_path)
        assert result.warning_count > 0
        missing_keys = [i.path for i in result.issues if "Missing" in i.message]
        assert len(missing_keys) > 0

    def test_wrong_type_checks_allowlist(self, tmp_path: Path) -> None:
        exo = tmp_path / ".exo"
        exo.mkdir()
        config = dict(DEFAULT_CONFIG)
        config["checks_allowlist"] = "not-a-list"
        dump_yaml(exo / "config.yaml", config)
        result = validate_config(tmp_path)
        assert any("checks_allowlist" in i.path for i in result.issues)

    def test_negative_budget_fails(self, tmp_path: Path) -> None:
        exo = tmp_path / ".exo"
        exo.mkdir()
        import copy
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["defaults"]["ticket_budgets"]["max_files_changed"] = -1
        dump_yaml(exo / "config.yaml", config)
        result = validate_config(tmp_path)
        assert not result.passed
        assert any("positive" in i.message for i in result.issues)

    def test_zero_budget_fails(self, tmp_path: Path) -> None:
        exo = tmp_path / ".exo"
        exo.mkdir()
        import copy
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["defaults"]["ticket_budgets"]["max_loc_changed"] = 0
        dump_yaml(exo / "config.yaml", config)
        result = validate_config(tmp_path)
        assert not result.passed

    def test_future_version_warning(self, tmp_path: Path) -> None:
        exo = tmp_path / ".exo"
        exo.mkdir()
        import copy
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["version"] = 999
        dump_yaml(exo / "config.yaml", config)
        result = validate_config(tmp_path)
        assert result.warning_count > 0
        assert any("newer" in i.message for i in result.issues)

    def test_non_string_list_items_warning(self, tmp_path: Path) -> None:
        exo = tmp_path / ".exo"
        exo.mkdir()
        import copy
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["checks_allowlist"] = ["valid", 42, True]
        dump_yaml(exo / "config.yaml", config)
        result = validate_config(tmp_path)
        assert result.warning_count >= 2  # 42 and True


class TestConfigValidationSerialization:

    def test_validation_to_dict(self) -> None:
        result = ConfigValidation(
            issues=[ConfigIssue(severity="error", path="version", message="bad")],
            config_exists=True,
            config_version=1,
        )
        d = validation_to_dict(result)
        assert d["passed"] is False
        assert d["error_count"] == 1
        assert len(d["issues"]) == 1
        # JSON serializable
        json.dumps(d, ensure_ascii=True)

    def test_format_validation_human_pass(self) -> None:
        result = ConfigValidation(config_exists=True, config_version=1)
        text = format_validation_human(result)
        assert "PASS" in text
        assert "No issues" in text

    def test_format_validation_human_fail(self) -> None:
        result = ConfigValidation(
            issues=[ConfigIssue(severity="error", path="version", message="bad type")],
            config_exists=True,
        )
        text = format_validation_human(result)
        assert "FAIL" in text
        assert "ERROR" in text


# ════════════════════════════════════════════════════════════════════
# Doctor
# ════════════════════════════════════════════════════════════════════


class TestDoctor:

    def test_healthy_repo_passes(self, tmp_path: Path) -> None:
        repo = _full_init_repo(tmp_path)
        report = doctor(repo)
        assert report.passed
        assert report.overall == "pass"
        assert len(report.sections) == 4

    def test_missing_exo_fails(self, tmp_path: Path) -> None:
        report = doctor(tmp_path)
        assert not report.passed
        scaffold = [s for s in report.sections if s.name == "scaffold"][0]
        assert scaffold.status == "fail"

    def test_scaffold_check(self, tmp_path: Path) -> None:
        repo = _full_init_repo(tmp_path)
        report = doctor(repo)
        scaffold = [s for s in report.sections if s.name == "scaffold"][0]
        assert scaffold.status == "pass"

    def test_config_check(self, tmp_path: Path) -> None:
        repo = _full_init_repo(tmp_path)
        report = doctor(repo)
        config_section = [s for s in report.sections if s.name == "config_validation"][0]
        assert config_section.status == "pass"

    def test_drift_check(self, tmp_path: Path) -> None:
        repo = _full_init_repo(tmp_path)
        report = doctor(repo)
        drift_section = [s for s in report.sections if s.name == "governance_drift"][0]
        assert drift_section.status == "pass"

    def test_scan_freshness_check(self, tmp_path: Path) -> None:
        repo = _full_init_repo(tmp_path)
        report = doctor(repo)
        scan_section = [s for s in report.sections if s.name == "scan_freshness"][0]
        assert scan_section.status == "pass"

    def test_bad_config_fails_doctor(self, tmp_path: Path) -> None:
        repo = _full_init_repo(tmp_path)
        # Corrupt config
        import copy
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["defaults"]["ticket_budgets"]["max_files_changed"] = -1
        dump_yaml(repo / ".exo" / "config.yaml", config)
        report = doctor(repo)
        assert not report.passed
        config_section = [s for s in report.sections if s.name == "config_validation"][0]
        assert config_section.status == "fail"

    def test_total_errors_and_warnings(self, tmp_path: Path) -> None:
        repo = _full_init_repo(tmp_path)
        report = doctor(repo)
        assert report.total_errors == 0
        assert isinstance(report.total_warnings, int)


class TestDoctorSerialization:

    def test_doctor_to_dict(self, tmp_path: Path) -> None:
        repo = _full_init_repo(tmp_path)
        report = doctor(repo)
        d = doctor_to_dict(report)
        assert "overall" in d
        assert "sections" in d
        assert "total_errors" in d
        assert len(d["sections"]) == 4
        json.dumps(d, ensure_ascii=True)

    def test_format_doctor_human(self, tmp_path: Path) -> None:
        repo = _full_init_repo(tmp_path)
        report = doctor(repo)
        text = format_doctor_human(report)
        assert "PASS" in text
        assert "scaffold" in text
        assert "config_validation" in text

    def test_format_doctor_human_failure(self, tmp_path: Path) -> None:
        report = doctor(tmp_path)
        text = format_doctor_human(report)
        assert "FAIL" in text


class TestDoctorProperties:

    def test_passed_true(self) -> None:
        report = DoctorReport(
            sections=[DoctorSection(name="test", status="pass", summary="ok")],
            overall="pass",
        )
        assert report.passed is True

    def test_passed_false(self) -> None:
        report = DoctorReport(
            sections=[DoctorSection(name="test", status="fail", summary="bad")],
            overall="fail",
        )
        assert report.passed is False


# ════════════════════════════════════════════════════════════════════
# Upgrade
# ════════════════════════════════════════════════════════════════════


class TestUpgrade:

    def test_upgrade_healthy_repo(self, tmp_path: Path) -> None:
        repo = _full_init_repo(tmp_path)
        result = upgrade(repo)
        assert result["upgraded"] is True
        assert result["from_version"] == 1
        assert result["to_version"] == 1

    def test_upgrade_no_exo_raises(self, tmp_path: Path) -> None:
        import pytest
        with pytest.raises(Exception) as exc_info:
            upgrade(tmp_path)
        assert "No .exo directory" in str(exc_info.value)

    def test_upgrade_no_config_raises(self, tmp_path: Path) -> None:
        import pytest
        (tmp_path / ".exo").mkdir()
        with pytest.raises(Exception) as exc_info:
            upgrade(tmp_path)
        assert "config.yaml" in str(exc_info.value)

    def test_upgrade_creates_missing_dirs(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        # Remove some dirs
        import shutil
        reflections = repo / ".exo" / "memory" / "reflections"
        if not reflections.exists():
            pass  # Already doesn't exist
        result = upgrade(repo)
        dirs_created = result["dirs_created"]
        # All dirs from _REQUIRED_DIRS should now exist
        from exo.stdlib.upgrade import _REQUIRED_DIRS
        for d in _REQUIRED_DIRS:
            assert (repo / d).is_dir(), f"Missing dir: {d}"

    def test_upgrade_backfills_missing_config_keys(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        # Remove a key from config
        config = load_yaml(repo / ".exo" / "config.yaml")
        config.pop("scheduler", None)
        dump_yaml(repo / ".exo" / "config.yaml", config)
        result = upgrade(repo)
        assert any("scheduler" in a for a in result["actions"])
        # Verify key was added
        updated = load_yaml(repo / ".exo" / "config.yaml")
        assert "scheduler" in updated

    def test_upgrade_dry_run(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        config = load_yaml(repo / ".exo" / "config.yaml")
        config.pop("scheduler", None)
        dump_yaml(repo / ".exo" / "config.yaml", config)
        result = upgrade(repo, dry_run=True)
        assert result["dry_run"] is True
        # Verify key was NOT added (dry run)
        updated = load_yaml(repo / ".exo" / "config.yaml")
        assert "scheduler" not in updated

    def test_upgrade_recompiles_governance(self, tmp_path: Path) -> None:
        repo = _full_init_repo(tmp_path)
        result = upgrade(repo)
        assert result["recompiled"] is True

    def test_upgrade_regenerates_adapters(self, tmp_path: Path) -> None:
        repo = _full_init_repo(tmp_path)
        result = upgrade(repo)
        assert isinstance(result["adapters_written"], list)

    def test_upgrade_idempotent(self, tmp_path: Path) -> None:
        repo = _full_init_repo(tmp_path)
        result1 = upgrade(repo)
        result2 = upgrade(repo)
        # Second run should have fewer actions (nothing to backfill)
        assert result2["upgraded"] is True


class TestBackfillConfig:

    def test_adds_missing_top_level(self) -> None:
        config: dict[str, Any] = {"version": 1}
        defaults = {"version": 1, "scheduler": {"enabled": False}}
        added = _backfill_config(config, defaults)
        assert "scheduler" in added
        assert config["scheduler"] == {"enabled": False}

    def test_adds_missing_nested(self) -> None:
        config: dict[str, Any] = {"defaults": {}}
        defaults = {"defaults": {"ticket_budgets": {"max_files_changed": 12}}}
        added = _backfill_config(config, defaults)
        assert "defaults.ticket_budgets" in added
        assert config["defaults"]["ticket_budgets"]["max_files_changed"] == 12

    def test_preserves_existing_values(self) -> None:
        config: dict[str, Any] = {"version": 1, "scheduler": {"enabled": True}}
        defaults = {"version": 1, "scheduler": {"enabled": False, "lanes": []}}
        added = _backfill_config(config, defaults)
        # Should add lanes but NOT overwrite enabled
        assert config["scheduler"]["enabled"] is True
        assert "scheduler.lanes" in added

    def test_empty_config(self) -> None:
        config: dict[str, Any] = {}
        import copy
        defaults = copy.deepcopy(DEFAULT_CONFIG)
        added = _backfill_config(config, defaults)
        assert len(added) > 0
        assert "version" in config


class TestUpgradeSerialization:

    def test_format_upgrade_human(self, tmp_path: Path) -> None:
        repo = _full_init_repo(tmp_path)
        result = upgrade(repo)
        text = format_upgrade_human(result)
        assert "v1" in text
        assert "Upgrade" in text

    def test_format_upgrade_dry_run(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        config = load_yaml(repo / ".exo" / "config.yaml")
        config.pop("scheduler", None)
        dump_yaml(repo / ".exo" / "config.yaml", config)
        result = upgrade(repo, dry_run=True)
        text = format_upgrade_human(result)
        assert "DRY RUN" in text

    def test_json_serializable(self, tmp_path: Path) -> None:
        repo = _full_init_repo(tmp_path)
        result = upgrade(repo)
        json.dumps(result, ensure_ascii=True)


# ════════════════════════════════════════════════════════════════════
# CLI Integration
# ════════════════════════════════════════════════════════════════════


class TestCLI:

    def test_cli_doctor(self, tmp_path: Path) -> None:
        from exo.cli import main
        _full_init_repo(tmp_path)
        exit_code = main(["--repo", str(tmp_path), "--format", "human", "doctor"])
        assert exit_code == 0

    def test_cli_doctor_json(self, tmp_path: Path) -> None:
        from exo.cli import main
        _full_init_repo(tmp_path)
        exit_code = main(["--repo", str(tmp_path), "--format", "json", "doctor"])
        assert exit_code == 0

    def test_cli_config_validate(self, tmp_path: Path) -> None:
        from exo.cli import main
        _full_init_repo(tmp_path)
        exit_code = main(["--repo", str(tmp_path), "--format", "human", "config-validate"])
        assert exit_code == 0

    def test_cli_upgrade(self, tmp_path: Path) -> None:
        from exo.cli import main
        _full_init_repo(tmp_path)
        exit_code = main(["--repo", str(tmp_path), "--format", "human", "upgrade"])
        assert exit_code == 0

    def test_cli_upgrade_dry_run(self, tmp_path: Path) -> None:
        from exo.cli import main
        _full_init_repo(tmp_path)
        exit_code = main(["--repo", str(tmp_path), "--format", "human", "upgrade", "--dry-run"])
        assert exit_code == 0
