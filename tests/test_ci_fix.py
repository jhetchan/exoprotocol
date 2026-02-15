"""Tests for exo.stdlib.ci_fix — CI failure detection and auto-fix."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from exo.kernel.errors import ExoError
from exo.stdlib.ci_fix import (
    apply_fixes,
    commit_and_push,
    fetch_ci_failure,
    format_ci_fix_human,
    parse_errors,
    suggest_fixes,
)

# ── parse_errors() ─────────────────────────────────────────────


class TestParseErrors:
    def test_ruff_format(self) -> None:
        logs = "12 files would be reformatted\nWould reformat: exo/cli.py\nWould reformat: tests/test_foo.py"
        errors = parse_errors(logs)
        assert len(errors) == 1
        assert errors[0]["tool"] == "ruff-format"
        assert errors[0]["auto_fixable"] is True
        assert "12" in errors[0]["message"]
        assert errors[0]["files"] == ["exo/cli.py", "tests/test_foo.py"]

    def test_ruff_format_single_file(self) -> None:
        logs = "1 file would be reformatted\nWould reformat: exo/cli.py"
        errors = parse_errors(logs)
        assert len(errors) == 1
        assert errors[0]["tool"] == "ruff-format"
        assert "1" in errors[0]["message"]

    def test_ruff_lint(self) -> None:
        logs = (
            "exo/session.py:42:5: SIM105 Use contextlib.suppress\ntests/test_foo.py:10:1: B017 assertRaises(Exception)"
        )
        errors = parse_errors(logs)
        assert len(errors) == 2
        assert errors[0]["tool"] == "ruff-lint"
        assert errors[0]["file"] == "exo/session.py"
        assert errors[0]["line"] == 42
        assert errors[0]["code"] == "SIM105"
        assert errors[0]["auto_fixable"] is False
        assert errors[1]["code"] == "B017"

    def test_pytest_failures(self) -> None:
        logs = "FAILED tests/test_foo.py::TestBar::test_baz\nFAILED tests/test_qux.py::test_quux"
        errors = parse_errors(logs)
        assert len(errors) == 2
        assert errors[0]["tool"] == "pytest"
        assert errors[0]["test"] == "tests/test_foo.py::TestBar::test_baz"
        assert errors[1]["test"] == "tests/test_qux.py::test_quux"

    def test_compile_errors(self) -> None:
        logs = "SyntaxError: unexpected EOF while parsing\nIndentationError: unexpected indent"
        errors = parse_errors(logs)
        assert len(errors) == 2
        assert errors[0]["tool"] == "python-compile"
        assert "SyntaxError" in errors[0]["message"]
        assert errors[1]["error_type"] == "IndentationError"

    def test_unknown_fallback(self) -> None:
        logs = "some random error output with no recognizable pattern"
        errors = parse_errors(logs)
        assert len(errors) == 1
        assert errors[0]["tool"] == "unknown"
        assert errors[0]["auto_fixable"] is False

    def test_mixed_errors(self) -> None:
        logs = (
            "3 files would be reformatted\n"
            "Would reformat: exo/cli.py\n"
            "exo/foo.py:10:1: B017 bad practice\n"
            "FAILED tests/test_bar.py::test_x\n"
        )
        errors = parse_errors(logs)
        tools = [e["tool"] for e in errors]
        assert "ruff-format" in tools
        assert "ruff-lint" in tools
        assert "pytest" in tools

    def test_empty_logs(self) -> None:
        errors = parse_errors("")
        assert len(errors) == 1
        assert errors[0]["tool"] == "unknown"


# ── suggest_fixes() ────────────────────────────────────────────


class TestSuggestFixes:
    def test_ruff_format_fix(self) -> None:
        errors = [
            {
                "tool": "ruff-format",
                "message": "3 files would be reformatted",
                "files": ["exo/cli.py", "tests/test_foo.py"],
                "auto_fixable": True,
            }
        ]
        fixes = suggest_fixes(errors)
        assert len(fixes) == 1
        assert fixes[0]["auto_fixable"] is True
        assert "ruff format" in fixes[0]["command"]
        assert "exo" in fixes[0]["command"]
        assert "tests" in fixes[0]["command"]

    def test_ruff_format_deduplication(self) -> None:
        errors = [
            {
                "tool": "ruff-format",
                "message": "3 files would be reformatted",
                "files": ["exo/a.py"],
                "auto_fixable": True,
            },
            {
                "tool": "ruff-format",
                "message": "2 files would be reformatted",
                "files": ["exo/b.py"],
                "auto_fixable": True,
            },
        ]
        fixes = suggest_fixes(errors)
        format_fixes = [f for f in fixes if f["tool"] == "ruff-format"]
        assert len(format_fixes) == 1

    def test_ruff_lint_fix(self) -> None:
        errors = [
            {
                "tool": "ruff-lint",
                "file": "exo/foo.py",
                "line": 42,
                "code": "SIM105",
                "message": "Use contextlib.suppress",
                "auto_fixable": False,
            }
        ]
        fixes = suggest_fixes(errors)
        assert len(fixes) == 1
        assert fixes[0]["auto_fixable"] is False
        assert "SIM105" in fixes[0]["description"]

    def test_pytest_fix(self) -> None:
        errors = [
            {
                "tool": "pytest",
                "test": "tests/test_foo.py::test_bar",
                "message": "Test failed",
                "auto_fixable": False,
            }
        ]
        fixes = suggest_fixes(errors)
        assert len(fixes) == 1
        assert fixes[0]["tool"] == "pytest"
        assert "test_bar" in fixes[0]["description"]

    def test_no_files_falls_back_to_dot(self) -> None:
        errors = [
            {
                "tool": "ruff-format",
                "message": "1 file would be reformatted",
                "files": [],
                "auto_fixable": True,
            }
        ]
        fixes = suggest_fixes(errors)
        assert fixes[0]["command"] == "ruff format ."


# ── fetch_ci_failure() ─────────────────────────────────────────


class TestFetchCiFailure:
    @patch("exo.stdlib.ci_fix._run_gh")
    def test_no_failures(self, mock_gh: MagicMock, tmp_path: Path) -> None:
        mock_gh.return_value = (0, "[]", "")
        result = fetch_ci_failure(tmp_path)
        assert result["status"] == "no_failures"

    @patch("exo.stdlib.ci_fix._run_gh")
    def test_fetch_latest(self, mock_gh: MagicMock, tmp_path: Path) -> None:
        run_list = json.dumps(
            [
                {
                    "databaseId": 123,
                    "conclusion": "failure",
                    "name": "CI",
                    "headBranch": "main",
                    "createdAt": "2026-02-16",
                    "url": "https://example.com",
                }
            ]
        )
        logs = "2 files would be reformatted\nWould reformat: exo/cli.py"

        def side_effect(args: list[str], cwd: Path) -> tuple[int, str, str]:
            if "list" in args:
                return (0, run_list, "")
            if "--log-failed" in args:
                return (0, logs, "")
            return (0, "{}", "")

        mock_gh.side_effect = side_effect
        result = fetch_ci_failure(tmp_path)
        assert result["status"] == "failure"
        assert result["run_id"] == "123"
        assert len(result["errors"]) == 1
        assert result["errors"][0]["tool"] == "ruff-format"
        assert len(result["fix_commands"]) == 1

    @patch("exo.stdlib.ci_fix._run_gh")
    def test_fetch_by_run_id(self, mock_gh: MagicMock, tmp_path: Path) -> None:
        run_info = json.dumps({"databaseId": 456, "name": "CI"})
        logs = "FAILED tests/test_x.py::test_y"

        def side_effect(args: list[str], cwd: Path) -> tuple[int, str, str]:
            if "view" in args and "--log-failed" in args:
                return (0, logs, "")
            if "view" in args:
                return (0, run_info, "")
            return (0, "[]", "")

        mock_gh.side_effect = side_effect
        result = fetch_ci_failure(tmp_path, run_id="456")
        assert result["run_id"] == "456"
        assert result["errors"][0]["tool"] == "pytest"

    @patch("exo.stdlib.ci_fix._run_gh")
    def test_gh_list_failure(self, mock_gh: MagicMock, tmp_path: Path) -> None:
        mock_gh.return_value = (1, "", "not authenticated")
        with pytest.raises(ExoError, match="GH_RUN_LIST_FAILED"):
            fetch_ci_failure(tmp_path)

    @patch("exo.stdlib.ci_fix._run_gh")
    def test_gh_log_failure(self, mock_gh: MagicMock, tmp_path: Path) -> None:
        run_list = json.dumps(
            [
                {
                    "databaseId": 1,
                    "conclusion": "failure",
                    "name": "CI",
                    "headBranch": "main",
                    "createdAt": "now",
                    "url": "",
                }
            ]
        )

        def side_effect(args: list[str], cwd: Path) -> tuple[int, str, str]:
            if "list" in args:
                return (0, run_list, "")
            return (1, "", "log fetch failed")

        mock_gh.side_effect = side_effect
        with pytest.raises(ExoError, match="GH_LOG_FAILED"):
            fetch_ci_failure(tmp_path)

    @patch("exo.stdlib.ci_fix._run_gh")
    def test_truncates_long_logs(self, mock_gh: MagicMock, tmp_path: Path) -> None:
        run_list = json.dumps(
            [
                {
                    "databaseId": 1,
                    "conclusion": "failure",
                    "name": "CI",
                    "headBranch": "main",
                    "createdAt": "now",
                    "url": "",
                }
            ]
        )
        long_logs = "x" * 20_000

        def side_effect(args: list[str], cwd: Path) -> tuple[int, str, str]:
            if "list" in args:
                return (0, run_list, "")
            if "--log-failed" in args:
                return (0, long_logs, "")
            return (0, "{}", "")

        mock_gh.side_effect = side_effect
        result = fetch_ci_failure(tmp_path)
        assert result["logs_truncated"] is True
        assert len(result["logs"]) < 20_000


# ── apply_fixes() ──────────────────────────────────────────────


class TestApplyFixes:
    def test_no_failures_passthrough(self) -> None:
        report: dict[str, Any] = {"status": "no_failures", "message": "all good"}
        result = apply_fixes(report=report)
        assert result["status"] == "no_failures"

    @patch("exo.stdlib.ci_fix.subprocess.run")
    def test_applies_auto_fixable(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="2 files reformatted", stderr="")
        report: dict[str, Any] = {
            "status": "failure",
            "run_id": "123",
            "fixes": [
                {"tool": "ruff-format", "command": "ruff format .", "description": "Reformat", "auto_fixable": True},
                {"tool": "pytest", "description": "Fix test", "auto_fixable": False},
            ],
        }
        result = apply_fixes(tmp_path, report=report)
        assert result["status"] == "fixed"
        assert len(result["applied"]) == 1
        assert result["applied"][0]["success"] is True
        assert len(result["remaining"]) == 1

    @patch("exo.stdlib.ci_fix.subprocess.run")
    def test_partial_on_failure(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="command not found")
        report: dict[str, Any] = {
            "status": "failure",
            "run_id": "123",
            "fixes": [
                {"tool": "ruff-format", "command": "ruff format .", "description": "Reformat", "auto_fixable": True},
            ],
        }
        result = apply_fixes(tmp_path, report=report)
        assert result["status"] == "partial"
        assert result["applied"][0]["success"] is False

    def test_skips_fixes_without_command(self) -> None:
        report: dict[str, Any] = {
            "status": "failure",
            "run_id": "123",
            "fixes": [
                {"tool": "ruff-lint", "description": "Fix lint", "auto_fixable": False},
            ],
        }
        result = apply_fixes(report=report)
        assert result["status"] == "partial"
        assert len(result["applied"]) == 0
        assert len(result["remaining"]) == 1


# ── commit_and_push() ──────────────────────────────────────────


class TestCommitAndPush:
    @patch("exo.stdlib.ci_fix.subprocess.run")
    def test_nothing_to_commit(self, mock_run: MagicMock, tmp_path: Path) -> None:
        # git add succeeds, git diff --cached --quiet returns 0 (nothing staged)
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # git add
            MagicMock(returncode=0, stdout="", stderr=""),  # git diff --cached --quiet
        ]
        result = commit_and_push(tmp_path, run_id="123")
        assert result["pushed"] is False
        assert "Nothing to commit" in result["error"]

    @patch("exo.stdlib.ci_fix.subprocess.run")
    def test_full_cycle(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # git add
            MagicMock(returncode=1, stdout="", stderr=""),  # git diff --cached --quiet (has changes)
            MagicMock(returncode=0, stdout="", stderr=""),  # git commit
            MagicMock(returncode=0, stdout="abc1234\n", stderr=""),  # git rev-parse HEAD
            MagicMock(returncode=0, stdout="", stderr=""),  # git push
        ]
        result = commit_and_push(tmp_path, run_id="123")
        assert result["pushed"] is True
        assert result["committed"] is True

    @patch("exo.stdlib.ci_fix.subprocess.run")
    def test_push_fails(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # git add
            MagicMock(returncode=1, stdout="", stderr=""),  # git diff --cached --quiet
            MagicMock(returncode=0, stdout="", stderr=""),  # git commit
            MagicMock(returncode=0, stdout="abc1234\n", stderr=""),  # git rev-parse HEAD
            MagicMock(returncode=1, stdout="", stderr="rejected"),  # git push
        ]
        result = commit_and_push(tmp_path, run_id="123")
        assert result["pushed"] is False
        assert result["committed"] is True


# ── format_ci_fix_human() ──────────────────────────────────────


class TestFormatHuman:
    def test_no_failures(self) -> None:
        result = format_ci_fix_human({"status": "no_failures"})
        assert "No failed CI runs" in result

    def test_failure_report(self) -> None:
        report: dict[str, Any] = {
            "status": "failure",
            "run_id": "123",
            "run_info": {"name": "CI", "headBranch": "main"},
            "errors": [
                {"tool": "ruff-format", "message": "3 files would be reformatted", "auto_fixable": True},
                {"tool": "ruff-lint", "file": "exo/foo.py", "line": 10, "message": "bad", "auto_fixable": False},
            ],
            "fixes": [
                {
                    "tool": "ruff-format",
                    "command": "ruff format .",
                    "description": "Reformat 3 files",
                    "auto_fixable": True,
                },
                {"tool": "ruff-lint", "description": "Fix SIM105 in exo/foo.py:10", "auto_fixable": False},
            ],
        }
        text = format_ci_fix_human(report)
        assert "CI Failure: run 123" in text
        assert "workflow: CI" in text
        assert "[auto-fixable]" in text
        assert "[manual]" in text
        assert "$ ruff format" in text
        assert "Manual fixes needed" in text

    def test_applied_report(self) -> None:
        report: dict[str, Any] = {
            "status": "fixed",
            "run_id": "123",
            "applied": [
                {
                    "command": "ruff format .",
                    "description": "Reformat",
                    "success": True,
                    "output": "2 files reformatted",
                },
            ],
            "remaining": [],
        }
        text = format_ci_fix_human(report)
        assert "[OK]" in text
        assert "ruff format" in text

    def test_push_result(self) -> None:
        report: dict[str, Any] = {
            "status": "fixed",
            "run_id": "123",
            "pushed": True,
            "committed": True,
            "commit_sha": "abc12345678",
            "applied": [],
            "remaining": [],
        }
        text = format_ci_fix_human(report)
        assert "Pushed: abc12345" in text
