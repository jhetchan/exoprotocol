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

import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

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


# ── Coherence wired into exo check ──────────────────────────────


def _create_ticket_and_lock(repo: Path, ticket_id: str = "TICKET-001") -> None:
    from exo.kernel import tickets

    tickets.save_ticket(
        repo,
        {
            "id": ticket_id,
            "title": "test",
            "intent": "test",
            "priority": 1,
            "type": "feature",
            "status": "todo",
            "labels": [],
            "checks": [],
        },
    )
    tickets.acquire_lock(repo, ticket_id, owner="test")


class TestCheckIncludesCoherence:
    """TKT-2WV0: exo check should run coherence checks automatically."""

    def test_check_returns_coherence_field(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine

        repo = _bootstrap_repo(tmp_path)
        _init_git(repo)
        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.check()
        data = result.get("data", {})
        assert "coherence" in data

    def test_check_coherence_passes_when_clean(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine

        repo = _bootstrap_repo(tmp_path)
        _init_git(repo)
        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.check()
        data = result.get("data", {})
        assert data["coherence"]["passed"] is True
        assert data["passed"] is True

    def test_check_coherence_warns_on_co_update_violation(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine

        repo = _bootstrap_repo(tmp_path)
        config = load_yaml(repo / ".exo" / "config.yaml")
        config["coherence"]["co_update_rules"] = [{"files": ["a.py", "b.py"], "label": "pair"}]
        dump_yaml(repo / ".exo" / "config.yaml", config)
        _init_git(repo)

        # Create feature branch and change only a.py (so diff main..HEAD shows it)
        subprocess.run(["git", "checkout", "-b", "feature-test"], cwd=str(repo), capture_output=True)
        (repo / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "a.py"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "add a"], cwd=str(repo), capture_output=True)

        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.check()
        data = result.get("data", {})
        coh = data["coherence"]
        assert coh["warning_count"] >= 1
        co_update_v = [v for v in coh["violations"] if v["kind"] == "co_update"]
        assert len(co_update_v) == 1

    def test_check_coherence_disabled_skips(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine

        repo = _bootstrap_repo(tmp_path)
        config = load_yaml(repo / ".exo" / "config.yaml")
        config["coherence"]["enabled"] = False
        dump_yaml(repo / ".exo" / "config.yaml", config)
        _init_git(repo)
        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.check()
        data = result.get("data", {})
        # Coherence disabled → None
        assert data["coherence"] is None

    def test_push_includes_coherence(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine

        repo = _bootstrap_repo(tmp_path)
        _init_git(repo)
        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.push()
        data = result.get("data", {})
        checks = data.get("checks", {})
        assert "coherence" in checks


# ── Plan-time co-update advisory ────────────────────────────────


class TestPlanCoUpdateAdvisory:
    """TKT-ZQJH: exo plan should warn about co-update impact."""

    def test_plan_returns_co_update_advisories(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine

        repo = _bootstrap_repo(tmp_path)
        config = load_yaml(repo / ".exo" / "config.yaml")
        config["coherence"]["co_update_rules"] = [
            {"files": ["exo/cli.py", "docs/cli-reference.md"], "label": "CLI-doc pair"}
        ]
        dump_yaml(repo / ".exo" / "config.yaml", config)
        _init_git(repo)

        engine = KernelEngine(str(repo))
        # Plan with a ticket whose scope includes cli.py but not docs/cli-reference.md
        result = engine.plan("Add a new CLI command")
        data = result.get("data", {})
        # Script may or may not create tickets; the co_update_advisories field
        # is present only when violations are found against created ticket scopes.
        # Since the script is a no-op, no tickets → no advisories
        assert "co_update_advisories" not in data or data["co_update_advisories"] == []

    def test_plan_co_update_advisory_with_scoped_ticket(self, tmp_path: Path) -> None:
        """When created tickets have scope overlapping co-update rules, advisory appears."""
        from exo.kernel import tickets as tmod
        from exo.stdlib.engine import KernelEngine

        repo = _bootstrap_repo(tmp_path)
        config = load_yaml(repo / ".exo" / "config.yaml")
        config["coherence"]["co_update_rules"] = [
            {"files": ["exo/cli.py", "docs/cli-reference.md"], "label": "CLI-doc pair"}
        ]
        dump_yaml(repo / ".exo" / "config.yaml", config)
        _init_git(repo)

        # Pre-create a ticket whose scope includes cli.py but not docs/cli-reference.md
        tmod.save_ticket(
            repo,
            {
                "id": "TKT-TEST-001",
                "title": "Test ticket",
                "intent": "test",
                "priority": 1,
                "type": "feature",
                "status": "todo",
                "labels": [],
                "checks": [],
                "scope": {"allow": ["exo/cli.py"], "deny": []},
            },
        )

        # Mock the plan script to return this ticket
        engine = KernelEngine(str(repo))
        from unittest.mock import patch as _patch

        with _patch.object(
            engine,
            "_run_script",
            return_value={
                "spec_markdown": "# Test Spec\n",
                "tickets": [
                    {
                        "id": "TKT-TEST-002",
                        "title": "Add CLI command",
                        "intent": "test",
                        "priority": 1,
                        "type": "feature",
                        "status": "todo",
                        "labels": [],
                        "checks": [],
                        "scope": {"allow": ["exo/cli.py"], "deny": []},
                    }
                ],
            },
        ):
            result = engine.plan("Add a new CLI command")
        data = result.get("data", {})
        advisories = data.get("co_update_advisories", [])
        assert len(advisories) >= 1
        assert advisories[0]["kind"] == "co_update_impact"
        assert "docs/cli-reference.md" in advisories[0]["missing_file"]

    def test_plan_no_advisory_when_scope_covers_both(self, tmp_path: Path) -> None:
        """No advisory when ticket scope includes all co-update files."""
        from exo.stdlib.engine import KernelEngine

        repo = _bootstrap_repo(tmp_path)
        config = load_yaml(repo / ".exo" / "config.yaml")
        config["coherence"]["co_update_rules"] = [
            {"files": ["exo/cli.py", "docs/cli-reference.md"], "label": "CLI-doc pair"}
        ]
        dump_yaml(repo / ".exo" / "config.yaml", config)
        _init_git(repo)

        engine = KernelEngine(str(repo))
        from unittest.mock import patch as _patch

        with _patch.object(
            engine,
            "_run_script",
            return_value={
                "spec_markdown": "# Test Spec\n",
                "tickets": [
                    {
                        "id": "TKT-TEST-003",
                        "title": "Add CLI command with docs",
                        "intent": "test",
                        "priority": 1,
                        "type": "feature",
                        "status": "todo",
                        "labels": [],
                        "checks": [],
                        "scope": {"allow": ["exo/cli.py", "docs/cli-reference.md"], "deny": []},
                    }
                ],
            },
        ):
            result = engine.plan("Add a new CLI command with docs")
        data = result.get("data", {})
        assert "co_update_advisories" not in data or data["co_update_advisories"] == []


# ── Session-start co-update advisory ────────────────────────────


class TestSessionStartCoUpdateAdvisory:
    """TKT-913A: session-start should warn about co-update impact."""

    def _setup_session_repo(self, tmp_path: Path) -> Path:
        from exo.kernel import tickets as tmod

        repo = _bootstrap_repo(tmp_path)
        _init_git(repo)
        config = load_yaml(repo / ".exo" / "config.yaml")
        config["coherence"]["co_update_rules"] = [
            {"files": ["exo/cli.py", "docs/cli-reference.md"], "label": "CLI-doc pair"}
        ]
        dump_yaml(repo / ".exo" / "config.yaml", config)

        tmod.save_ticket(
            repo,
            {
                "id": "TKT-COTEST-001",
                "title": "Test co-update",
                "intent": "test",
                "priority": 1,
                "type": "feature",
                "status": "todo",
                "labels": [],
                "checks": [],
                "scope": {"allow": ["exo/cli.py", "tests/**"], "deny": []},
            },
        )
        return repo

    def test_session_start_co_update_advisory_in_bootstrap(self, tmp_path: Path) -> None:
        from exo.orchestrator.session import AgentSessionManager

        repo = self._setup_session_repo(tmp_path)
        mgr = AgentSessionManager(root=repo, actor="agent:test")
        result = mgr.start(
            ticket_id="TKT-COTEST-001",
            vendor="test",
            model="test-model",
            acquire_lock=True,
        )
        bootstrap = result.get("bootstrap_prompt", "")
        assert "Co-update" in bootstrap

    def test_session_start_co_update_advisory_mentions_missing_file(self, tmp_path: Path) -> None:
        from exo.orchestrator.session import AgentSessionManager

        repo = self._setup_session_repo(tmp_path)
        mgr = AgentSessionManager(root=repo, actor="agent:test")
        result = mgr.start(
            ticket_id="TKT-COTEST-001",
            vendor="test",
            model="test-model",
            acquire_lock=True,
        )
        bootstrap = result.get("bootstrap_prompt", "")
        assert "docs/cli-reference.md" in bootstrap

    def test_session_start_no_advisory_when_scope_covers_all(self, tmp_path: Path) -> None:
        from exo.kernel import tickets as tmod
        from exo.orchestrator.session import AgentSessionManager

        repo = _bootstrap_repo(tmp_path)
        _init_git(repo)
        config = load_yaml(repo / ".exo" / "config.yaml")
        config["coherence"]["co_update_rules"] = [
            {"files": ["exo/cli.py", "docs/cli-reference.md"], "label": "CLI-doc pair"}
        ]
        dump_yaml(repo / ".exo" / "config.yaml", config)

        tmod.save_ticket(
            repo,
            {
                "id": "TKT-COTEST-002",
                "title": "Test co-update full coverage",
                "intent": "test",
                "priority": 1,
                "type": "feature",
                "status": "todo",
                "labels": [],
                "checks": [],
                "scope": {"allow": ["exo/cli.py", "docs/cli-reference.md"], "deny": []},
            },
        )

        mgr = AgentSessionManager(root=repo, actor="agent:test")
        result = mgr.start(
            ticket_id="TKT-COTEST-002",
            vendor="test",
            model="test-model",
            acquire_lock=True,
        )
        advisories = result.get("start_advisories") or []
        co_update_adv = [a for a in advisories if a.get("kind") == "co_update_impact"]
        assert co_update_adv == []

    def test_session_start_advisory_in_advisories_dict(self, tmp_path: Path) -> None:
        from exo.orchestrator.session import AgentSessionManager

        repo = self._setup_session_repo(tmp_path)
        mgr = AgentSessionManager(root=repo, actor="agent:test")
        result = mgr.start(
            ticket_id="TKT-COTEST-001",
            vendor="test",
            model="test-model",
            acquire_lock=True,
        )
        advisories = result.get("start_advisories") or []
        co_update_adv = [a for a in advisories if a.get("kind") == "co_update_impact"]
        assert len(co_update_adv) >= 1
        assert "docs/cli-reference.md" in co_update_adv[0]["message"]

    def test_session_start_no_advisory_without_co_update_rules(self, tmp_path: Path) -> None:
        from exo.kernel import tickets as tmod
        from exo.orchestrator.session import AgentSessionManager

        repo = _bootstrap_repo(tmp_path)
        _init_git(repo)

        tmod.save_ticket(
            repo,
            {
                "id": "TKT-COTEST-003",
                "title": "No rules",
                "intent": "test",
                "priority": 1,
                "type": "feature",
                "status": "todo",
                "labels": [],
                "checks": [],
                "scope": {"allow": ["exo/cli.py"], "deny": []},
            },
        )

        mgr = AgentSessionManager(root=repo, actor="agent:test")
        result = mgr.start(
            ticket_id="TKT-COTEST-003",
            vendor="test",
            model="test-model",
            acquire_lock=True,
        )
        advisories = result.get("start_advisories") or []
        co_update_adv = [a for a in advisories if a.get("kind") == "co_update_impact"]
        assert co_update_adv == []
