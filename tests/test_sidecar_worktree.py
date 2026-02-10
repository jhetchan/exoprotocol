from __future__ import annotations

import os
import subprocess
from pathlib import Path

from exo.stdlib.sidecar import init_sidecar_worktree


_GIT_TEST_ENV = {
    "GIT_AUTHOR_NAME": "ExoProtocol",
    "GIT_AUTHOR_EMAIL": "exo@local.invalid",
    "GIT_COMMITTER_NAME": "ExoProtocol",
    "GIT_COMMITTER_EMAIL": "exo@local.invalid",
}


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(_GIT_TEST_ENV)
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(
            "git command failed\n"
            f"command: git {' '.join(args)}\n"
            f"cwd: {cwd}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}\n"
        )
    return proc


def test_sidecar_init_bootstraps_and_splits_history(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    (repo / "app.txt").write_text("app timeline\n", encoding="utf-8")
    (repo / ".exo").mkdir(parents=True, exist_ok=True)
    (repo / ".exo" / "CONSTITUTION.md").write_text("# Governance lane\n", encoding="utf-8")

    result = init_sidecar_worktree(
        repo,
        branch="exo-governance",
        sidecar=".exo",
        init_git=True,
        default_branch="main",
        fetch_remote=False,
        commit_migration=True,
    )

    assert result["git_repo_created"] is True
    assert result["migrated_existing_sidecar"] is True
    assert result["governance_branch"] == "exo-governance"
    assert result["sidecar_rel"] == ".exo"
    assert (repo / ".exo" / "CONSTITUTION.md").exists()
    assert _git(repo / ".exo", "symbolic-ref", "--short", "HEAD").stdout.strip() == "exo-governance"
    assert ".exo/" in (repo / ".gitignore").read_text(encoding="utf-8").splitlines()

    _git(repo, "add", ".gitignore", "app.txt")
    _git(repo, "commit", "-m", "chore: app baseline")

    main_files = set(_git(repo, "ls-tree", "-r", "--name-only", "main").stdout.splitlines())
    governance_files = set(_git(repo, "ls-tree", "-r", "--name-only", "exo-governance").stdout.splitlines())

    assert "app.txt" in main_files
    assert "CONSTITUTION.md" not in main_files
    assert "CONSTITUTION.md" in governance_files
    assert "app.txt" not in governance_files


def test_sidecar_init_is_idempotent_when_already_mounted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".exo").mkdir(parents=True, exist_ok=True)
    (repo / ".exo" / "CONSTITUTION.md").write_text("# Governance lane\n", encoding="utf-8")

    _ = init_sidecar_worktree(
        repo,
        branch="exo-governance",
        sidecar=".exo",
        init_git=True,
        default_branch="main",
        fetch_remote=False,
    )
    second = init_sidecar_worktree(
        repo,
        branch="exo-governance",
        sidecar=".exo",
        init_git=True,
        default_branch="main",
        fetch_remote=False,
    )

    assert second["already_mounted"] is True
    assert second["worktree_added"] is False
    assert second["governance_branch"] == "exo-governance"
    assert second["sidecar_rel"] == ".exo"
