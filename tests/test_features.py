"""Tests for the Feature Manifest and Traceability Linter.

Covers:
- Feature manifest loading and validation (.exo/features.yaml)
- Code tag scanning (@feature: / @endfeature)
- Traceability linting (cross-reference tags vs manifest)
- Violation detection (invalid_tag, deprecated_usage, deleted_usage, locked_edit, unbound_feature)
- Report formatting and serialization
- CLI integration (exo features, exo trace)
- Scope deny generation from locked features
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from exo.kernel import governance as governance_mod
from exo.kernel import tickets as tickets_mod
from exo.orchestrator import AgentSessionManager
from exo.orchestrator.session import _exo_banner
from exo.stdlib.features import (
    END_TAG_PATTERN,
    TAG_PATTERN,
    VALID_STATUSES,
    FeatureDef,
    features_to_list,
    format_prune_human,
    format_trace_human,
    generate_scope_deny,
    load_features,
    prune,
    prune_to_dict,
    scan_tags,
    trace,
    trace_to_dict,
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


def _write_features_yaml(repo: Path, features: list[dict[str, Any]]) -> Path:
    """Write features.yaml with the given feature list."""
    import yaml

    features_path = repo / ".exo" / "features.yaml"
    features_path.write_text(
        yaml.dump({"features": features}, default_flow_style=False),
        encoding="utf-8",
    )
    return features_path


def _write_source_file(repo: Path, rel_path: str, content: str) -> Path:
    """Write a source file at the given relative path."""
    filepath = repo / rel_path
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(content, encoding="utf-8")
    return filepath


# ──────────────────────────────────────────────────────────────
# Manifest loading and validation
# ──────────────────────────────────────────────────────────────


class TestLoadFeatures:
    def test_load_basic_manifest(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(
            repo,
            [
                {"id": "auth", "status": "active", "description": "Authentication system", "owner": "team-a"},
                {"id": "billing", "status": "deprecated"},
                {"id": "legacy-api", "status": "deleted"},
            ],
        )
        features = load_features(repo)
        assert len(features) == 3
        assert features[0].id == "auth"
        assert features[0].status == "active"
        assert features[0].description == "Authentication system"
        assert features[0].owner == "team-a"
        assert features[1].status == "deprecated"
        assert features[2].status == "deleted"

    def test_load_with_files_and_lock(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(
            repo,
            [
                {
                    "id": "core-engine",
                    "status": "active",
                    "files": ["exo/kernel/*.py", "exo/kernel/**/*.py"],
                    "allow_agent_edit": False,
                },
            ],
        )
        features = load_features(repo)
        assert len(features) == 1
        f = features[0]
        assert f.id == "core-engine"
        assert f.files == ("exo/kernel/*.py", "exo/kernel/**/*.py")
        assert f.allow_agent_edit is False

    def test_defaults(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "minimal"}])
        features = load_features(repo)
        f = features[0]
        assert f.status == "active"
        assert f.description == ""
        assert f.owner == ""
        assert f.files == ()
        assert f.allow_agent_edit is True

    def test_experimental_status(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "beta-feature", "status": "experimental"}])
        features = load_features(repo)
        assert features[0].status == "experimental"

    def test_missing_manifest_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        import pytest

        with pytest.raises(Exception) as exc_info:
            load_features(repo)
        assert "FEATURES_MANIFEST_MISSING" in str(exc_info.value)

    def test_missing_id_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"status": "active"}])
        import pytest

        with pytest.raises(Exception) as exc_info:
            load_features(repo)
        assert "FEATURES_ENTRY_MISSING_ID" in str(exc_info.value)

    def test_duplicate_id_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(
            repo,
            [
                {"id": "auth", "status": "active"},
                {"id": "auth", "status": "deprecated"},
            ],
        )
        import pytest

        with pytest.raises(Exception) as exc_info:
            load_features(repo)
        assert "FEATURES_DUPLICATE_ID" in str(exc_info.value)

    def test_invalid_status_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "bad", "status": "invalid_status"}])
        import pytest

        with pytest.raises(Exception) as exc_info:
            load_features(repo)
        assert "FEATURES_INVALID_STATUS" in str(exc_info.value)

    def test_non_list_features_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        import yaml

        (repo / ".exo" / "features.yaml").write_text(
            yaml.dump({"features": "not_a_list"}, default_flow_style=False),
            encoding="utf-8",
        )
        import pytest

        with pytest.raises(Exception) as exc_info:
            load_features(repo)
        assert "FEATURES_MANIFEST_INVALID" in str(exc_info.value)

    def test_non_dict_entry_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        import yaml

        (repo / ".exo" / "features.yaml").write_text(
            yaml.dump({"features": ["not_a_dict"]}, default_flow_style=False),
            encoding="utf-8",
        )
        import pytest

        with pytest.raises(Exception) as exc_info:
            load_features(repo)
        assert "FEATURES_ENTRY_INVALID" in str(exc_info.value)


# ──────────────────────────────────────────────────────────────
# Tag scanning
# ──────────────────────────────────────────────────────────────


class TestScanTags:
    def test_scan_python_tags(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "auth"}])
        _write_source_file(repo, "src/auth.py", ("# @feature: auth\ndef login():\n    pass\n# @endfeature\n"))
        tags = scan_tags(repo)
        assert len(tags) == 1
        assert tags[0].feature_id == "auth"
        assert tags[0].file == "src/auth.py"
        assert tags[0].line == 1
        assert tags[0].end_line == 4

    def test_scan_js_tags(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_source_file(repo, "src/billing.js", ("// @feature: billing\nfunction charge() {}\n// @endfeature\n"))
        tags = scan_tags(repo)
        assert len(tags) == 1
        assert tags[0].feature_id == "billing"
        assert tags[0].end_line == 3

    def test_scan_single_point_binding(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_source_file(repo, "src/utils.py", ("# @feature: utils\ndef helper():\n    pass\n"))
        tags = scan_tags(repo)
        assert len(tags) == 1
        assert tags[0].end_line is None

    def test_scan_multiple_features_same_file(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_source_file(
            repo,
            "src/app.py",
            (
                "# @feature: auth\n"
                "def login(): pass\n"
                "# @endfeature\n"
                "\n"
                "# @feature: billing\n"
                "def charge(): pass\n"
                "# @endfeature\n"
            ),
        )
        tags = scan_tags(repo)
        assert len(tags) == 2
        ids = {t.feature_id for t in tags}
        assert ids == {"auth", "billing"}

    def test_scan_case_insensitive(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_source_file(repo, "src/test.py", ("# @Feature: CamelCase\ndef foo(): pass\n# @EndFeature\n"))
        tags = scan_tags(repo)
        assert len(tags) == 1
        assert tags[0].feature_id == "CamelCase"

    def test_scan_skips_excluded_dirs(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_source_file(repo, "node_modules/dep.js", "// @feature: dep\n")
        _write_source_file(repo, "__pycache__/cached.py", "# @feature: cached\n")
        _write_source_file(repo, ".git/hooks/pre-commit.py", "# @feature: hook\n")
        tags = scan_tags(repo)
        assert len(tags) == 0

    def test_scan_multiple_files(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_source_file(repo, "src/a.py", "# @feature: alpha\n")
        _write_source_file(repo, "src/b.py", "# @feature: beta\n")
        _write_source_file(repo, "lib/c.ts", "// @feature: gamma\n")
        tags = scan_tags(repo)
        assert len(tags) == 3

    def test_scan_custom_globs(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_source_file(repo, "src/a.py", "# @feature: alpha\n")
        _write_source_file(repo, "src/b.txt", "# @feature: beta\n")
        # Only scan .txt files
        tags = scan_tags(repo, globs=["**/*.txt"])
        assert len(tags) == 1
        assert tags[0].feature_id == "beta"


# ──────────────────────────────────────────────────────────────
# Traceability linting
# ──────────────────────────────────────────────────────────────


class TestTrace:
    def test_clean_trace_passes(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(
            repo,
            [
                {"id": "auth", "status": "active"},
                {"id": "billing", "status": "active"},
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @feature: auth\ndef login(): pass\n# @endfeature\n")
        _write_source_file(repo, "src/billing.py", "# @feature: billing\ndef charge(): pass\n")
        report = trace(repo)
        assert report.passed is True
        assert report.features_total == 2
        assert report.features_active == 2
        assert report.tags_total == 2
        assert set(report.bound_features) == {"auth", "billing"}
        assert report.unbound_features == []

    def test_invalid_tag_is_error(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "auth"}])
        _write_source_file(repo, "src/orphan.py", "# @feature: nonexistent\ndef orphan(): pass\n")
        report = trace(repo)
        assert report.passed is False
        errors = [v for v in report.violations if v.kind == "invalid_tag"]
        assert len(errors) == 1
        assert errors[0].feature_id == "nonexistent"
        assert errors[0].severity == "error"

    def test_deprecated_usage_is_warning(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "old-api", "status": "deprecated"}])
        _write_source_file(repo, "src/old.py", "# @feature: old-api\ndef legacy(): pass\n")
        report = trace(repo)
        assert report.passed is True  # warnings don't fail
        warnings = [v for v in report.violations if v.kind == "deprecated_usage"]
        assert len(warnings) == 1
        assert warnings[0].severity == "warning"
        assert "old-api" in report.deprecated_with_code

    def test_deleted_usage_is_error(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "dead-feature", "status": "deleted"}])
        _write_source_file(repo, "src/dead.py", "# @feature: dead-feature\ndef zombie(): pass\n")
        report = trace(repo)
        assert report.passed is False
        errors = [v for v in report.violations if v.kind == "deleted_usage"]
        assert len(errors) == 1
        assert errors[0].feature_id == "dead-feature"

    def test_locked_edit_warning(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(
            repo,
            [
                {"id": "core", "status": "active", "allow_agent_edit": False},
            ],
        )
        _write_source_file(repo, "src/core.py", "# @feature: core\ndef kernel(): pass\n")
        report = trace(repo)
        assert report.passed is True
        locked = [v for v in report.violations if v.kind == "locked_edit"]
        assert len(locked) == 1
        assert locked[0].severity == "warning"

    def test_unbound_active_feature_warning(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(
            repo,
            [
                {"id": "auth", "status": "active"},
                {"id": "billing", "status": "active"},
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @feature: auth\ndef login(): pass\n")
        # billing has no code tags
        report = trace(repo)
        assert report.passed is True  # unbound is warning
        unbound = [v for v in report.violations if v.kind == "unbound_feature"]
        assert len(unbound) == 1
        assert unbound[0].feature_id == "billing"
        assert "billing" in report.unbound_features

    def test_unbound_check_skippable(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "new-thing", "status": "active"}])
        report = trace(repo, check_unbound=False)
        assert report.unbound_features == []
        unbound = [v for v in report.violations if v.kind == "unbound_feature"]
        assert len(unbound) == 0

    def test_deleted_feature_not_flagged_as_unbound(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "removed", "status": "deleted"}])
        report = trace(repo)
        assert "removed" not in report.unbound_features

    def test_feature_counts(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(
            repo,
            [
                {"id": "a", "status": "active"},
                {"id": "b", "status": "active"},
                {"id": "c", "status": "deprecated"},
                {"id": "d", "status": "deleted"},
                {"id": "e", "status": "experimental"},
            ],
        )
        report = trace(repo, check_unbound=False)
        assert report.features_total == 5
        assert report.features_active == 2
        assert report.features_deprecated == 1
        assert report.features_deleted == 1

    def test_multiple_violations_same_file(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(
            repo,
            [
                {"id": "deleted-a", "status": "deleted"},
                {"id": "deleted-b", "status": "deleted"},
            ],
        )
        _write_source_file(
            repo, "src/mess.py", ("# @feature: deleted-a\ndef a(): pass\n# @feature: deleted-b\ndef b(): pass\n")
        )
        report = trace(repo)
        assert report.passed is False
        errors = [v for v in report.violations if v.kind == "deleted_usage"]
        assert len(errors) == 2


# ──────────────────────────────────────────────────────────────
# Report serialization and formatting
# ──────────────────────────────────────────────────────────────


class TestReportOutput:
    def test_trace_to_dict_structure(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "auth", "status": "active"}])
        _write_source_file(repo, "src/auth.py", "# @feature: auth\n")
        report = trace(repo)
        d = trace_to_dict(report)
        assert d["features_total"] == 1
        assert d["tags_total"] == 1
        assert d["passed"] is True
        assert isinstance(d["violations"], list)
        assert isinstance(d["bound_features"], list)
        assert isinstance(d["unbound_features"], list)
        assert "checked_at" in d
        assert "error_count" in d
        assert "warning_count" in d

    def test_trace_to_dict_with_violations(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "auth"}])
        _write_source_file(repo, "src/orphan.py", "# @feature: ghost\n")
        report = trace(repo)
        d = trace_to_dict(report)
        assert d["passed"] is False
        assert d["error_count"] == 1
        assert d["violations"][0]["kind"] == "invalid_tag"

    def test_format_trace_human_pass(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "auth"}])
        _write_source_file(repo, "src/auth.py", "# @feature: auth\n")
        report = trace(repo)
        text = format_trace_human(report)
        assert "PASS" in text
        assert "features: 1" in text
        assert "code tags: 1" in text

    def test_format_trace_human_fail(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "auth"}])
        _write_source_file(repo, "src/bad.py", "# @feature: nonexistent\n")
        report = trace(repo)
        text = format_trace_human(report)
        assert "FAIL" in text
        assert "errors" in text
        assert "invalid_tag" in text

    def test_format_trace_human_deprecated_with_code(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "old", "status": "deprecated"}])
        _write_source_file(repo, "src/old.py", "# @feature: old\n")
        report = trace(repo)
        text = format_trace_human(report)
        assert "deprecated with code" in text


# ──────────────────────────────────────────────────────────────
# Scope deny generation
# ──────────────────────────────────────────────────────────────


class TestScopeDeny:
    def test_generate_scope_deny_from_locked_features(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(
            repo,
            [
                {"id": "core", "status": "active", "allow_agent_edit": False, "files": ["exo/kernel/*.py"]},
                {"id": "auth", "status": "active", "allow_agent_edit": True, "files": ["src/auth.py"]},
                {
                    "id": "config",
                    "status": "active",
                    "allow_agent_edit": False,
                    "files": ["config/*.yaml", "config/*.json"],
                },
            ],
        )
        features = load_features(repo)
        deny = generate_scope_deny(features)
        assert "exo/kernel/*.py" in deny
        assert "config/*.yaml" in deny
        assert "config/*.json" in deny
        assert "src/auth.py" not in deny

    def test_generate_scope_deny_empty_when_all_editable(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(
            repo,
            [
                {"id": "a", "status": "active", "files": ["src/*.py"]},
            ],
        )
        features = load_features(repo)
        deny = generate_scope_deny(features)
        assert deny == []

    def test_generate_scope_deny_no_files_on_locked(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(
            repo,
            [
                {"id": "a", "status": "active", "allow_agent_edit": False},
            ],
        )
        features = load_features(repo)
        deny = generate_scope_deny(features)
        assert deny == []


# ──────────────────────────────────────────────────────────────
# Features to list serialization
# ──────────────────────────────────────────────────────────────


class TestFeaturesToList:
    def test_round_trip(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(
            repo,
            [
                {
                    "id": "auth",
                    "status": "active",
                    "description": "Auth system",
                    "owner": "team-a",
                    "files": ["src/auth.py"],
                    "allow_agent_edit": False,
                },
            ],
        )
        features = load_features(repo)
        result = features_to_list(features)
        assert len(result) == 1
        d = result[0]
        assert d["id"] == "auth"
        assert d["status"] == "active"
        assert d["description"] == "Auth system"
        assert d["owner"] == "team-a"
        assert d["files"] == ("src/auth.py",)
        assert d["allow_agent_edit"] is False


# ──────────────────────────────────────────────────────────────
# Regex pattern tests
# ──────────────────────────────────────────────────────────────


class TestTagPatterns:
    def test_python_comment_tag(self) -> None:
        m = TAG_PATTERN.search("# @feature: auth")
        assert m is not None
        assert m.group(1) == "auth"

    def test_js_comment_tag(self) -> None:
        m = TAG_PATTERN.search("// @feature: billing")
        assert m is not None
        assert m.group(1) == "billing"

    def test_tag_with_extra_spaces(self) -> None:
        m = TAG_PATTERN.search("#   @feature:   my-feature")
        assert m is not None
        assert m.group(1) == "my-feature"

    def test_endfeature_python(self) -> None:
        assert END_TAG_PATTERN.search("# @endfeature") is not None

    def test_endfeature_js(self) -> None:
        assert END_TAG_PATTERN.search("// @endfeature") is not None

    def test_no_match_plain_text(self) -> None:
        assert TAG_PATTERN.search("feature: auth") is None

    def test_no_match_mid_word(self) -> None:
        assert TAG_PATTERN.search("x@feature: auth") is None


# ──────────────────────────────────────────────────────────────
# Valid statuses constant
# ──────────────────────────────────────────────────────────────


class TestValidStatuses:
    def test_valid_statuses_set(self) -> None:
        assert {"active", "experimental", "deprecated", "deleted"} == VALID_STATUSES


# ──────────────────────────────────────────────────────────────
# CLI integration
# ──────────────────────────────────────────────────────────────


class TestCLIFeatures:
    def test_features_json(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(
            repo,
            [
                {"id": "auth", "status": "active", "description": "Auth"},
                {"id": "billing", "status": "deprecated"},
            ],
        )
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--repo", str(repo), "--format", "json", "features"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert data["data"]["count"] == 2
        assert len(data["data"]["features"]) == 2

    def test_features_status_filter(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(
            repo,
            [
                {"id": "auth", "status": "active"},
                {"id": "old", "status": "deprecated"},
                {"id": "gone", "status": "deleted"},
            ],
        )
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--repo", str(repo), "--format", "json", "features", "--status", "active"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["count"] == 1
        assert data["data"]["features"][0]["id"] == "auth"

    def test_features_missing_manifest(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--repo", str(repo), "--format", "json", "features"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 1
        data = json.loads(result.stdout)
        assert data["ok"] is False
        assert "FEATURES_MANIFEST_MISSING" in json.dumps(data)

    def test_features_scope_deny(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(
            repo,
            [
                {"id": "core", "allow_agent_edit": False, "files": ["exo/kernel/*.py"]},
            ],
        )
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--repo", str(repo), "--format", "json", "features"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "exo/kernel/*.py" in data["data"]["scope_deny"]


class TestCLITrace:
    def test_trace_pass(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "auth", "status": "active"}])
        _write_source_file(repo, "src/auth.py", "# @feature: auth\ndef login(): pass\n")
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--repo", str(repo), "--format", "json", "trace"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert data["data"]["passed"] is True

    def test_trace_fail_invalid_tag(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "auth"}])
        _write_source_file(repo, "src/bad.py", "# @feature: nonexistent\n")
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--repo", str(repo), "--format", "json", "trace"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0  # CLI returns 0 even with violations (report is data)
        data = json.loads(result.stdout)
        assert data["data"]["passed"] is False
        assert data["data"]["error_count"] == 1

    def test_trace_human_output(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "auth"}])
        _write_source_file(repo, "src/auth.py", "# @feature: auth\n")
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--repo", str(repo), "trace"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "Feature Traceability: PASS" in result.stdout

    def test_trace_no_check_unbound(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "auth"}])
        # No source files — would normally produce unbound warning
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--repo", str(repo), "--format", "json", "trace", "--no-check-unbound"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["warning_count"] == 0

    def test_trace_custom_glob(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "alpha"}, {"id": "beta"}])
        _write_source_file(repo, "src/a.py", "# @feature: alpha\n")
        _write_source_file(repo, "src/b.txt", "# @feature: beta\n")
        result = subprocess.run(
            [
                "python3",
                "-m",
                "exo.cli",
                "--repo",
                str(repo),
                "--format",
                "json",
                "trace",
                "--glob",
                "**/*.txt",
                "--no-check-unbound",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["tags_total"] == 1
        assert "beta" in data["data"]["bound_features"]


# ──────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_file_no_crash(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "auth"}])
        _write_source_file(repo, "src/empty.py", "")
        report = trace(repo)
        assert report.tags_total == 0

    def test_binary_file_skipped_gracefully(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "auth"}])
        binary_path = repo / "src" / "data.py"
        binary_path.parent.mkdir(parents=True, exist_ok=True)
        binary_path.write_bytes(b"\x00\x01\x02\xff\xfe" * 100)
        # Should not crash
        tags = scan_tags(repo)
        assert isinstance(tags, list)

    def test_nested_tags_last_open_closed_first(self, tmp_path: Path) -> None:
        """@endfeature closes the most recently opened tag (stack behavior)."""
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "outer"}, {"id": "inner"}])
        _write_source_file(
            repo,
            "src/nested.py",
            (
                "# @feature: outer\n"
                "# @feature: inner\n"
                "def nested(): pass\n"
                "# @endfeature\n"
                "def outer_only(): pass\n"
                "# @endfeature\n"
            ),
        )
        tags = scan_tags(repo)
        assert len(tags) == 2
        inner = [t for t in tags if t.feature_id == "inner"][0]
        outer = [t for t in tags if t.feature_id == "outer"][0]
        assert inner.line == 2
        assert inner.end_line == 4
        assert outer.line == 1
        assert outer.end_line == 6

    def test_no_source_files_in_repo(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "auth"}])
        report = trace(repo)
        assert report.tags_total == 0
        assert report.unbound_features == ["auth"]

    def test_features_to_list_empty(self) -> None:
        result = features_to_list([])
        assert result == []

    def test_generate_scope_deny_deduplicates(self) -> None:
        features = [
            FeatureDef(id="a", status="active", allow_agent_edit=False, files=("x.py", "y.py")),
            FeatureDef(id="b", status="active", allow_agent_edit=False, files=("y.py", "z.py")),
        ]
        deny = generate_scope_deny(features)
        assert deny == ["x.py", "y.py", "z.py"]


# ──────────────────────────────────────────────────────────────
# Session-finish trace integration
# ──────────────────────────────────────────────────────────────


def _seed_ticket(repo: Path, ticket_id: str = "TICKET-111") -> None:
    tickets_mod.save_ticket(
        repo,
        {
            "id": ticket_id,
            "type": "feature",
            "title": "Feature trace test ticket",
            "status": "active",
            "priority": 4,
            "scope": {"allow": ["**"], "deny": []},
            "checks": [],
        },
    )


def _start_and_finish(repo: Path, *, summary: str = "done", **finish_kwargs: Any) -> dict[str, Any]:
    """Start a session and finish it, returning the finish result."""
    tickets_mod.acquire_lock(repo, "TICKET-111", owner="agent:test", role="developer", duration_hours=1)
    manager = AgentSessionManager(repo, actor="agent:test")
    manager.start(ticket_id="TICKET-111", vendor="anthropic", model="test-model")
    return manager.finish(summary=summary, set_status="review", **finish_kwargs)


class TestSessionFinishTrace:
    def test_trace_data_in_finish_result_when_features_exist(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _write_features_yaml(repo, [{"id": "auth", "status": "active"}])
        _write_source_file(repo, "src/auth.py", "# @feature: auth\ndef login(): pass\n")
        result = _start_and_finish(repo)
        assert result["trace"] is not None
        assert result["trace"]["passed"] is True
        assert result["trace"]["features_total"] == 1

    def test_trace_data_null_when_no_features_yaml(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        result = _start_and_finish(repo)
        assert result["trace"] is None

    def test_trace_violations_in_finish_result(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _write_features_yaml(repo, [{"id": "auth"}])
        _write_source_file(repo, "src/orphan.py", "# @feature: nonexistent\n")
        result = _start_and_finish(repo)
        assert result["trace"] is not None
        assert result["trace"]["passed"] is False
        assert result["trace"]["error_count"] >= 1

    def test_trace_in_memento(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _write_features_yaml(repo, [{"id": "auth"}])
        _write_source_file(repo, "src/auth.py", "# @feature: auth\n")
        result = _start_and_finish(repo)
        memento = (repo / result["memento_path"]).read_text(encoding="utf-8")
        assert "Feature Traceability: PASS" in memento

    def test_trace_violations_in_memento(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _write_features_yaml(repo, [{"id": "auth"}])
        _write_source_file(repo, "src/bad.py", "# @feature: ghost\n")
        result = _start_and_finish(repo)
        memento = (repo / result["memento_path"]).read_text(encoding="utf-8")
        assert "Feature Traceability: FAIL" in memento
        assert "invalid_tag" in memento

    def test_trace_does_not_block_finish(self, tmp_path: Path) -> None:
        """Trace is advisory — even with errors, session-finish succeeds."""
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _write_features_yaml(repo, [{"id": "auth"}])
        _write_source_file(repo, "src/bad.py", "# @feature: nonexistent\n")
        result = _start_and_finish(repo)
        # Finish should succeed even with trace violations
        assert result["verify"] == "passed"
        assert result["trace"]["passed"] is False

    def test_trace_in_session_index(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _write_features_yaml(repo, [{"id": "auth"}])
        _write_source_file(repo, "src/auth.py", "# @feature: auth\n")
        result = _start_and_finish(repo)
        index_path = repo / result["session_index_path"]
        lines = index_path.read_text(encoding="utf-8").strip().splitlines()
        row = json.loads(lines[-1])
        assert row["trace_passed"] is True
        assert row["trace_violations"] == 0

    def test_trace_violations_in_session_index(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _write_features_yaml(repo, [{"id": "auth"}])
        _write_source_file(repo, "src/bad.py", "# @feature: ghost\n")
        result = _start_and_finish(repo)
        index_path = repo / result["session_index_path"]
        lines = index_path.read_text(encoding="utf-8").strip().splitlines()
        row = json.loads(lines[-1])
        assert row["trace_passed"] is False
        assert row["trace_violations"] >= 1


class TestBannerTrace:
    def test_banner_includes_trace_pass(self) -> None:
        banner = _exo_banner(
            event="finish",
            ticket_id="TICKET-111",
            verify="passed",
            trace_passed=True,
            trace_violations=0,
        )
        assert "trace: PASS" in banner

    def test_banner_includes_trace_fail(self) -> None:
        banner = _exo_banner(
            event="finish",
            ticket_id="TICKET-111",
            verify="passed",
            trace_passed=False,
            trace_violations=3,
        )
        assert "trace: FAIL" in banner
        assert "3 violations" in banner

    def test_banner_no_trace_when_none(self) -> None:
        banner = _exo_banner(
            event="finish",
            ticket_id="TICKET-111",
            verify="passed",
        )
        assert "trace:" not in banner

    def test_finish_banner_has_trace(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _write_features_yaml(repo, [{"id": "auth"}])
        _write_source_file(repo, "src/auth.py", "# @feature: auth\n")
        result = _start_and_finish(repo)
        assert "trace: PASS" in result["exo_banner"]


# ──────────────────────────────────────────────────────────────
# Prune: auto-delete deprecated/deleted feature code blocks
# ──────────────────────────────────────────────────────────────


class TestPrune:
    def test_prune_deleted_block(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(
            repo,
            [
                {"id": "alive", "status": "active"},
                {"id": "dead", "status": "deleted"},
            ],
        )
        _write_source_file(
            repo,
            "src/app.py",
            (
                "# @feature: alive\n"
                "def keep_this(): pass\n"
                "# @endfeature\n"
                "\n"
                "# @feature: dead\n"
                "def remove_this(): pass\n"
                "# @endfeature\n"
                "\n"
                "def standalone(): pass\n"
            ),
        )
        report = prune(repo)
        assert len(report.pruned) == 1
        assert report.pruned[0].feature_id == "dead"
        assert report.total_lines_removed == 3
        assert report.files_modified == ["src/app.py"]
        assert report.features_pruned == ["dead"]
        assert report.dry_run is False

        # Verify file was actually modified
        content = (repo / "src/app.py").read_text(encoding="utf-8")
        assert "remove_this" not in content
        assert "keep_this" in content
        assert "standalone" in content

    def test_prune_dry_run(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "dead", "status": "deleted"}])
        _write_source_file(repo, "src/app.py", ("# @feature: dead\ndef zombie(): pass\n# @endfeature\n"))
        report = prune(repo, dry_run=True)
        assert len(report.pruned) == 1
        assert report.dry_run is True

        # File should NOT be modified
        content = (repo / "src/app.py").read_text(encoding="utf-8")
        assert "zombie" in content

    def test_prune_single_point_tag(self, tmp_path: Path) -> None:
        """Single-point tags (no @endfeature) remove just the tag line."""
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "dead", "status": "deleted"}])
        _write_source_file(repo, "src/app.py", ("def before(): pass\n# @feature: dead\ndef after(): pass\n"))
        report = prune(repo)
        assert len(report.pruned) == 1
        assert report.pruned[0].lines_removed == 1
        content = (repo / "src/app.py").read_text(encoding="utf-8")
        assert "before" in content
        assert "after" in content
        assert "@feature: dead" not in content

    def test_prune_does_not_touch_active_features(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "alive", "status": "active"}])
        _write_source_file(repo, "src/app.py", ("# @feature: alive\ndef keep(): pass\n# @endfeature\n"))
        report = prune(repo)
        assert len(report.pruned) == 0
        assert report.total_lines_removed == 0

    def test_prune_deprecated_only_with_flag(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "old", "status": "deprecated"}])
        _write_source_file(repo, "src/app.py", ("# @feature: old\ndef legacy(): pass\n# @endfeature\n"))
        # Without flag — deprecated NOT pruned
        report = prune(repo)
        assert len(report.pruned) == 0

        # With flag — deprecated IS pruned
        report = prune(repo, include_deprecated=True)
        assert len(report.pruned) == 1
        assert report.features_pruned == ["old"]

    def test_prune_multiple_blocks_same_file(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(
            repo,
            [
                {"id": "dead-a", "status": "deleted"},
                {"id": "dead-b", "status": "deleted"},
                {"id": "alive", "status": "active"},
            ],
        )
        _write_source_file(
            repo,
            "src/app.py",
            (
                "# @feature: dead-a\n"
                "def a(): pass\n"
                "# @endfeature\n"
                "# @feature: alive\n"
                "def keep(): pass\n"
                "# @endfeature\n"
                "# @feature: dead-b\n"
                "def b(): pass\n"
                "# @endfeature\n"
            ),
        )
        report = prune(repo)
        assert len(report.pruned) == 2
        assert report.total_lines_removed == 6
        content = (repo / "src/app.py").read_text(encoding="utf-8")
        assert "keep" in content
        assert "def a" not in content
        assert "def b" not in content

    def test_prune_multiple_files(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "dead", "status": "deleted"}])
        _write_source_file(repo, "src/a.py", "# @feature: dead\ndef a(): pass\n# @endfeature\n")
        _write_source_file(repo, "src/b.py", "# @feature: dead\ndef b(): pass\n# @endfeature\n")
        report = prune(repo)
        assert len(report.pruned) == 2
        assert len(report.files_modified) == 2

    def test_prune_no_prunable_features(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "alive", "status": "active"}])
        _write_source_file(repo, "src/app.py", "# @feature: alive\ndef ok(): pass\n# @endfeature\n")
        report = prune(repo)
        assert len(report.pruned) == 0
        assert report.total_lines_removed == 0
        assert report.files_modified == []

    def test_prune_custom_globs(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "dead", "status": "deleted"}])
        _write_source_file(repo, "src/a.py", "# @feature: dead\ndef a(): pass\n# @endfeature\n")
        _write_source_file(repo, "src/b.txt", "# @feature: dead\nsome text\n# @endfeature\n")
        # Only prune .txt files
        report = prune(repo, globs=["**/*.txt"])
        assert len(report.pruned) == 1
        assert report.pruned[0].file == "src/b.txt"
        # .py file should be untouched
        content = (repo / "src/a.py").read_text(encoding="utf-8")
        assert "dead" in content

    def test_prune_nested_blocks(self, tmp_path: Path) -> None:
        """When a deleted feature is nested inside an active one, only the inner block is removed."""
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(
            repo,
            [
                {"id": "outer", "status": "active"},
                {"id": "inner-dead", "status": "deleted"},
            ],
        )
        _write_source_file(
            repo,
            "src/app.py",
            (
                "# @feature: outer\n"
                "def outer_start(): pass\n"
                "# @feature: inner-dead\n"
                "def dead_code(): pass\n"
                "# @endfeature\n"
                "def outer_end(): pass\n"
                "# @endfeature\n"
            ),
        )
        report = prune(repo)
        assert len(report.pruned) == 1
        assert report.pruned[0].feature_id == "inner-dead"
        content = (repo / "src/app.py").read_text(encoding="utf-8")
        assert "outer_start" in content
        assert "outer_end" in content
        assert "dead_code" not in content


class TestPruneReport:
    def test_prune_to_dict(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "dead", "status": "deleted"}])
        _write_source_file(repo, "src/app.py", "# @feature: dead\ndef x(): pass\n# @endfeature\n")
        report = prune(repo, dry_run=True)
        d = prune_to_dict(report)
        assert d["pruned_count"] == 1
        assert d["total_lines_removed"] == 3
        assert d["dry_run"] is True
        assert "pruned_at" in d
        assert isinstance(d["pruned"], list)

    def test_format_prune_human_pruned(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "dead", "status": "deleted"}])
        _write_source_file(repo, "src/app.py", "# @feature: dead\ndef x(): pass\n# @endfeature\n")
        report = prune(repo)
        text = format_prune_human(report)
        assert "PRUNED" in text
        assert "blocks removed: 1" in text
        assert "dead" in text

    def test_format_prune_human_dry_run(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "dead", "status": "deleted"}])
        _write_source_file(repo, "src/app.py", "# @feature: dead\ndef x(): pass\n# @endfeature\n")
        report = prune(repo, dry_run=True)
        text = format_prune_human(report)
        assert "DRY RUN" in text


class TestCLIPrune:
    def test_prune_json_output(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "dead", "status": "deleted"}])
        _write_source_file(repo, "src/app.py", "# @feature: dead\ndef x(): pass\n# @endfeature\n")
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--repo", str(repo), "--format", "json", "prune", "--dry-run"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert data["data"]["pruned_count"] == 1
        assert data["data"]["dry_run"] is True

    def test_prune_human_output(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "dead", "status": "deleted"}])
        _write_source_file(repo, "src/app.py", "# @feature: dead\ndef x(): pass\n# @endfeature\n")
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--repo", str(repo), "prune", "--dry-run"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "DRY RUN" in result.stdout

    def test_prune_include_deprecated_cli(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "old", "status": "deprecated"}])
        _write_source_file(repo, "src/app.py", "# @feature: old\ndef x(): pass\n# @endfeature\n")
        result = subprocess.run(
            [
                "python3",
                "-m",
                "exo.cli",
                "--repo",
                str(repo),
                "--format",
                "json",
                "prune",
                "--include-deprecated",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["pruned_count"] == 1

    def test_prune_actually_modifies_file(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "dead", "status": "deleted"}])
        _write_source_file(
            repo,
            "src/app.py",
            ("def before(): pass\n# @feature: dead\ndef remove_me(): pass\n# @endfeature\ndef after(): pass\n"),
        )
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--repo", str(repo), "--format", "json", "prune"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        content = (repo / "src/app.py").read_text(encoding="utf-8")
        assert "remove_me" not in content
        assert "before" in content
        assert "after" in content


# ──────────────────────────────────────────────────────────────
# Uncovered code detection
# ──────────────────────────────────────────────────────────────


class TestUncoveredCode:
    def test_uncovered_file_detected(self, tmp_path: Path) -> None:
        """A source file with no tags and no glob coverage should be flagged."""
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "auth", "status": "active", "files": ["src/auth.py"]}])
        _write_source_file(repo, "src/auth.py", "# @feature: auth\ndef login(): pass\n# @endfeature\n")
        _write_source_file(repo, "src/orphan.py", "def orphan(): pass\n")
        report = trace(repo)
        uncovered_kinds = [v for v in report.violations if v.kind == "uncovered_code"]
        assert len(uncovered_kinds) >= 1
        uncovered_files = [v.file for v in uncovered_kinds]
        assert "src/orphan.py" in uncovered_files
        assert "src/orphan.py" in report.uncovered_files

    def test_file_covered_by_glob_not_flagged(self, tmp_path: Path) -> None:
        """A file matched by a feature's files glob should not be flagged."""
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "utils", "status": "active", "files": ["lib/*.py"]}])
        _write_source_file(repo, "lib/helpers.py", "def helper(): pass\n")
        report = trace(repo)
        uncovered_kinds = [v for v in report.violations if v.kind == "uncovered_code"]
        uncovered_files = [v.file for v in uncovered_kinds]
        assert "lib/helpers.py" not in uncovered_files

    def test_file_covered_by_tag_not_flagged(self, tmp_path: Path) -> None:
        """A file with a @feature: tag should not be flagged."""
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "core", "status": "active"}])
        _write_source_file(repo, "src/core.py", "# @feature: core\ndef main(): pass\n# @endfeature\n")
        report = trace(repo)
        uncovered_kinds = [v for v in report.violations if v.kind == "uncovered_code"]
        uncovered_files = [v.file for v in uncovered_kinds]
        assert "src/core.py" not in uncovered_files

    def test_init_files_excluded(self, tmp_path: Path) -> None:
        """__init__.py files should never be flagged as uncovered."""
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "pkg", "status": "active"}])
        _write_source_file(repo, "src/__init__.py", "")
        _write_source_file(repo, "src/main.py", "# @feature: pkg\ndef fn(): pass\n")
        report = trace(repo)
        uncovered_files = [v.file for v in report.violations if v.kind == "uncovered_code"]
        assert "src/__init__.py" not in uncovered_files

    def test_test_files_excluded(self, tmp_path: Path) -> None:
        """Files in tests/ directories should be excluded from uncovered check."""
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "app", "status": "active", "files": ["src/app.py"]}])
        _write_source_file(repo, "src/app.py", "def app(): pass\n")
        _write_source_file(repo, "tests/test_app.py", "def test_it(): pass\n")
        report = trace(repo)
        uncovered_files = [v.file for v in report.violations if v.kind == "uncovered_code"]
        assert "tests/test_app.py" not in uncovered_files

    def test_check_uncovered_disabled(self, tmp_path: Path) -> None:
        """When check_uncovered=False, no uncovered_code violations are produced."""
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "x", "status": "active"}])
        _write_source_file(repo, "src/orphan.py", "def orphan(): pass\n")
        report = trace(repo, check_uncovered=False)
        uncovered_kinds = [v for v in report.violations if v.kind == "uncovered_code"]
        assert len(uncovered_kinds) == 0
        assert report.uncovered_files == []

    def test_uncovered_in_trace_to_dict(self, tmp_path: Path) -> None:
        """The uncovered_files field should appear in the serialized report."""
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "x", "status": "active"}])
        _write_source_file(repo, "src/orphan.py", "def orphan(): pass\n")
        report = trace(repo)
        d = trace_to_dict(report)
        assert "uncovered_files" in d
        assert "src/orphan.py" in d["uncovered_files"]

    def test_uncovered_in_human_output(self, tmp_path: Path) -> None:
        """format_trace_human should mention uncovered files when present."""
        repo = _bootstrap_repo(tmp_path)
        _write_features_yaml(repo, [{"id": "x", "status": "active"}])
        _write_source_file(repo, "src/orphan.py", "def orphan(): pass\n")
        report = trace(repo)
        text = format_trace_human(report)
        assert "uncovered" in text.lower()
