# @feature:install-command
"""Tests for ``exo install`` — one-shot setup pipeline."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from exo.kernel import governance as governance_mod
from exo.kernel.utils import dump_yaml
from exo.stdlib.defaults import DEFAULT_CONFIG, DEFAULT_CONSTITUTION
from exo.stdlib.install import (
    _EXO_GITIGNORE_ENTRIES,
    InstallReport,
    InstallStep,
    _is_git_repo,
    format_install_human,
    install,
    install_to_dict,
    is_exo_tracked,
)

# ── Helpers ────────────────────────────────────────────────────────


def _bootstrap_repo(tmp_path: Path) -> Path:
    """Set up a fully compiled .exo/ governance scaffold."""
    repo = tmp_path
    exo_dir = repo / ".exo"
    exo_dir.mkdir(parents=True, exist_ok=True)
    (exo_dir / "CONSTITUTION.md").write_text(DEFAULT_CONSTITUTION, encoding="utf-8")
    dump_yaml(exo_dir / "config.yaml", DEFAULT_CONFIG)
    for d in ["tickets", "locks", "logs", "memory", "memory/sessions", "cache", "cache/sessions"]:
        (exo_dir / d).mkdir(parents=True, exist_ok=True)
    governance_mod.compile_constitution(repo)
    return repo


def _bare_repo(tmp_path: Path) -> Path:
    """Return a repo with NO .exo/ directory (greenfield)."""
    return tmp_path


# ── TestInstallGreenfield ──────────────────────────────────────────


class TestInstallGreenfield:
    """Fresh repo: should create .exo/, compile, generate adapters, install hooks, create gitignore."""

    def test_greenfield_succeeds(self, tmp_path: Path) -> None:
        report = install(tmp_path)
        assert report.succeeded
        assert report.overall == "ok"

    def test_greenfield_creates_exo_dir(self, tmp_path: Path) -> None:
        install(tmp_path)
        assert (tmp_path / ".exo").is_dir()
        assert (tmp_path / ".exo" / "CONSTITUTION.md").exists()

    def test_greenfield_compiles_governance(self, tmp_path: Path) -> None:
        install(tmp_path)
        assert (tmp_path / ".exo" / "governance.lock.json").exists()

    def test_greenfield_creates_gitignore(self, tmp_path: Path) -> None:
        install(tmp_path)
        gi = tmp_path / ".exo" / ".gitignore"
        assert gi.exists()
        content = gi.read_text(encoding="utf-8")
        assert "cache/" in content
        assert "locks/" in content

    def test_greenfield_step_names(self, tmp_path: Path) -> None:
        report = install(tmp_path)
        names = [s.name for s in report.steps]
        assert "init" in names
        assert "compile" in names
        assert "gitignore" in names


# ── TestInstallBrownfield ──────────────────────────────────────────


class TestInstallBrownfield:
    """Existing .exo/: should skip init, still compile, be idempotent."""

    def test_brownfield_skips_init(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = install(repo)
        init_step = next(s for s in report.steps if s.name == "init")
        assert init_step.status == "skipped"
        assert "already initialized" in init_step.summary

    def test_brownfield_still_compiles(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = install(repo)
        compile_step = next(s for s in report.steps if s.name == "compile")
        assert compile_step.status == "updated"
        assert "compiled" in compile_step.summary

    def test_brownfield_idempotent(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        r1 = install(repo)
        r2 = install(repo)
        assert r1.succeeded
        assert r2.succeeded

    def test_brownfield_preserves_existing_files(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        # Write a custom file into .exo/
        custom = repo / ".exo" / "custom_notes.txt"
        custom.write_text("my notes", encoding="utf-8")
        install(repo)
        assert custom.read_text(encoding="utf-8") == "my notes"


# ── TestInstallDryRun ──────────────────────────────────────────────


class TestInstallDryRun:
    """Dry run: no files should be written."""

    def test_dry_run_no_files(self, tmp_path: Path) -> None:
        report = install(tmp_path, dry_run=True)
        assert report.dry_run is True
        # .exo/ should NOT have been created
        assert not (tmp_path / ".exo" / "governance.lock.json").exists()

    def test_dry_run_report_ok(self, tmp_path: Path) -> None:
        report = install(tmp_path, dry_run=True)
        # All steps should be skipped
        for step in report.steps:
            assert step.status == "skipped", f"step {step.name} was {step.status}"


# ── TestInstallSkipFlags ──────────────────────────────────────────


class TestInstallSkipFlags:
    """--skip-init, --skip-hooks, --skip-adapters flags."""

    def test_skip_init(self, tmp_path: Path) -> None:
        report = install(tmp_path, skip_init=True)
        names = [s.name for s in report.steps]
        assert "init" not in names

    def test_skip_hooks(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = install(repo, skip_hooks=True)
        names = [s.name for s in report.steps]
        assert "hooks" not in names

    def test_skip_adapters(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = install(repo, skip_adapters=True)
        names = [s.name for s in report.steps]
        assert "adapters" not in names


# ── TestInstallGitignore ──────────────────────────────────────────


class TestInstallGitignore:
    """Gitignore creation and idempotency."""

    def test_creates_gitignore(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        install(repo)
        gi = repo / ".exo" / ".gitignore"
        assert gi.exists()
        content = gi.read_text(encoding="utf-8")
        for entry in _EXO_GITIGNORE_ENTRIES:
            assert entry in content

    def test_skips_if_exists(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        gi = repo / ".exo" / ".gitignore"
        gi.write_text("cache/\ncustom_stuff/\n", encoding="utf-8")
        report = install(repo)
        gitignore_step = next(s for s in report.steps if s.name == "gitignore")
        assert gitignore_step.status == "skipped"
        # Should preserve existing content
        assert "custom_stuff/" in gi.read_text(encoding="utf-8")

    def test_correct_entries(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        install(repo)
        content = (repo / ".exo" / ".gitignore").read_text(encoding="utf-8")
        assert "cache/" in content
        assert "memory/sessions/" in content
        assert "logs/" in content
        assert "locks/" in content
        assert "audit/" in content


# ── TestInstallErrorHandling ──────────────────────────────────────


class TestInstallErrorHandling:
    """Errors in one step don't block the rest."""

    def test_compile_error_doesnt_block_gitignore(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        # Corrupt constitution so compile fails
        (repo / ".exo" / "CONSTITUTION.md").write_text("", encoding="utf-8")
        report = install(repo)
        # Gitignore step should still run
        gitignore_step = next(s for s in report.steps if s.name == "gitignore")
        assert gitignore_step.status in ("created", "skipped")

    def test_no_constitution_skips_compile(self, tmp_path: Path) -> None:
        repo = tmp_path
        exo_dir = repo / ".exo"
        exo_dir.mkdir(parents=True, exist_ok=True)
        dump_yaml(exo_dir / "config.yaml", DEFAULT_CONFIG)
        # No CONSTITUTION.md — compile should skip
        report = install(repo, skip_init=True)
        compile_step = next(s for s in report.steps if s.name == "compile")
        assert compile_step.status == "skipped"
        assert "no CONSTITUTION.md" in compile_step.summary


# ── TestInstallSerialization ──────────────────────────────────────


class TestInstallSerialization:
    """install_to_dict() and format_install_human()."""

    def test_to_dict_structure(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = install(repo)
        data = install_to_dict(report)
        assert data["overall"] == "ok"
        assert data["succeeded"] is True
        assert data["dry_run"] is False
        assert isinstance(data["installed_at"], str)
        assert isinstance(data["steps"], list)
        assert data["step_count"] == len(report.steps)
        assert data["error_count"] == 0

    def test_to_dict_roundtrip(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = install(repo)
        data = install_to_dict(report)
        # Should be JSON serializable
        text = json.dumps(data)
        loaded = json.loads(text)
        assert loaded["overall"] == "ok"
        assert loaded["step_count"] == data["step_count"]

    def test_human_format_ok(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = install(repo)
        human = format_install_human(report)
        assert "ExoProtocol Install: OK" in human
        assert "errors: 0" in human

    def test_human_format_dry_run(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = install(repo, dry_run=True)
        human = format_install_human(report)
        assert "DRY RUN" in human

    def test_human_format_step_icons(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = install(repo)
        human = format_install_human(report)
        # Should have at least one [+] or [~] marker
        assert "[" in human


# ── TestInstallCLI ─────────────────────────────────────────────────


class TestInstallCLI:
    """CLI integration: exit codes, JSON output."""

    def test_cli_json_output(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "exo.cli", "--repo", str(tmp_path), "--format", "json", "install"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert data["data"]["overall"] == "ok"

    def test_cli_dry_run(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "exo.cli", "--repo", str(tmp_path), "--format", "json", "install", "--dry-run"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert data["data"]["dry_run"] is True

    def test_cli_human_output(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "exo.cli", "--repo", str(tmp_path), "--format", "human", "install"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "ExoProtocol Install" in result.stdout


# ── TestInstallDataclasses ─────────────────────────────────────────


class TestInstallDataclasses:
    """Direct dataclass property tests."""

    def test_report_succeeded_ok(self) -> None:
        r = InstallReport(overall="ok")
        assert r.succeeded is True

    def test_report_succeeded_error(self) -> None:
        r = InstallReport(overall="error")
        assert r.succeeded is False

    def test_report_error_count(self) -> None:
        r = InstallReport(
            steps=[
                InstallStep(name="a", status="created", summary="ok"),
                InstallStep(name="b", status="error", summary="fail"),
                InstallStep(name="c", status="error", summary="fail2"),
            ]
        )
        assert r.error_count == 2

    def test_step_defaults(self) -> None:
        s = InstallStep(name="test", status="created", summary="hello")
        assert s.details == {}


# ── TestIsExoTracked ──────────────────────────────────────────────


class TestIsExoTracked:
    """Test git tracking detection for .exo/ files."""

    def test_returns_false_for_non_git_repo(self, tmp_path: Path) -> None:
        assert is_exo_tracked(tmp_path) is False

    def test_returns_false_when_untracked(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        exo_dir = tmp_path / ".exo"
        exo_dir.mkdir()
        (exo_dir / "governance.lock.json").write_text("{}", encoding="utf-8")
        assert is_exo_tracked(tmp_path) is False

    def test_returns_true_when_tracked(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path),
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path),
            capture_output=True,
        )
        exo_dir = tmp_path / ".exo"
        exo_dir.mkdir()
        (exo_dir / "governance.lock.json").write_text("{}", encoding="utf-8")
        subprocess.run(
            ["git", "add", ".exo/governance.lock.json"],
            cwd=str(tmp_path),
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(tmp_path),
            capture_output=True,
        )
        assert is_exo_tracked(tmp_path) is True


class TestIsGitRepo:
    """Test git repo detection."""

    def test_non_git_dir(self, tmp_path: Path) -> None:
        assert _is_git_repo(tmp_path) is False

    def test_git_dir(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        assert _is_git_repo(tmp_path) is True


class TestInstallGitTrack:
    """Test the git_track step in install pipeline."""

    def test_git_track_step_present(self, tmp_path: Path) -> None:
        """Install pipeline includes git_track step."""
        report = install(tmp_path)
        step_names = [s.name for s in report.steps]
        assert "git_track" in step_names

    def test_git_track_skips_non_git(self, tmp_path: Path) -> None:
        """In a non-git repo, git_track step is skipped or errors gracefully."""
        report = install(tmp_path)
        git_step = [s for s in report.steps if s.name == "git_track"][0]
        # No git repo — should error or skip, not crash
        assert git_step.status in ("skipped", "error")

    def test_git_track_commits_in_git_repo(self, tmp_path: Path) -> None:
        """In a git repo with untracked .exo/, git_track commits the files."""
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path),
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path),
            capture_output=True,
        )
        report = install(tmp_path, skip_hooks=True)
        git_step = [s for s in report.steps if s.name == "git_track"][0]
        assert git_step.status == "created"
        assert is_exo_tracked(tmp_path) is True

    def test_git_track_skips_already_tracked(self, tmp_path: Path) -> None:
        """If .exo/ is already committed, git_track skips."""
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path),
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path),
            capture_output=True,
        )
        # First install: creates and commits
        install(tmp_path, skip_hooks=True)
        # Second install: should skip git_track
        report2 = install(tmp_path, skip_hooks=True)
        git_step = [s for s in report2.steps if s.name == "git_track"][0]
        assert git_step.status == "skipped"

    def test_git_track_dry_run(self, tmp_path: Path) -> None:
        """Dry run doesn't actually commit."""
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path),
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path),
            capture_output=True,
        )
        report = install(tmp_path, dry_run=True, skip_hooks=True)
        git_step = [s for s in report.steps if s.name == "git_track"][0]
        assert git_step.status == "skipped"
        assert is_exo_tracked(tmp_path) is False
