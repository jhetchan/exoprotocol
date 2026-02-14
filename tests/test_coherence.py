"""Tests for semantic coherence detection.

Covers:
- CoherenceViolation dataclass
- CoherenceReport properties
- Co-update rule checks
- Docstring freshness detection
- check_coherence orchestrator
- Serialization and human output
- Drift composite integration
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from exo.kernel import governance as governance_mod
from exo.kernel.utils import dump_yaml, load_yaml
from exo.stdlib.defaults import DEFAULT_CONFIG, DEFAULT_CONSTITUTION


# ── Helpers ──────────────────────────────────────────────────────


def _bootstrap_repo(tmp_path: Path) -> Path:
    repo = tmp_path
    exo_dir = repo / ".exo"
    exo_dir.mkdir(parents=True, exist_ok=True)
    constitution = DEFAULT_CONSTITUTION
    (exo_dir / "CONSTITUTION.md").write_text(constitution, encoding="utf-8")
    dump_yaml(exo_dir / "config.yaml", DEFAULT_CONFIG)
    for d in ["tickets", "locks", "logs", "memory", "memory/reflections", "memory/sessions", "cache", "cache/sessions"]:
        (exo_dir / d).mkdir(parents=True, exist_ok=True)
    governance_mod.compile_constitution(repo)
    return repo


def _init_git(repo: Path) -> None:
    """Initialize a git repo with an initial commit."""
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)


# ── CoherenceViolation ───────────────────────────────────────────


class TestCoherenceViolation:
    def test_violation_has_correct_fields(self) -> None:
        from exo.stdlib.coherence import CoherenceViolation

        v = CoherenceViolation(
            kind="co_update",
            severity="warning",
            file="README.md",
            message="API docs: src/api.py changed but README.md was not updated",
        )
        assert v.kind == "co_update"
        assert v.severity == "warning"
        assert v.file == "README.md"
        assert "README.md" in v.message

    def test_warning_severity_default_pattern(self) -> None:
        from exo.stdlib.coherence import CoherenceViolation

        v = CoherenceViolation(
            kind="stale_docstring",
            severity="warning",
            file="src/foo.py",
            message="stale",
        )
        assert v.severity == "warning"

    def test_detail_dict_populated(self) -> None:
        from exo.stdlib.coherence import CoherenceViolation

        detail = {"label": "API docs", "changed": ["src/api.py"], "missing": ["README.md"]}
        v = CoherenceViolation(
            kind="co_update",
            severity="warning",
            file="README.md",
            message="stale",
            detail=detail,
        )
        assert v.detail["label"] == "API docs"
        assert "src/api.py" in v.detail["changed"]


# ── CoherenceReport ──────────────────────────────────────────────


class TestCoherenceReport:
    def test_empty_violations_passed(self) -> None:
        from exo.stdlib.coherence import CoherenceReport

        r = CoherenceReport(violations=[], files_checked=0, functions_checked=0, checked_at="now")
        assert r.passed is True

    def test_warning_only_passed(self) -> None:
        from exo.stdlib.coherence import CoherenceReport, CoherenceViolation

        v = CoherenceViolation(kind="co_update", severity="warning", file="a.py", message="warn")
        r = CoherenceReport(violations=[v], files_checked=1, functions_checked=0, checked_at="now")
        assert r.passed is True

    def test_error_violation_fails(self) -> None:
        from exo.stdlib.coherence import CoherenceReport, CoherenceViolation

        v = CoherenceViolation(kind="co_update", severity="error", file="a.py", message="err")
        r = CoherenceReport(violations=[v], files_checked=1, functions_checked=0, checked_at="now")
        assert r.passed is False

    def test_warning_count(self) -> None:
        from exo.stdlib.coherence import CoherenceReport, CoherenceViolation

        vs = [
            CoherenceViolation(kind="co_update", severity="warning", file="a.py", message="w1"),
            CoherenceViolation(kind="stale_docstring", severity="warning", file="b.py", message="w2"),
            CoherenceViolation(kind="co_update", severity="error", file="c.py", message="e1"),
        ]
        r = CoherenceReport(violations=vs, files_checked=3, functions_checked=2, checked_at="now")
        assert r.warning_count == 2


# ── Co-update Rules ──────────────────────────────────────────────


class TestCoUpdateRules:
    def test_no_rules_no_violations(self) -> None:
        from exo.stdlib.coherence import check_co_updates

        violations = check_co_updates(["src/api.py"], [])
        assert violations == []

    def test_all_files_changed_no_violation(self) -> None:
        from exo.stdlib.coherence import check_co_updates

        rules = [{"files": ["src/api.py", "README.md"], "label": "API docs"}]
        violations = check_co_updates(["src/api.py", "README.md"], rules)
        assert violations == []

    def test_partial_change_violation(self) -> None:
        from exo.stdlib.coherence import check_co_updates

        rules = [{"files": ["src/api.py", "README.md"], "label": "API docs"}]
        violations = check_co_updates(["src/api.py"], rules)
        assert len(violations) == 1
        assert violations[0].file == "README.md"
        assert violations[0].kind == "co_update"
        assert "API docs" in violations[0].message

    def test_no_files_changed_no_violation(self) -> None:
        from exo.stdlib.coherence import check_co_updates

        rules = [{"files": ["src/api.py", "README.md"], "label": "API docs"}]
        violations = check_co_updates(["other.py"], rules)
        assert violations == []

    def test_multiple_rules_independent(self) -> None:
        from exo.stdlib.coherence import check_co_updates

        rules = [
            {"files": ["src/api.py", "README.md"], "label": "API docs"},
            {"files": ["src/config.py", "docs/config.md"], "label": "Config docs"},
        ]
        violations = check_co_updates(["src/api.py", "src/config.py"], rules)
        assert len(violations) == 2
        files = {v.file for v in violations}
        assert files == {"README.md", "docs/config.md"}

    def test_label_in_message(self) -> None:
        from exo.stdlib.coherence import check_co_updates

        rules = [{"files": ["a.py", "b.py"], "label": "My label"}]
        violations = check_co_updates(["a.py"], rules)
        assert "My label" in violations[0].message

    def test_empty_files_list_skipped(self) -> None:
        from exo.stdlib.coherence import check_co_updates

        rules = [{"files": [], "label": "empty"}]
        violations = check_co_updates(["a.py"], rules)
        assert violations == []


# ── Docstring Freshness ──────────────────────────────────────────


class TestFindPythonFunctions:
    def test_simple_function(self) -> None:
        from exo.stdlib.coherence import _find_python_functions

        code = textwrap.dedent('''\
            def hello():
                """Say hello."""
                print("hello")
        ''')
        fns = _find_python_functions(code)
        assert len(fns) == 1
        assert fns[0].name == "hello"
        assert fns[0].def_line == 1
        assert fns[0].docstring_start == 2
        assert fns[0].docstring_end == 2

    def test_no_docstring(self) -> None:
        from exo.stdlib.coherence import _find_python_functions

        code = textwrap.dedent("""\
            def hello():
                print("hello")
        """)
        fns = _find_python_functions(code)
        assert len(fns) == 1
        assert fns[0].docstring_start == 0
        assert fns[0].docstring_end == 0

    def test_multiline_docstring(self) -> None:
        from exo.stdlib.coherence import _find_python_functions

        code = textwrap.dedent('''\
            def hello():
                """
                Say hello.
                More details.
                """
                print("hello")
        ''')
        fns = _find_python_functions(code)
        assert len(fns) == 1
        assert fns[0].docstring_start == 2
        assert fns[0].docstring_end == 5

    def test_multiple_functions(self) -> None:
        from exo.stdlib.coherence import _find_python_functions

        code = textwrap.dedent('''\
            def foo():
                """Foo."""
                pass

            def bar():
                """Bar."""
                pass
        ''')
        fns = _find_python_functions(code)
        assert len(fns) == 2
        assert fns[0].name == "foo"
        assert fns[1].name == "bar"

    def test_class_method(self) -> None:
        from exo.stdlib.coherence import _find_python_functions

        code = textwrap.dedent('''\
            class MyClass:
                def method(self):
                    """Method doc."""
                    return 42
        ''')
        fns = _find_python_functions(code)
        assert len(fns) == 1
        assert fns[0].name == "method"
        assert fns[0].docstring_start > 0


class TestDocstringFreshness:
    def test_body_changed_docstring_changed_no_violation(self, tmp_path: Path) -> None:
        """Both body and docstring changed → no violation."""
        from exo.stdlib.coherence import check_docstring_freshness

        repo = tmp_path
        (repo / "src").mkdir()
        (repo / "src" / "foo.py").write_text(
            textwrap.dedent('''\
            def hello():
                """Say hello."""
                print("hello world")
        ''')
        )

        # Mock changed ranges: both docstring (line 2) and body (line 3) changed
        with patch("exo.stdlib.coherence._changed_line_ranges", return_value=[(2, 3)]):
            violations, fn_count = check_docstring_freshness(repo, ["src/foo.py"], "main", ["py"])
        assert violations == []
        assert fn_count == 1

    def test_body_changed_docstring_not_changed_violation(self, tmp_path: Path) -> None:
        """Body changed but docstring didn't → violation."""
        from exo.stdlib.coherence import check_docstring_freshness

        repo = tmp_path
        (repo / "src").mkdir()
        (repo / "src" / "foo.py").write_text(
            textwrap.dedent('''\
            def hello():
                """Say hello."""
                print("hello world")
        ''')
        )

        # Only body line (3) changed, not docstring (2)
        with patch("exo.stdlib.coherence._changed_line_ranges", return_value=[(3, 3)]):
            violations, fn_count = check_docstring_freshness(repo, ["src/foo.py"], "main", ["py"])
        assert len(violations) == 1
        assert violations[0].kind == "stale_docstring"
        assert violations[0].file == "src/foo.py"
        assert "hello" in violations[0].message

    def test_no_docstring_no_violation(self, tmp_path: Path) -> None:
        """Function with no docstring + body changed → no violation."""
        from exo.stdlib.coherence import check_docstring_freshness

        repo = tmp_path
        (repo / "foo.py").write_text(
            textwrap.dedent("""\
            def hello():
                print("hello")
        """)
        )

        with patch("exo.stdlib.coherence._changed_line_ranges", return_value=[(2, 2)]):
            violations, _ = check_docstring_freshness(repo, ["foo.py"], "main", ["py"])
        assert violations == []

    def test_only_docstring_changed_no_violation(self, tmp_path: Path) -> None:
        """Only docstring changed, not body → no violation."""
        from exo.stdlib.coherence import check_docstring_freshness

        repo = tmp_path
        (repo / "foo.py").write_text(
            textwrap.dedent('''\
            def hello():
                """Say hello."""
                print("hello")
        ''')
        )

        # Only docstring line (2) changed
        with patch("exo.stdlib.coherence._changed_line_ranges", return_value=[(2, 2)]):
            violations, _ = check_docstring_freshness(repo, ["foo.py"], "main", ["py"])
        assert violations == []

    def test_multiple_functions_one_stale(self, tmp_path: Path) -> None:
        """Two functions, only one stale → one violation."""
        from exo.stdlib.coherence import check_docstring_freshness

        repo = tmp_path
        (repo / "foo.py").write_text(
            textwrap.dedent('''\
            def foo():
                """Foo doc."""
                pass

            def bar():
                """Bar doc."""
                pass
        ''')
        )

        # Only bar's body changed (line 7), not its docstring (line 6)
        with patch("exo.stdlib.coherence._changed_line_ranges", return_value=[(7, 7)]):
            violations, fn_count = check_docstring_freshness(repo, ["foo.py"], "main", ["py"])
        assert len(violations) == 1
        assert violations[0].detail["function"] == "bar"

    def test_non_python_skipped(self, tmp_path: Path) -> None:
        """Non-Python files skipped."""
        from exo.stdlib.coherence import check_docstring_freshness

        repo = tmp_path
        (repo / "readme.md").write_text("# Readme\n")

        violations, fn_count = check_docstring_freshness(repo, ["readme.md"], "main", ["py"])
        assert violations == []
        assert fn_count == 0

    def test_file_not_in_changed_list_skipped(self, tmp_path: Path) -> None:
        """Files not in the changed list are not checked."""
        from exo.stdlib.coherence import check_docstring_freshness

        repo = tmp_path
        (repo / "foo.py").write_text("def foo():\n    pass\n")

        violations, fn_count = check_docstring_freshness(repo, [], "main", ["py"])
        assert violations == []
        assert fn_count == 0

    def test_skip_patterns_respected(self, tmp_path: Path) -> None:
        """Files matching skip_patterns are excluded."""
        from exo.stdlib.coherence import check_docstring_freshness

        repo = tmp_path
        vendor = repo / "vendor"
        vendor.mkdir()
        (vendor / "lib.py").write_text(
            textwrap.dedent('''\
            def vendored():
                """Vendored function."""
                pass
        ''')
        )

        with patch("exo.stdlib.coherence._changed_line_ranges", return_value=[(3, 3)]):
            violations, fn_count = check_docstring_freshness(
                repo, ["vendor/lib.py"], "main", ["py"], skip_patterns=["vendor/**"]
            )
        assert violations == []

    def test_class_method_docstring(self, tmp_path: Path) -> None:
        """Class method docstring freshness detection."""
        from exo.stdlib.coherence import check_docstring_freshness

        repo = tmp_path
        (repo / "cls.py").write_text(
            textwrap.dedent('''\
            class MyClass:
                def method(self):
                    """Method doc."""
                    return 42
        ''')
        )

        # Body line (4) changed, docstring (3) didn't
        with patch("exo.stdlib.coherence._changed_line_ranges", return_value=[(4, 4)]):
            violations, fn_count = check_docstring_freshness(repo, ["cls.py"], "main", ["py"])
        assert len(violations) == 1
        assert violations[0].detail["function"] == "method"


# ── check_coherence orchestrator ─────────────────────────────────


class TestCheckCoherence:
    def test_no_changes_empty_report(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _init_git(repo)
        from exo.stdlib.coherence import check_coherence

        report = check_coherence(repo, base="HEAD")
        assert report.passed is True
        assert report.violations == []

    def test_co_update_violation_in_report(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        config = load_yaml(repo / ".exo" / "config.yaml")
        config["coherence"]["co_update_rules"] = [{"files": ["a.py", "b.py"], "label": "pair"}]
        dump_yaml(repo / ".exo" / "config.yaml", config)
        _init_git(repo)

        # Create and modify only a.py
        (repo / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "a.py"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "add a"], cwd=str(repo), capture_output=True)

        from exo.stdlib.coherence import check_coherence

        # base=HEAD~1 so the diff shows a.py changed
        report = check_coherence(repo, base="HEAD~1")
        co_update_violations = [v for v in report.violations if v.kind == "co_update"]
        assert len(co_update_violations) == 1
        assert co_update_violations[0].file == "b.py"

    def test_stale_docstring_in_report(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _init_git(repo)

        # Create file with docstring
        (repo / "foo.py").write_text(
            textwrap.dedent('''\
            def greet():
                """Greet user."""
                print("hi")
        ''')
        )
        subprocess.run(["git", "add", "foo.py"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "add foo"], cwd=str(repo), capture_output=True)

        # Change body but not docstring
        (repo / "foo.py").write_text(
            textwrap.dedent('''\
            def greet():
                """Greet user."""
                print("hello world")
        ''')
        )
        subprocess.run(["git", "add", "foo.py"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "update foo body"], cwd=str(repo), capture_output=True)

        from exo.stdlib.coherence import check_coherence

        report = check_coherence(repo, base="HEAD~1")
        doc_violations = [v for v in report.violations if v.kind == "stale_docstring"]
        assert len(doc_violations) == 1
        assert doc_violations[0].detail["function"] == "greet"

    def test_skip_co_updates_flag(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        config = load_yaml(repo / ".exo" / "config.yaml")
        config["coherence"]["co_update_rules"] = [{"files": ["a.py", "b.py"], "label": "pair"}]
        dump_yaml(repo / ".exo" / "config.yaml", config)
        _init_git(repo)

        (repo / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "a.py"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "add a"], cwd=str(repo), capture_output=True)

        from exo.stdlib.coherence import check_coherence

        report = check_coherence(repo, base="HEAD~1", skip_co_updates=True)
        co_update_violations = [v for v in report.violations if v.kind == "co_update"]
        assert co_update_violations == []

    def test_skip_docstrings_flag(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _init_git(repo)

        (repo / "foo.py").write_text(
            textwrap.dedent('''\
            def greet():
                """Greet user."""
                print("hi")
        ''')
        )
        subprocess.run(["git", "add", "foo.py"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "add foo"], cwd=str(repo), capture_output=True)

        (repo / "foo.py").write_text(
            textwrap.dedent('''\
            def greet():
                """Greet user."""
                print("hello world")
        ''')
        )
        subprocess.run(["git", "add", "foo.py"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "update body"], cwd=str(repo), capture_output=True)

        from exo.stdlib.coherence import check_coherence

        report = check_coherence(repo, base="HEAD~1", skip_docstrings=True)
        doc_violations = [v for v in report.violations if v.kind == "stale_docstring"]
        assert doc_violations == []


# ── Serialization ────────────────────────────────────────────────


class TestCoherenceSerialization:
    def test_coherence_to_dict(self) -> None:
        from exo.stdlib.coherence import CoherenceReport, CoherenceViolation, coherence_to_dict

        v = CoherenceViolation(kind="co_update", severity="warning", file="a.py", message="stale")
        r = CoherenceReport(violations=[v], files_checked=5, functions_checked=10, checked_at="2024-01-01T00:00:00Z")
        d = coherence_to_dict(r)
        assert d["passed"] is True
        assert d["warning_count"] == 1
        assert d["files_checked"] == 5
        assert d["functions_checked"] == 10
        assert len(d["violations"]) == 1
        assert d["violations"][0]["kind"] == "co_update"

    def test_format_human_with_violations(self) -> None:
        from exo.stdlib.coherence import CoherenceReport, CoherenceViolation, format_coherence_human

        v = CoherenceViolation(kind="stale_docstring", severity="warning", file="foo.py", message="stale")
        r = CoherenceReport(violations=[v], files_checked=1, functions_checked=3, checked_at="now")
        output = format_coherence_human(r)
        assert "PASS" in output
        assert "stale docstrings: 1" in output
        assert "[WARN]" in output

    def test_format_human_no_issues(self) -> None:
        from exo.stdlib.coherence import CoherenceReport, format_coherence_human

        r = CoherenceReport(violations=[], files_checked=2, functions_checked=5, checked_at="now")
        output = format_coherence_human(r)
        assert "PASS" in output
        assert "No issues found" in output


# ── Drift Composite Integration ──────────────────────────────────


class TestDriftCoherenceSection:
    def test_drift_includes_coherence_section(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _init_git(repo)
        from exo.stdlib.drift import drift

        report = drift(repo, skip_adapters=True, skip_features=True, skip_requirements=True, skip_sessions=True)
        names = [s.name for s in report.sections]
        assert "coherence" in names

    def test_skip_coherence_flag(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _init_git(repo)
        from exo.stdlib.drift import drift

        report = drift(
            repo,
            skip_adapters=True,
            skip_features=True,
            skip_requirements=True,
            skip_sessions=True,
            skip_coherence=True,
        )
        names = [s.name for s in report.sections]
        assert "coherence" not in names

    def test_coherence_error_produces_error_section(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _init_git(repo)
        from exo.stdlib.drift import _check_coherence

        with patch("exo.stdlib.coherence.check_coherence", side_effect=Exception("boom")):
            section = _check_coherence(repo)
        assert section.status == "error"
        assert section.errors == 1


# ── Config Schema ────────────────────────────────────────────────


class TestCoherenceConfigSchema:
    def test_default_config_has_coherence(self) -> None:
        assert "coherence" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["coherence"]["enabled"] is True
        assert DEFAULT_CONFIG["coherence"]["co_update_rules"] == []
        assert DEFAULT_CONFIG["coherence"]["docstring_languages"] == ["py"]
        assert DEFAULT_CONFIG["coherence"]["skip_patterns"] == []

    def test_config_validation_passes(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        from exo.stdlib.config_schema import validate_config

        result = validate_config(repo)
        coherence_issues = [i for i in result.issues if "coherence" in i.path]
        assert not coherence_issues

    def test_missing_coherence_warns(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        config = load_yaml(repo / ".exo" / "config.yaml")
        del config["coherence"]
        dump_yaml(repo / ".exo" / "config.yaml", config)
        from exo.stdlib.config_schema import validate_config

        result = validate_config(repo)
        coherence_issues = [i for i in result.issues if i.path == "coherence"]
        assert len(coherence_issues) == 1
        assert coherence_issues[0].severity == "warning"

    def test_wrong_type_coherence_key_errors(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        config = load_yaml(repo / ".exo" / "config.yaml")
        config["coherence"]["enabled"] = "yes"  # should be bool
        dump_yaml(repo / ".exo" / "config.yaml", config)
        from exo.stdlib.config_schema import validate_config

        result = validate_config(repo)
        coherence_issues = [i for i in result.issues if "enabled" in i.path and "coherence" in i.path]
        assert len(coherence_issues) == 1
        assert coherence_issues[0].severity == "error"


# ── Upgrade Backfill ─────────────────────────────────────────────


class TestUpgradeBackfillsCoherence:
    def test_upgrade_adds_coherence_section(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine

        engine = KernelEngine(repo=tmp_path, actor="test-agent")
        engine.init(scan=False)
        config = load_yaml(tmp_path / ".exo" / "config.yaml")
        del config["coherence"]
        dump_yaml(tmp_path / ".exo" / "config.yaml", config)

        from exo.stdlib.upgrade import upgrade

        upgrade(tmp_path)
        config_after = load_yaml(tmp_path / ".exo" / "config.yaml")
        assert "coherence" in config_after
        assert config_after["coherence"]["enabled"] is True
