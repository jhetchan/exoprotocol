from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from exo.kernel import governance as governance_mod
from exo.kernel import tickets as tickets_mod
from exo.orchestrator import AgentSessionManager
from exo.stdlib.sidecar import commit_sidecar, init_sidecar_worktree, is_sidecar_worktree

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


# ---------------------------------------------------------------------------
# Helpers for sidecar API + session integration tests
# ---------------------------------------------------------------------------


def _policy_block(rule: dict) -> str:
    return f"\n```yaml exo-policy\n{json.dumps(rule)}\n```\n"


def _init_sidecar_repo(tmp_path: Path) -> Path:
    """Create a repo with git, governance, and sidecar worktree mounted."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    (repo / ".exo").mkdir(parents=True, exist_ok=True)
    constitution = "# Test Constitution\n\n" + _policy_block(
        {
            "id": "RULE-SEC-001",
            "type": "filesystem_deny",
            "patterns": ["**/.env*"],
            "actions": ["read", "write"],
            "message": "Secret deny",
        }
    )
    (repo / ".exo" / "CONSTITUTION.md").write_text(constitution, encoding="utf-8")

    init_sidecar_worktree(
        repo,
        branch="exo-governance",
        sidecar=".exo",
        init_git=True,
        default_branch="main",
        fetch_remote=False,
        commit_migration=True,
    )

    # Compile governance after sidecar is mounted (lock goes into .exo/)
    governance_mod.compile_constitution(repo)
    # Commit governance lock into sidecar
    commit_sidecar(repo, message="chore(exo): bootstrap governance")

    # Commit main branch baseline
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "chore: baseline")

    return repo


def _seed_ticket(repo: Path, ticket_id: str = "TICKET-111", actor: str = "agent:test") -> None:
    tickets_mod.save_ticket(
        repo,
        {
            "id": ticket_id,
            "type": "feature",
            "title": "Test ticket",
            "status": "active",
            "priority": 4,
            "scope": {"allow": ["**"], "deny": []},
            "checks": [],
        },
    )
    tickets_mod.acquire_lock(repo, ticket_id, owner=actor, role="developer", duration_hours=1)


def _sidecar_status(repo: Path) -> str:
    """Return git status output for the sidecar worktree."""
    proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo / ".exo",
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_TEST_ENV},
    )
    return proc.stdout.strip()


def _sidecar_log(repo: Path, n: int = 5) -> list[str]:
    """Return last N commit messages from sidecar worktree."""
    proc = subprocess.run(
        ["git", "log", f"-{n}", "--format=%s"],
        cwd=repo / ".exo",
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_TEST_ENV},
    )
    return [line for line in proc.stdout.strip().splitlines() if line]


# ===========================================================================
# API Tests: is_sidecar_worktree
# ===========================================================================


class TestIsSidecarWorktree:
    def test_true_when_mounted(self, tmp_path: Path) -> None:
        repo = _init_sidecar_repo(tmp_path)
        assert is_sidecar_worktree(repo) is True

    def test_false_when_plain_dir(self, tmp_path: Path) -> None:
        repo = tmp_path / "plain"
        repo.mkdir()
        (repo / ".exo").mkdir()
        assert is_sidecar_worktree(repo) is False

    def test_false_when_no_exo_dir(self, tmp_path: Path) -> None:
        repo = tmp_path / "empty"
        repo.mkdir()
        assert is_sidecar_worktree(repo) is False


# ===========================================================================
# API Tests: commit_sidecar
# ===========================================================================


class TestCommitSidecar:
    def test_commits_changes(self, tmp_path: Path) -> None:
        repo = _init_sidecar_repo(tmp_path)
        (repo / ".exo" / "new_file.txt").write_text("hello\n", encoding="utf-8")
        result = commit_sidecar(repo, message="test: add file")
        assert result["committed"] is True
        assert result["commit"] is not None
        assert len(result["commit"]) == 40  # full SHA
        assert result["branch"] == "exo-governance"

    def test_noop_when_clean(self, tmp_path: Path) -> None:
        repo = _init_sidecar_repo(tmp_path)
        result = commit_sidecar(repo, message="test: nothing to commit")
        assert result["committed"] is False
        assert result["commit"] is None
        assert result["branch"] == "exo-governance"

    def test_noop_no_worktree(self, tmp_path: Path) -> None:
        repo = tmp_path / "plain"
        repo.mkdir()
        (repo / ".exo").mkdir()
        result = commit_sidecar(repo, message="test: no worktree")
        assert result["committed"] is False
        assert result["commit"] is None
        assert result["branch"] is None

    def test_returns_branch(self, tmp_path: Path) -> None:
        repo = _init_sidecar_repo(tmp_path)
        (repo / ".exo" / "marker.txt").write_text("x\n", encoding="utf-8")
        result = commit_sidecar(repo, message="test: branch check")
        assert result["branch"] == "exo-governance"


# ===========================================================================
# Session Integration Tests
# ===========================================================================


class TestSessionSidecarAutoCommit:
    def test_start_auto_commits_sidecar(self, tmp_path: Path) -> None:
        repo = _init_sidecar_repo(tmp_path)
        _seed_ticket(repo)
        mgr = AgentSessionManager(repo, actor="agent:test")
        result = mgr.start(ticket_id="TICKET-111", vendor="test", model="test-model")
        assert "sidecar_commit" in result
        # Session start writes active session file into .exo/ — should be committed
        if result["sidecar_commit"] is not None:
            assert result["sidecar_commit"]["committed"] is True
            assert result["sidecar_commit"]["branch"] == "exo-governance"

    def test_finish_auto_commits_sidecar(self, tmp_path: Path) -> None:
        repo = _init_sidecar_repo(tmp_path)
        _seed_ticket(repo)
        mgr = AgentSessionManager(repo, actor="agent:test")
        mgr.start(ticket_id="TICKET-111", vendor="test", model="test-model")
        result = mgr.finish(summary="Done", set_status="review", skip_check=True, break_glass_reason="test")
        assert "sidecar_commit" in result
        # Session finish writes memento, index, removes active — should be committed
        if result["sidecar_commit"] is not None:
            assert result["sidecar_commit"]["committed"] is True
        # Sidecar should be clean after finish
        status = _sidecar_status(repo)
        assert status == "", f"Sidecar worktree not clean after finish: {status}"

    def test_finish_no_worktree_noop(self, tmp_path: Path) -> None:
        """No crash when sidecar is a plain directory (not a worktree)."""
        repo = tmp_path / "plain"
        repo.mkdir()
        exo = repo / ".exo"
        exo.mkdir()
        constitution = "# Test Constitution\n\n" + _policy_block(
            {
                "id": "RULE-SEC-001",
                "type": "filesystem_deny",
                "patterns": ["**/.env*"],
                "actions": ["read", "write"],
                "message": "Secret deny",
            }
        )
        (exo / "CONSTITUTION.md").write_text(constitution, encoding="utf-8")
        governance_mod.compile_constitution(repo)
        _seed_ticket(repo)
        mgr = AgentSessionManager(repo, actor="agent:test")
        mgr.start(ticket_id="TICKET-111", vendor="test", model="test-model")
        result = mgr.finish(summary="Done", set_status="review", skip_check=True, break_glass_reason="test")
        # Should not crash — sidecar_commit should be None
        assert result.get("sidecar_commit") is None

    def test_finish_sidecar_commit_in_result(self, tmp_path: Path) -> None:
        repo = _init_sidecar_repo(tmp_path)
        _seed_ticket(repo)
        mgr = AgentSessionManager(repo, actor="agent:test")
        mgr.start(ticket_id="TICKET-111", vendor="test", model="test-model")
        result = mgr.finish(summary="Done", set_status="review", skip_check=True, break_glass_reason="test")
        # sidecar_commit key always present
        assert "sidecar_commit" in result
        sc = result["sidecar_commit"]
        if sc is not None:
            assert "committed" in sc
            assert "commit" in sc
            assert "branch" in sc

    def test_sidecar_commit_message_format(self, tmp_path: Path) -> None:
        repo = _init_sidecar_repo(tmp_path)
        _seed_ticket(repo)
        mgr = AgentSessionManager(repo, actor="agent:test")
        start_result = mgr.start(ticket_id="TICKET-111", vendor="test", model="test-model")
        session_id = start_result["session"]["session_id"]
        mgr.finish(summary="Done", set_status="review", skip_check=True, break_glass_reason="test")
        messages = _sidecar_log(repo, n=5)
        # Should find commit messages containing session-start and session-finish
        start_msgs = [m for m in messages if "session-start" in m]
        finish_msgs = [m for m in messages if "session-finish" in m]
        assert len(start_msgs) >= 1, f"No session-start commit found in: {messages}"
        assert len(finish_msgs) >= 1, f"No session-finish commit found in: {messages}"
        # Messages should contain session_id and ticket_id
        assert any(session_id in m for m in start_msgs), f"session_id {session_id} not in start messages: {start_msgs}"
        assert any("TICKET-111" in m for m in finish_msgs), f"TICKET-111 not in finish messages: {finish_msgs}"
