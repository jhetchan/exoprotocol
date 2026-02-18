"""Tests for the Requirement Registry and Traceability Linter.

Covers:
- Requirement manifest loading and validation (.exo/requirements.yaml)
- Code ref scanning (@req: / @implements: annotations)
- Traceability linting (cross-reference refs vs manifest)
- Violation detection (orphan_ref, deleted_ref, deprecated_ref, uncovered_req)
- Report formatting and serialization
- CLI integration (exo requirements, exo trace-reqs)
- Edge cases (empty files, multi-ref lines, missing manifest)
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from exo.kernel import governance as governance_mod
from exo.stdlib.requirements import (
    ACC_TAG_PATTERN,
    REQ_TAG_PATTERN,
    VALID_PRIORITIES,
    VALID_STATUSES,
    format_req_trace_human,
    load_requirements,
    req_trace_to_dict,
    requirements_to_list,
    scan_acc_refs,
    scan_req_refs,
    trace_requirements,
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


def _write_requirements_yaml(repo: Path, requirements: list[dict[str, Any]]) -> Path:
    """Write requirements.yaml with the given requirement list."""
    import yaml

    req_path = repo / ".exo" / "requirements.yaml"
    req_path.write_text(
        yaml.dump({"requirements": requirements}, default_flow_style=False),
        encoding="utf-8",
    )
    return req_path


def _write_source_file(repo: Path, rel_path: str, content: str) -> Path:
    """Write a source file at the given relative path."""
    filepath = repo / rel_path
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(content, encoding="utf-8")
    return filepath


# ── Load Requirements ────────────────────────────────────────────────


class TestLoadRequirements:
    def test_load_basic(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "User auth", "status": "active", "priority": "high"},
                {"id": "REQ-002", "title": "Logging", "status": "active"},
            ],
        )
        reqs = load_requirements(repo)
        assert len(reqs) == 2
        assert reqs[0].id == "REQ-001"
        assert reqs[0].title == "User auth"
        assert reqs[0].status == "active"
        assert reqs[0].priority == "high"
        assert reqs[1].id == "REQ-002"
        assert reqs[1].priority == "medium"  # default

    def test_load_defaults(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Basic req"},
            ],
        )
        reqs = load_requirements(repo)
        assert reqs[0].status == "active"
        assert reqs[0].priority == "medium"
        assert reqs[0].description == ""
        assert reqs[0].tags == ()

    def test_load_with_tags(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth", "tags": ["security", "auth"]},
            ],
        )
        reqs = load_requirements(repo)
        assert reqs[0].tags == ("security", "auth")

    def test_missing_manifest_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        try:
            load_requirements(repo)
            raise AssertionError("Should have raised")
        except Exception as e:
            assert "REQUIREMENTS_MANIFEST_MISSING" in str(e)

    def test_missing_id_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(repo, [{"title": "No ID"}])
        try:
            load_requirements(repo)
            raise AssertionError("Should have raised")
        except Exception as e:
            assert "REQUIREMENTS_ENTRY_MISSING_ID" in str(e)

    def test_missing_title_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(repo, [{"id": "REQ-001"}])
        try:
            load_requirements(repo)
            raise AssertionError("Should have raised")
        except Exception as e:
            assert "REQUIREMENTS_ENTRY_MISSING_TITLE" in str(e)

    def test_duplicate_id_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "First"},
                {"id": "REQ-001", "title": "Dupe"},
            ],
        )
        try:
            load_requirements(repo)
            raise AssertionError("Should have raised")
        except Exception as e:
            assert "REQUIREMENTS_DUPLICATE_ID" in str(e)

    def test_invalid_status_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Bad status", "status": "wontfix"},
            ],
        )
        try:
            load_requirements(repo)
            raise AssertionError("Should have raised")
        except Exception as e:
            assert "REQUIREMENTS_INVALID_STATUS" in str(e)

    def test_invalid_priority_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Bad priority", "priority": "critical"},
            ],
        )
        try:
            load_requirements(repo)
            raise AssertionError("Should have raised")
        except Exception as e:
            assert "REQUIREMENTS_INVALID_PRIORITY" in str(e)

    def test_invalid_entry_type_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(repo, ["not a dict"])
        try:
            load_requirements(repo)
            raise AssertionError("Should have raised")
        except Exception as e:
            assert "REQUIREMENTS_ENTRY_INVALID" in str(e)


# ── Scan Req Refs ────────────────────────────────────────────────────


class TestScanReqRefs:
    def test_scan_req_tag(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\ndef login(): pass\n")
        refs = scan_req_refs(repo)
        assert len(refs) == 1
        assert refs[0].req_id == "REQ-001"
        assert refs[0].file == "src/auth.py"
        assert refs[0].line == 1

    def test_scan_implements_tag(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_source_file(repo, "src/auth.py", "# @implements: REQ-001\ndef login(): pass\n")
        refs = scan_req_refs(repo)
        assert len(refs) == 1
        assert refs[0].req_id == "REQ-001"

    def test_scan_multi_ref_line(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001, REQ-002\ndef login(): pass\n")
        refs = scan_req_refs(repo)
        assert len(refs) == 2
        assert refs[0].req_id == "REQ-001"
        assert refs[1].req_id == "REQ-002"

    def test_scan_js_comment(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_source_file(repo, "src/auth.js", "// @req: REQ-001\nfunction login() {}\n")
        refs = scan_req_refs(repo)
        assert len(refs) == 1
        assert refs[0].req_id == "REQ-001"

    def test_scan_case_insensitive(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_source_file(repo, "src/auth.py", "# @REQ: REQ-001\n# @Implements: REQ-002\n")
        refs = scan_req_refs(repo)
        assert len(refs) == 2

    def test_scan_excludes_skip_dirs(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_source_file(repo, "node_modules/lib.js", "// @req: REQ-001\n")
        refs = scan_req_refs(repo)
        assert len(refs) == 0

    def test_scan_custom_globs(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        _write_source_file(repo, "src/auth.js", "// @req: REQ-002\n")
        refs = scan_req_refs(repo, globs=["**/*.py"])
        assert len(refs) == 1
        assert refs[0].req_id == "REQ-001"

    def test_scan_multiple_files(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        _write_source_file(repo, "src/log.py", "# @req: REQ-002\n# @req: REQ-003\n")
        refs = scan_req_refs(repo)
        req_ids = [r.req_id for r in refs]
        assert "REQ-001" in req_ids
        assert "REQ-002" in req_ids
        assert "REQ-003" in req_ids


# ── Trace Requirements ───────────────────────────────────────────────


class TestTraceRequirements:
    def test_clean_trace(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth"},
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\ndef login(): pass\n")
        report = trace_requirements(repo)
        assert report.passed
        assert report.reqs_total == 1
        assert report.reqs_active == 1
        assert report.refs_total == 1
        assert len(report.violations) == 0
        assert "REQ-001" in report.covered_reqs

    def test_orphan_ref(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth"},
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-999\ndef login(): pass\n")
        report = trace_requirements(repo)
        assert not report.passed
        orphans = [v for v in report.violations if v.kind == "orphan_ref"]
        assert len(orphans) == 1
        assert orphans[0].req_id == "REQ-999"
        assert orphans[0].severity == "error"

    def test_deleted_ref(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth", "status": "deleted"},
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        report = trace_requirements(repo)
        assert not report.passed
        violations = [v for v in report.violations if v.kind == "deleted_ref"]
        assert len(violations) == 1
        assert violations[0].severity == "error"

    def test_deprecated_ref(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth", "status": "deprecated"},
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        report = trace_requirements(repo)
        assert report.passed  # deprecated is a warning, not error
        violations = [v for v in report.violations if v.kind == "deprecated_ref"]
        assert len(violations) == 1
        assert violations[0].severity == "warning"
        assert "REQ-001" in report.deprecated_with_refs

    def test_uncovered_req(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth"},
            ],
        )
        # No source file references REQ-001
        report = trace_requirements(repo)
        assert report.passed  # uncovered is a warning
        violations = [v for v in report.violations if v.kind == "uncovered_req"]
        assert len(violations) == 1
        assert violations[0].severity == "warning"
        assert "REQ-001" in report.uncovered_reqs

    def test_uncovered_skip_deleted(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth", "status": "deleted"},
            ],
        )
        report = trace_requirements(repo)
        # Deleted reqs shouldn't be flagged as uncovered
        uncovered_violations = [v for v in report.violations if v.kind == "uncovered_req"]
        assert len(uncovered_violations) == 0

    def test_no_check_uncovered(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth"},
            ],
        )
        report = trace_requirements(repo, check_uncovered=False)
        assert len(report.violations) == 0

    def test_mixed_violations(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth"},
                {"id": "REQ-002", "title": "Logging", "status": "deleted"},
                {"id": "REQ-003", "title": "Caching", "status": "deprecated"},
            ],
        )
        _write_source_file(repo, "src/app.py", ("# @req: REQ-001\n# @req: REQ-002\n# @req: REQ-003\n# @req: REQ-999\n"))
        report = trace_requirements(repo)
        assert not report.passed  # orphan_ref + deleted_ref
        kinds = {v.kind for v in report.violations}
        assert "orphan_ref" in kinds
        assert "deleted_ref" in kinds
        assert "deprecated_ref" in kinds
        assert "REQ-001" in report.covered_reqs
        assert "REQ-003" in report.deprecated_with_refs

    def test_implements_and_req_both_work(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth"},
                {"id": "REQ-002", "title": "Logging"},
            ],
        )
        _write_source_file(repo, "src/app.py", ("# @req: REQ-001\n# @implements: REQ-002\n"))
        report = trace_requirements(repo)
        assert report.passed
        assert "REQ-001" in report.covered_reqs
        assert "REQ-002" in report.covered_reqs

    def test_custom_globs(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth"},
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        _write_source_file(repo, "src/auth.js", "// @req: REQ-999\n")
        report = trace_requirements(repo, globs=["**/*.py"])
        # Only py files scanned — REQ-999 not found
        assert report.passed
        orphans = [v for v in report.violations if v.kind == "orphan_ref"]
        assert len(orphans) == 0


# ── Report Output ────────────────────────────────────────────────────


class TestReportOutput:
    def test_to_dict_structure(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth"},
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        report = trace_requirements(repo)
        d = req_trace_to_dict(report)
        assert d["passed"] is True
        assert d["reqs_total"] == 1
        assert d["reqs_active"] == 1
        assert d["refs_total"] == 1
        assert d["violation_count"] == 0
        assert d["error_count"] == 0
        assert d["warning_count"] == 0
        assert "checked_at" in d

    def test_to_dict_with_violations(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth"},
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-999\n")
        report = trace_requirements(repo)
        d = req_trace_to_dict(report)
        assert d["passed"] is False
        assert d["error_count"] == 1
        assert len(d["violations"]) >= 1

    def test_human_format_pass(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth"},
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        report = trace_requirements(repo)
        text = format_req_trace_human(report)
        assert "PASS" in text
        assert "requirements: 1 total" in text

    def test_human_format_fail(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth"},
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-999\n")
        report = trace_requirements(repo)
        text = format_req_trace_human(report)
        assert "FAIL" in text
        assert "orphan_ref" in text

    def test_human_format_deprecated_with_refs(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth", "status": "deprecated"},
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        report = trace_requirements(repo)
        text = format_req_trace_human(report)
        assert "deprecated with refs" in text


# ── Requirements To List ─────────────────────────────────────────────


class TestRequirementsToList:
    def test_round_trip(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth", "priority": "high", "tags": ["security"]},
            ],
        )
        reqs = load_requirements(repo)
        lst = requirements_to_list(reqs)
        assert len(lst) == 1
        assert lst[0]["id"] == "REQ-001"
        assert lst[0]["title"] == "Auth"
        assert lst[0]["priority"] == "high"
        assert lst[0]["tags"] == ("security",)


# ── Regex Patterns ───────────────────────────────────────────────────


class TestReqTagPatterns:
    def test_python_req(self) -> None:
        assert REQ_TAG_PATTERN.search("# @req: REQ-001")

    def test_python_implements(self) -> None:
        assert REQ_TAG_PATTERN.search("# @implements: REQ-001")

    def test_js_req(self) -> None:
        assert REQ_TAG_PATTERN.search("// @req: REQ-001")

    def test_js_implements(self) -> None:
        assert REQ_TAG_PATTERN.search("// @implements: REQ-002")

    def test_case_insensitive(self) -> None:
        assert REQ_TAG_PATTERN.search("# @REQ: REQ-001")
        assert REQ_TAG_PATTERN.search("# @Implements: REQ-001")

    def test_multi_ref(self) -> None:
        m = REQ_TAG_PATTERN.search("# @req: REQ-001, REQ-002, REQ-003")
        assert m
        assert "REQ-001, REQ-002, REQ-003" in m.group(1)


# ── Valid Constants ──────────────────────────────────────────────────


class TestValidConstants:
    def test_valid_statuses(self) -> None:
        assert frozenset({"active", "deprecated", "deleted"}) == VALID_STATUSES

    def test_valid_priorities(self) -> None:
        assert frozenset({"high", "medium", "low"}) == VALID_PRIORITIES


# ── CLI Integration ──────────────────────────────────────────────────


class TestCLIRequirements:
    def test_json_output(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth", "priority": "high"},
                {"id": "REQ-002", "title": "Logging"},
            ],
        )
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), "requirements"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"]
        assert data["data"]["count"] == 2
        assert data["data"]["requirements"][0]["id"] == "REQ-001"

    def test_status_filter(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth", "status": "active"},
                {"id": "REQ-002", "title": "Old logging", "status": "deprecated"},
            ],
        )
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), "requirements", "--status", "active"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["count"] == 1
        assert data["data"]["requirements"][0]["id"] == "REQ-001"

    def test_missing_manifest(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), "requirements"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        data = json.loads(result.stdout)
        assert not data["ok"]
        assert "REQUIREMENTS_MANIFEST_MISSING" in data["error"]["code"]


class TestCLITraceReqs:
    def test_pass(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth"},
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\ndef login(): pass\n")
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), "trace-reqs"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"]
        assert data["data"]["passed"]

    def test_fail(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth"},
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-999\n")
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), "trace-reqs"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert not data["data"]["passed"]
        assert data["data"]["error_count"] >= 1

    def test_human_output(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth"},
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "human", "--repo", str(repo), "trace-reqs"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "Requirement Traceability: PASS" in result.stdout

    def test_no_check_uncovered(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth"},
            ],
        )
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), "trace-reqs", "--no-check-uncovered"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["violation_count"] == 0

    def test_custom_glob(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth"},
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        _write_source_file(repo, "src/bad.js", "// @req: REQ-999\n")
        result = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo), "trace-reqs", "--glob", "**/*.py"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["passed"]


# ── Edge Cases ───────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_file(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth"},
            ],
        )
        _write_source_file(repo, "src/empty.py", "")
        report = trace_requirements(repo)
        assert len(report.covered_reqs) == 0

    def test_no_source_files(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth"},
            ],
        )
        report = trace_requirements(repo)
        assert report.refs_total == 0

    def test_dedup_coverage(self, tmp_path: Path) -> None:
        """Same requirement referenced in multiple files only counts once in covered."""
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth"},
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        _write_source_file(repo, "src/login.py", "# @req: REQ-001\n")
        report = trace_requirements(repo)
        assert report.covered_reqs.count("REQ-001") == 1
        assert report.refs_total == 2

    def test_all_statuses(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Active", "status": "active"},
                {"id": "REQ-002", "title": "Deprecated", "status": "deprecated"},
                {"id": "REQ-003", "title": "Deleted", "status": "deleted"},
            ],
        )
        reqs = load_requirements(repo)
        assert len(reqs) == 3
        report = trace_requirements(repo)
        assert report.reqs_active == 1
        assert report.reqs_deprecated == 1
        assert report.reqs_deleted == 1

    def test_all_priorities(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "High", "priority": "high"},
                {"id": "REQ-002", "title": "Medium", "priority": "medium"},
                {"id": "REQ-003", "title": "Low", "priority": "low"},
            ],
        )
        reqs = load_requirements(repo)
        assert reqs[0].priority == "high"
        assert reqs[1].priority == "medium"
        assert reqs[2].priority == "low"

    def test_requirements_list_not_a_list(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        import yaml

        req_path = repo / ".exo" / "requirements.yaml"
        req_path.write_text(
            yaml.dump({"requirements": "not a list"}, default_flow_style=False),
            encoding="utf-8",
        )
        try:
            load_requirements(repo)
            raise AssertionError("Should have raised")
        except Exception as e:
            assert "REQUIREMENTS_MANIFEST_INVALID" in str(e)


# ── Acceptance Criteria: Schema ─────────────────────────────────────


class TestAcceptanceSchema:
    """Tests for the acceptance field on RequirementDef."""

    def test_load_with_acceptance(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {
                    "id": "REQ-001",
                    "title": "Auth",
                    "acceptance": ["ACC-001", "ACC-002"],
                },
            ],
        )
        reqs = load_requirements(repo)
        assert reqs[0].acceptance == ("ACC-001", "ACC-002")

    def test_load_without_acceptance_defaults_empty(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [{"id": "REQ-001", "title": "Auth"}],
        )
        reqs = load_requirements(repo)
        assert reqs[0].acceptance == ()

    def test_duplicate_acc_id_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {"id": "REQ-001", "title": "Auth", "acceptance": ["ACC-001"]},
                {"id": "REQ-002", "title": "Log", "acceptance": ["ACC-001"]},
            ],
        )
        try:
            load_requirements(repo)
            raise AssertionError("Should have raised")
        except Exception as e:
            assert "REQUIREMENTS_DUPLICATE_ACC" in str(e)

    def test_duplicate_acc_within_same_req_raises(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {
                    "id": "REQ-001",
                    "title": "Auth",
                    "acceptance": ["ACC-001", "ACC-001"],
                },
            ],
        )
        try:
            load_requirements(repo)
            raise AssertionError("Should have raised")
        except Exception as e:
            assert "REQUIREMENTS_DUPLICATE_ACC" in str(e)

    def test_acceptance_non_list_ignored(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [{"id": "REQ-001", "title": "Auth", "acceptance": "not-a-list"}],
        )
        reqs = load_requirements(repo)
        assert reqs[0].acceptance == ()

    def test_acceptance_in_round_trip(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {
                    "id": "REQ-001",
                    "title": "Auth",
                    "acceptance": ["ACC-001", "ACC-002"],
                },
            ],
        )
        reqs = load_requirements(repo)
        lst = requirements_to_list(reqs)
        assert lst[0]["acceptance"] == ("ACC-001", "ACC-002")


# ── ACC Tag Pattern ─────────────────────────────────────────────────


class TestAccTagPattern:
    """Tests for the ACC_TAG_PATTERN regex."""

    def test_python_comment(self) -> None:
        m = ACC_TAG_PATTERN.search("# @acc: ACC-001")
        assert m
        assert "ACC-001" in m.group(1)

    def test_js_comment(self) -> None:
        m = ACC_TAG_PATTERN.search("// @acc: ACC-001")
        assert m
        assert "ACC-001" in m.group(1)

    def test_case_insensitive(self) -> None:
        assert ACC_TAG_PATTERN.search("# @ACC: ACC-001")
        assert ACC_TAG_PATTERN.search("# @Acc: ACC-001")

    def test_multi_ref(self) -> None:
        m = ACC_TAG_PATTERN.search("# @acc: ACC-001, ACC-002")
        assert m
        assert "ACC-001, ACC-002" in m.group(1)

    def test_no_match_without_prefix(self) -> None:
        assert not ACC_TAG_PATTERN.search("@acc: ACC-001")


# ── Scan ACC Refs ───────────────────────────────────────────────────


class TestScanAccRefs:
    """Tests for scanning test files for @acc: annotations."""

    def test_scan_in_tests_dir(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        (repo / "tests").mkdir(parents=True, exist_ok=True)
        (repo / "tests" / "test_auth.py").write_text(
            "# @acc: ACC-001\ndef test_login(): pass\n",
            encoding="utf-8",
        )
        refs = scan_acc_refs(repo)
        assert len(refs) == 1
        assert refs[0].acc_id == "ACC-001"
        assert "tests/test_auth.py" in refs[0].file
        assert refs[0].line == 1

    def test_scan_in_test_dir(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        (repo / "test").mkdir(parents=True, exist_ok=True)
        (repo / "test" / "test_auth.py").write_text(
            "# @acc: ACC-001\ndef test_login(): pass\n",
            encoding="utf-8",
        )
        refs = scan_acc_refs(repo)
        assert len(refs) == 1
        assert refs[0].acc_id == "ACC-001"

    def test_scan_multi_ref_line(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        (repo / "tests").mkdir(parents=True, exist_ok=True)
        (repo / "tests" / "test_auth.py").write_text(
            "# @acc: ACC-001, ACC-002\ndef test_login(): pass\n",
            encoding="utf-8",
        )
        refs = scan_acc_refs(repo)
        assert len(refs) == 2
        assert refs[0].acc_id == "ACC-001"
        assert refs[1].acc_id == "ACC-002"

    def test_scan_ignores_source_dirs(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        # Put @acc: in source dir — should NOT be found by scan_acc_refs
        _write_source_file(repo, "src/auth.py", "# @acc: ACC-001\n")
        refs = scan_acc_refs(repo)
        assert len(refs) == 0

    def test_scan_no_test_dirs(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        # Neither tests/ nor test/ exist
        refs = scan_acc_refs(repo)
        assert len(refs) == 0

    def test_scan_nested_test_file(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        (repo / "tests" / "integration").mkdir(parents=True, exist_ok=True)
        (repo / "tests" / "integration" / "test_flow.py").write_text(
            "# @acc: ACC-003\ndef test_flow(): pass\n",
            encoding="utf-8",
        )
        refs = scan_acc_refs(repo)
        assert len(refs) == 1
        assert refs[0].acc_id == "ACC-003"

    def test_scan_js_test_file(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        (repo / "tests").mkdir(parents=True, exist_ok=True)
        (repo / "tests" / "auth.test.js").write_text(
            "// @acc: ACC-001\ntest('login', () => {});\n",
            encoding="utf-8",
        )
        refs = scan_acc_refs(repo)
        assert len(refs) == 1
        assert refs[0].acc_id == "ACC-001"


# ── Trace with ACC ──────────────────────────────────────────────────


class TestTraceWithAcc:
    """Tests for trace_requirements with check_tests=True."""

    def test_clean_trace_with_acc(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {
                    "id": "REQ-001",
                    "title": "Auth",
                    "acceptance": ["ACC-001", "ACC-002"],
                },
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        (repo / "tests").mkdir(parents=True, exist_ok=True)
        (repo / "tests" / "test_auth.py").write_text(
            "# @acc: ACC-001\n# @acc: ACC-002\ndef test_login(): pass\n",
            encoding="utf-8",
        )
        report = trace_requirements(repo, check_tests=True)
        assert report.passed
        assert report.acc_total == 2
        assert report.acc_tested == 2
        assert report.untested_accs == []

    def test_untested_acc_violation(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {
                    "id": "REQ-001",
                    "title": "Auth",
                    "acceptance": ["ACC-001", "ACC-002"],
                },
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        (repo / "tests").mkdir(parents=True, exist_ok=True)
        (repo / "tests" / "test_auth.py").write_text(
            "# @acc: ACC-001\ndef test_login(): pass\n",
            encoding="utf-8",
        )
        report = trace_requirements(repo, check_tests=True)
        assert not report.passed
        assert report.acc_total == 2
        assert report.acc_tested == 1
        assert "ACC-002" in report.untested_accs
        untested_violations = [v for v in report.violations if v.kind == "untested_acc"]
        assert len(untested_violations) == 1
        assert untested_violations[0].severity == "error"
        assert "ACC-002" in untested_violations[0].message
        assert "REQ-001" in untested_violations[0].message

    def test_acc_orphan_violation(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {
                    "id": "REQ-001",
                    "title": "Auth",
                    "acceptance": ["ACC-001"],
                },
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        (repo / "tests").mkdir(parents=True, exist_ok=True)
        (repo / "tests" / "test_auth.py").write_text(
            "# @acc: ACC-001\n# @acc: ACC-GHOST\ndef test_login(): pass\n",
            encoding="utf-8",
        )
        report = trace_requirements(repo, check_tests=True)
        assert not report.passed
        orphans = [v for v in report.violations if v.kind == "acc_orphan"]
        assert len(orphans) == 1
        assert orphans[0].req_id == "ACC-GHOST"
        assert orphans[0].severity == "error"

    def test_check_tests_false_skips_acc(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {
                    "id": "REQ-001",
                    "title": "Auth",
                    "acceptance": ["ACC-001"],
                },
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        # No test files — but check_tests=False so should pass
        report = trace_requirements(repo, check_tests=False)
        assert report.passed
        assert report.acc_total == 0
        assert report.acc_tested == 0
        assert report.untested_accs is None

    def test_deleted_req_acc_excluded(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {
                    "id": "REQ-001",
                    "title": "Auth",
                    "status": "deleted",
                    "acceptance": ["ACC-001"],
                },
            ],
        )
        report = trace_requirements(repo, check_tests=True)
        # Deleted requirement's ACCs should not be counted
        assert report.acc_total == 0

    def test_deprecated_req_acc_included(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {
                    "id": "REQ-001",
                    "title": "Auth",
                    "status": "deprecated",
                    "acceptance": ["ACC-001"],
                },
            ],
        )
        report = trace_requirements(repo, check_tests=True)
        # Deprecated requirement's ACCs should still be counted
        assert report.acc_total == 1

    def test_mixed_acc_violations(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {
                    "id": "REQ-001",
                    "title": "Auth",
                    "acceptance": ["ACC-001", "ACC-002", "ACC-003"],
                },
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        (repo / "tests").mkdir(parents=True, exist_ok=True)
        (repo / "tests" / "test_auth.py").write_text(
            "# @acc: ACC-001\n# @acc: ACC-ORPHAN\ndef test_login(): pass\n",
            encoding="utf-8",
        )
        report = trace_requirements(repo, check_tests=True)
        assert not report.passed
        kinds = {v.kind for v in report.violations}
        assert "untested_acc" in kinds  # ACC-002, ACC-003 untested
        assert "acc_orphan" in kinds  # ACC-ORPHAN not in manifest
        assert report.acc_total == 3
        assert report.acc_tested == 1
        assert "ACC-002" in report.untested_accs
        assert "ACC-003" in report.untested_accs

    def test_no_acceptance_with_check_tests(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [{"id": "REQ-001", "title": "Auth"}],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        report = trace_requirements(repo, check_tests=True)
        assert report.passed
        assert report.acc_total == 0
        assert report.acc_tested == 0
        assert report.untested_accs == []


# ── ACC Report Output ───────────────────────────────────────────────


class TestAccReportOutput:
    """Tests for ACC data in report serialization and formatting."""

    def test_to_dict_includes_acc_fields(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {
                    "id": "REQ-001",
                    "title": "Auth",
                    "acceptance": ["ACC-001", "ACC-002"],
                },
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        (repo / "tests").mkdir(parents=True, exist_ok=True)
        (repo / "tests" / "test_auth.py").write_text(
            "# @acc: ACC-001\n",
            encoding="utf-8",
        )
        report = trace_requirements(repo, check_tests=True)
        d = req_trace_to_dict(report)
        assert d["acc_total"] == 2
        assert d["acc_tested"] == 1
        assert "untested_accs" in d
        assert "ACC-002" in d["untested_accs"]

    def test_to_dict_no_untested_when_check_tests_false(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {
                    "id": "REQ-001",
                    "title": "Auth",
                    "acceptance": ["ACC-001"],
                },
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        report = trace_requirements(repo, check_tests=False)
        d = req_trace_to_dict(report)
        assert d["acc_total"] == 0
        assert "untested_accs" not in d

    def test_human_format_shows_acc_line(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {
                    "id": "REQ-001",
                    "title": "Auth",
                    "acceptance": ["ACC-001", "ACC-002"],
                },
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        (repo / "tests").mkdir(parents=True, exist_ok=True)
        (repo / "tests" / "test_auth.py").write_text(
            "# @acc: ACC-001\n# @acc: ACC-002\n",
            encoding="utf-8",
        )
        report = trace_requirements(repo, check_tests=True)
        text = format_req_trace_human(report)
        assert "acceptance criteria: 2 defined, 2 tested" in text

    def test_human_format_no_acc_line_when_zero(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [{"id": "REQ-001", "title": "Auth"}],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        report = trace_requirements(repo, check_tests=False)
        text = format_req_trace_human(report)
        assert "acceptance criteria" not in text

    def test_human_format_shows_untested_violations(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {
                    "id": "REQ-001",
                    "title": "Auth",
                    "acceptance": ["ACC-001"],
                },
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        report = trace_requirements(repo, check_tests=True)
        text = format_req_trace_human(report)
        assert "untested_acc" in text
        assert "ACC-001" in text


# ── CLI --check-tests ───────────────────────────────────────────────


class TestCLICheckTests:
    """Tests for CLI trace-reqs --check-tests flag."""

    def test_check_tests_pass(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {
                    "id": "REQ-001",
                    "title": "Auth",
                    "acceptance": ["ACC-001"],
                },
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        (repo / "tests").mkdir(parents=True, exist_ok=True)
        (repo / "tests" / "test_auth.py").write_text(
            "# @acc: ACC-001\ndef test_login(): pass\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                "python3",
                "-m",
                "exo.cli",
                "--format",
                "json",
                "--repo",
                str(repo),
                "trace-reqs",
                "--check-tests",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"]
        assert data["data"]["passed"]
        assert data["data"]["acc_total"] == 1
        assert data["data"]["acc_tested"] == 1

    def test_check_tests_fail(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {
                    "id": "REQ-001",
                    "title": "Auth",
                    "acceptance": ["ACC-001"],
                },
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        # No test files with @acc: annotation
        result = subprocess.run(
            [
                "python3",
                "-m",
                "exo.cli",
                "--format",
                "json",
                "--repo",
                str(repo),
                "trace-reqs",
                "--check-tests",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert not data["data"]["passed"]
        assert data["data"]["acc_total"] == 1
        assert data["data"]["acc_tested"] == 0
        assert "ACC-001" in data["data"]["untested_accs"]

    def test_without_check_tests_flag(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_requirements_yaml(
            repo,
            [
                {
                    "id": "REQ-001",
                    "title": "Auth",
                    "acceptance": ["ACC-001"],
                },
            ],
        )
        _write_source_file(repo, "src/auth.py", "# @req: REQ-001\n")
        # No --check-tests flag: ACC not checked, should pass
        result = subprocess.run(
            [
                "python3",
                "-m",
                "exo.cli",
                "--format",
                "json",
                "--repo",
                str(repo),
                "trace-reqs",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["passed"]
        assert data["data"]["acc_total"] == 0
