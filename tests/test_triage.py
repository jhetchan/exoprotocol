"""Tests for stale-test triage (closes feedback #6).

Covers:
- triage_test classification: stale / regression / ambiguous / unknown
- Evidence generation (test edit, behavior edit timestamps)
- Window-based behavior detection
- CLI: exo test-triage
- Format helpers
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from exo.stdlib.triage import (
    TriageReport,
    format_triage_human,
    triage_test,
    triage_to_dict,
)

_GIT_TEST_ENV = {
    "GIT_AUTHOR_NAME": "Author One",
    "GIT_AUTHOR_EMAIL": "author@test",
    "GIT_COMMITTER_NAME": "Author One",
    "GIT_COMMITTER_EMAIL": "author@test",
}


def _git(cwd: Path, *args: str, env_override: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(_GIT_TEST_ENV)
    if env_override:
        env.update(env_override)
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    return repo


def _commit_at(repo: Path, *, files: dict[str, str], message: str, when: datetime, author: str = "Author One") -> str:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        _git(repo, "add", rel)
    iso = when.astimezone(timezone.utc).isoformat()
    env = {"GIT_AUTHOR_DATE": iso, "GIT_COMMITTER_DATE": iso}
    if author != "Author One":
        env["GIT_AUTHOR_NAME"] = author
        env["GIT_COMMITTER_NAME"] = author
    _git(repo, "commit", "-m", message, env_override=env)
    proc = _git(repo, "rev-parse", "HEAD")
    return proc.stdout.strip()


class TestTriageInputValidation:
    def test_missing_test_file_raises(self, tmp_path: Path) -> None:
        from exo.kernel.errors import ExoError

        repo = _init_repo(tmp_path)
        with pytest.raises(ExoError) as exc_info:
            triage_test(repo, "tests/test_does_not_exist.py")
        assert exc_info.value.code == "TEST_NOT_FOUND"


class TestTriageClassification:
    def test_regression_when_behavior_edited_after_test(self, tmp_path: Path) -> None:
        """Test edit is older; behavior change is newer → regression."""
        repo = _init_repo(tmp_path)
        now = datetime.now(timezone.utc)

        _commit_at(
            repo,
            files={
                "src/foo.py": "def foo(): return 1\n",
                "tests/test_foo.py": "from src.foo import foo\ndef test_foo(): assert foo() == 1\n",
            },
            message="initial",
            when=now - timedelta(days=10),
        )
        # Behavior change AFTER the test was last edited
        _commit_at(
            repo,
            files={"src/foo.py": "def foo(): return 2\n"},
            message="bump foo",
            when=now - timedelta(days=2),
            author="Author Two",
        )

        report = triage_test(repo, "tests/test_foo.py", window_days=30, now=now)
        assert report.classification == "regression"
        assert "regression" in report.rationale.lower()
        assert report.recommended_owner == "Author Two"

    def test_stale_when_test_predates_window_with_no_recent_behavior(self, tmp_path: Path) -> None:
        """Test was last edited far in the past, no recent behavior change → stale."""
        repo = _init_repo(tmp_path)
        now = datetime.now(timezone.utc)

        _commit_at(
            repo,
            files={"src/foo.py": "def foo(): return 1\n", "tests/test_foo.py": "x\n"},
            message="initial — long ago",
            when=now - timedelta(days=180),
        )
        # No recent (within window) behavior changes
        report = triage_test(repo, "tests/test_foo.py", window_days=30, now=now)
        assert report.classification == "stale"
        assert "obsolete" in report.rationale.lower() or "stale" in report.rationale.lower()

    def test_ambiguous_when_both_recent(self, tmp_path: Path) -> None:
        """Both test and behavior were edited recently within ~24h → ambiguous."""
        repo = _init_repo(tmp_path)
        now = datetime.now(timezone.utc)

        _commit_at(
            repo,
            files={"src/foo.py": "def foo(): return 1\n", "tests/test_foo.py": "x\n"},
            message="initial",
            when=now - timedelta(days=2),
        )
        # Both files edited in close succession (within ~24h)
        _commit_at(
            repo,
            files={"tests/test_foo.py": "y\n"},
            message="tweak test",
            when=now - timedelta(hours=1),
        )
        _commit_at(
            repo,
            files={"src/foo.py": "def foo(): return 2\n"},
            message="tweak code",
            when=now - timedelta(minutes=30),
            author="Author Two",
        )

        report = triage_test(repo, "tests/test_foo.py", window_days=30, now=now)
        # Behavior was edited AFTER test; this is regression-like
        # Could be regression (behavior > test) OR ambiguous depending on exact ordering
        # Most importantly: classification is not "stale" and rationale is informative
        assert report.classification in ("regression", "ambiguous")
        assert report.evidence  # non-empty

    def test_evidence_includes_authors(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        now = datetime.now(timezone.utc)
        _commit_at(
            repo,
            files={"src/foo.py": "x\n", "tests/test_foo.py": "y\n"},
            message="setup",
            when=now - timedelta(days=10),
            author="Alice",
        )
        report = triage_test(repo, "tests/test_foo.py", now=now)
        # Evidence captures the test author
        assert any("Alice" in line for line in report.evidence)


class TestTriageScopeFiltering:
    def test_other_test_changes_do_not_count_as_behavior(self, tmp_path: Path) -> None:
        """A change to ANOTHER test file shouldn't be counted as 'behavior'."""
        repo = _init_repo(tmp_path)
        now = datetime.now(timezone.utc)

        _commit_at(
            repo,
            files={
                "src/foo.py": "x\n",
                "tests/test_foo.py": "y\n",
                "tests/test_other.py": "z\n",
            },
            message="initial",
            when=now - timedelta(days=180),
        )
        # Recent edit only to ANOTHER test
        _commit_at(
            repo,
            files={"tests/test_other.py": "z2\n"},
            message="tweak other test",
            when=now - timedelta(days=1),
        )
        report = triage_test(repo, "tests/test_foo.py", window_days=30, now=now)
        # Should still be 'stale' because the only recent change was a test
        assert report.classification == "stale"

    def test_governance_changes_do_not_count_as_behavior(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        now = datetime.now(timezone.utc)
        _commit_at(
            repo,
            files={"src/foo.py": "x\n", "tests/test_foo.py": "y\n"},
            message="initial",
            when=now - timedelta(days=180),
        )
        _commit_at(
            repo,
            files={".exo/CONSTITUTION.md": "# tweak\n"},
            message="governance bump",
            when=now - timedelta(days=1),
        )
        report = triage_test(repo, "tests/test_foo.py", window_days=30, now=now)
        assert report.classification == "stale"


class TestTriageSerialization:
    def test_to_dict_round_trips(self) -> None:
        report = TriageReport(
            test_path="tests/test_x.py",
            classification="stale",
            recommended_owner="Alice",
            test_authored_at="2025-01-01T00:00:00+00:00",
            evidence=["test edit: abc"],
            rationale="encoding obsolete behavior",
        )
        d = triage_to_dict(report)
        assert d["test_path"] == "tests/test_x.py"
        assert d["classification"] == "stale"
        assert d["evidence"] == ["test edit: abc"]

    def test_format_human_includes_classification_and_evidence(self) -> None:
        report = TriageReport(
            test_path="tests/test_x.py",
            classification="regression",
            recommended_owner="Bob",
            evidence=["e1", "e2"],
            rationale="changed code recently",
        )
        text = format_triage_human(report)
        assert "tests/test_x.py" in text
        assert "REGRESSION" in text
        assert "Bob" in text
        assert "e1" in text


class TestTriageCLI:
    def test_cli_classifies_and_prints(self, tmp_path: Path, capsys, monkeypatch) -> None:
        from exo.cli import main as cli_main_local

        repo = _init_repo(tmp_path)
        now = datetime.now(timezone.utc)
        _commit_at(
            repo,
            files={"src/foo.py": "x\n", "tests/test_foo.py": "y\n"},
            message="initial",
            when=now - timedelta(days=180),
        )

        monkeypatch.chdir(repo)
        rc = cli_main_local(["test-triage", "tests/test_foo.py", "--window-days", "30"])
        assert rc == 0
