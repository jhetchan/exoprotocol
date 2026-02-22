"""Tests for cross-branch contamination detection.

Covers:
- _git_head_sha() helper
- _classify_session_commits() — SHA-based commit classification
- Inherited commit detection at session-finish (TKT-20260222-233828-ZJYK)
- Scope blast radius from inherited commits (TKT-20260222-233829-ZEL8)
- PreToolUse hook gating git merge/pull (TKT-20260222-233828-63E8)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from exo.kernel import governance as governance_mod
from exo.kernel import tickets as tickets_mod

# ── Helpers ──────────────────────────────────────────────────────────


def _policy_block(payload: dict) -> str:
    return "```yaml exo-policy\n" + json.dumps(payload, ensure_ascii=True, indent=2) + "\n```\n"


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
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    return repo


def _seed_ticket(
    repo: Path,
    ticket_id: str = "TICKET-111",
    scope_allow: list[str] | None = None,
    scope_deny: list[str] | None = None,
) -> None:
    tickets_mod.save_ticket(
        repo,
        {
            "id": ticket_id,
            "type": "feature",
            "title": f"Test ticket {ticket_id}",
            "status": "active",
            "priority": 4,
            "scope": {
                "allow": scope_allow if scope_allow is not None else ["**"],
                "deny": scope_deny if scope_deny is not None else [],
            },
            "checks": [],
        },
    )


def _acquire_lock(repo: Path, ticket_id: str = "TICKET-111", owner: str = "agent:test") -> None:
    tickets_mod.acquire_lock(repo, ticket_id, owner=owner, role="developer", duration_hours=1)


# ── TestGitHeadSha ────────────────────────────────────────────────


class TestGitHeadSha:
    def test_returns_empty_on_non_git(self, tmp_path: Path) -> None:
        from exo.orchestrator.session import _git_head_sha

        assert _git_head_sha(tmp_path) == ""

    def test_returns_sha_on_mock(self) -> None:
        from exo.orchestrator.session import _git_head_sha

        with patch("exo.orchestrator.session.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc123def456\n")
            sha = _git_head_sha(Path("/tmp"))
        assert sha == "abc123def456"

    def test_handles_subprocess_error(self) -> None:
        from exo.orchestrator.session import _git_head_sha

        with patch("exo.orchestrator.session.subprocess.run", side_effect=FileNotFoundError):
            assert _git_head_sha(Path("/tmp")) == ""


# ── TestClassifySessionCommits ─────────────────────────────────────


class TestClassifySessionCommits:
    def test_empty_start_sha(self) -> None:
        from exo.orchestrator.session import _classify_session_commits

        result = _classify_session_commits(Path("/tmp"), "", "2026-01-01T00:00:00+00:00")
        assert result == {"session_commits": [], "inherited_commits": [], "inherited_files": []}

    def test_classifies_by_author_date(self) -> None:
        from exo.orchestrator.session import _classify_session_commits

        session_start = "2026-02-20T12:00:00+00:00"
        # One commit before session, one after
        git_log_output = (
            "aaa111 2026-02-20T11:00:00+00:00\n"  # before → inherited
            "bbb222 2026-02-20T13:00:00+00:00\n"  # after → session
        )

        def mock_run(cmd, **kwargs):
            if "log" in cmd:
                return MagicMock(returncode=0, stdout=git_log_output)
            if "diff-tree" in cmd:
                return MagicMock(returncode=0, stdout="lib/merged.py\n")
            return MagicMock(returncode=1, stdout="")

        with patch("exo.orchestrator.session.subprocess.run", side_effect=mock_run):
            result = _classify_session_commits(Path("/tmp"), "start123", session_start)

        assert result["session_commits"] == ["bbb222"]
        assert result["inherited_commits"] == ["aaa111"]
        assert result["inherited_files"] == ["lib/merged.py"]

    def test_all_session_commits(self) -> None:
        from exo.orchestrator.session import _classify_session_commits

        session_start = "2026-02-20T12:00:00+00:00"
        git_log_output = "ccc333 2026-02-20T14:00:00+00:00\n"

        with patch("exo.orchestrator.session.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=git_log_output)
            result = _classify_session_commits(Path("/tmp"), "start123", session_start)

        assert result["session_commits"] == ["ccc333"]
        assert result["inherited_commits"] == []
        assert result["inherited_files"] == []

    def test_all_inherited_commits(self) -> None:
        from exo.orchestrator.session import _classify_session_commits

        session_start = "2026-02-20T12:00:00+00:00"
        git_log_output = "ddd444 2026-02-19T10:00:00+00:00\neee555 2026-02-18T09:00:00+00:00\n"

        def mock_run(cmd, **kwargs):
            if "log" in cmd:
                return MagicMock(returncode=0, stdout=git_log_output)
            if "diff-tree" in cmd:
                sha = cmd[-1]
                if sha == "ddd444":
                    return MagicMock(returncode=0, stdout="a.py\nb.py\n")
                return MagicMock(returncode=0, stdout="c.py\n")
            return MagicMock(returncode=1, stdout="")

        with patch("exo.orchestrator.session.subprocess.run", side_effect=mock_run):
            result = _classify_session_commits(Path("/tmp"), "start123", session_start)

        assert result["session_commits"] == []
        assert len(result["inherited_commits"]) == 2
        assert sorted(result["inherited_files"]) == ["a.py", "b.py", "c.py"]

    def test_git_log_failure(self) -> None:
        from exo.orchestrator.session import _classify_session_commits

        with patch("exo.orchestrator.session.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            result = _classify_session_commits(Path("/tmp"), "start123", "2026-01-01T00:00:00+00:00")

        assert result["session_commits"] == []
        assert result["inherited_commits"] == []

    def test_no_commits_between_shas(self) -> None:
        from exo.orchestrator.session import _classify_session_commits

        with patch("exo.orchestrator.session.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = _classify_session_commits(Path("/tmp"), "start123", "2026-01-01T00:00:00+00:00")

        assert result == {"session_commits": [], "inherited_commits": [], "inherited_files": []}

    def test_deduplicates_inherited_files(self) -> None:
        from exo.orchestrator.session import _classify_session_commits

        session_start = "2026-02-20T12:00:00+00:00"
        git_log_output = "aaa 2026-02-19T10:00:00+00:00\nbbb 2026-02-19T11:00:00+00:00\n"

        def mock_run(cmd, **kwargs):
            if "log" in cmd:
                return MagicMock(returncode=0, stdout=git_log_output)
            if "diff-tree" in cmd:
                # Both commits touch same file
                return MagicMock(returncode=0, stdout="shared.py\n")
            return MagicMock(returncode=1, stdout="")

        with patch("exo.orchestrator.session.subprocess.run", side_effect=mock_run):
            result = _classify_session_commits(Path("/tmp"), "start123", session_start)

        assert result["inherited_files"] == ["shared.py"]  # deduplicated


# ── TestHeadShaRecordedAtStart ─────────────────────────────────────


class TestHeadShaRecordedAtStart:
    @patch("exo.orchestrator.session._git_head_sha", return_value="abc123start")
    @patch("exo.orchestrator.session._current_git_branch", return_value="main")
    def test_session_payload_contains_git_head_sha(
        self, _mock_branch: object, _mock_sha: object, tmp_path: Path
    ) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _acquire_lock(repo)

        from exo.orchestrator import AgentSessionManager

        manager = AgentSessionManager(repo, actor="agent:test")
        result = manager.start(ticket_id="TICKET-111", vendor="anthropic", model="claude")
        payload = result["session"]
        assert payload["git_head_sha"] == "abc123start"


# ── TestInheritedCommitsAtFinish ───────────────────────────────────


class TestInheritedCommitsAtFinish:
    @patch("exo.orchestrator.session._git_head_sha", return_value="start_sha_000")
    @patch("exo.orchestrator.session._current_git_branch", return_value="feature/x")
    def test_finish_reports_inherited_commits(self, _mock_branch: object, _mock_sha: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, scope_allow=["src/**"])
        _acquire_lock(repo)

        from exo.orchestrator import AgentSessionManager

        manager = AgentSessionManager(repo, actor="agent:test")
        manager.start(ticket_id="TICKET-111", vendor="anthropic", model="claude")

        classification = {
            "session_commits": ["sess1"],
            "inherited_commits": ["inh1", "inh2"],
            "inherited_files": ["lib/merged.py", "src/ok.py"],
        }
        with patch(
            "exo.orchestrator.session._classify_session_commits",
            return_value=classification,
        ):
            result = manager.finish(
                summary="test finish",
                ticket_id="TICKET-111",
                skip_check=True,
                break_glass_reason="test",
            )

        assert result["inherited_commit_count"] == 2
        assert result["inherited_commits"] == ["inh1", "inh2"]
        assert result["inherited_files"] == ["lib/merged.py", "src/ok.py"]
        # lib/merged.py is out of scope (allow: src/**)
        violations = result.get("merge_scope_violations") or []
        assert len(violations) == 1
        assert violations[0]["file"] == "lib/merged.py"

    @patch("exo.orchestrator.session._git_head_sha", return_value="")
    @patch("exo.orchestrator.session._current_git_branch", return_value="main")
    def test_finish_no_start_sha_no_crash(self, _mock_branch: object, _mock_sha: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _acquire_lock(repo)

        from exo.orchestrator import AgentSessionManager

        manager = AgentSessionManager(repo, actor="agent:test")
        manager.start(ticket_id="TICKET-111", vendor="anthropic", model="claude")

        result = manager.finish(
            summary="test",
            ticket_id="TICKET-111",
            skip_check=True,
            break_glass_reason="test",
        )
        assert result["inherited_commit_count"] == 0
        assert result.get("inherited_commits") is None

    @patch("exo.orchestrator.session._git_head_sha", return_value="start_sha_000")
    @patch("exo.orchestrator.session._current_git_branch", return_value="feature/x")
    def test_inherited_commits_in_memento(self, _mock_branch: object, _mock_sha: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _acquire_lock(repo)

        from exo.orchestrator import AgentSessionManager

        manager = AgentSessionManager(repo, actor="agent:test")
        manager.start(ticket_id="TICKET-111", vendor="anthropic", model="claude")

        classification = {
            "session_commits": [],
            "inherited_commits": ["inh1"],
            "inherited_files": ["merged.py"],
        }
        with patch(
            "exo.orchestrator.session._classify_session_commits",
            return_value=classification,
        ):
            result = manager.finish(
                summary="test",
                ticket_id="TICKET-111",
                skip_check=True,
                break_glass_reason="test",
            )

        memento = Path(repo / result["memento_path"]).read_text(encoding="utf-8")
        assert "Inherited Commits" in memento
        assert "mid-session merge" in memento
        assert "inh1" in memento
        assert "merged.py" in memento

    @patch("exo.orchestrator.session._git_head_sha", return_value="start_sha_000")
    @patch("exo.orchestrator.session._current_git_branch", return_value="feature/x")
    def test_inherited_commits_in_banner(self, _mock_branch: object, _mock_sha: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, scope_allow=["src/**"])
        _acquire_lock(repo)

        from exo.orchestrator import AgentSessionManager

        manager = AgentSessionManager(repo, actor="agent:test")
        manager.start(ticket_id="TICKET-111", vendor="anthropic", model="claude")

        classification = {
            "session_commits": [],
            "inherited_commits": ["inh1"],
            "inherited_files": ["lib/out.py"],
        }
        with patch(
            "exo.orchestrator.session._classify_session_commits",
            return_value=classification,
        ):
            result = manager.finish(
                summary="test",
                ticket_id="TICKET-111",
                skip_check=True,
                break_glass_reason="test",
            )

        banner = result.get("exo_banner", "")
        assert "MERGE" in banner

    @patch("exo.orchestrator.session._git_head_sha", return_value="start_sha_000")
    @patch("exo.orchestrator.session._current_git_branch", return_value="feature/x")
    def test_inherited_classification_error_is_advisory(
        self, _mock_branch: object, _mock_sha: object, tmp_path: Path
    ) -> None:
        """Classification failure should not crash session-finish."""
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _acquire_lock(repo)

        from exo.orchestrator import AgentSessionManager

        manager = AgentSessionManager(repo, actor="agent:test")
        manager.start(ticket_id="TICKET-111", vendor="anthropic", model="claude")

        with patch(
            "exo.orchestrator.session._classify_session_commits",
            side_effect=RuntimeError("git broke"),
        ):
            result = manager.finish(
                summary="test",
                ticket_id="TICKET-111",
                skip_check=True,
                break_glass_reason="test",
            )

        # Should succeed without crashing
        assert result["session_id"]
        assert result["inherited_commit_count"] == 0


# ── TestInheritedCommitsBanner ─────────────────────────────────────


class TestInheritedCommitsBanner:
    def test_banner_no_inherited_warnings(self) -> None:
        from exo.orchestrator.session import _exo_banner

        banner = _exo_banner(
            event="finish",
            ticket_id="T-1",
            verify="passed",
            inherited_warnings=None,
        )
        assert "MERGE" not in banner

    def test_banner_with_inherited_warnings(self) -> None:
        from exo.orchestrator.session import _exo_banner

        banner = _exo_banner(
            event="finish",
            ticket_id="T-1",
            verify="passed",
            inherited_warnings=["MERGE: 3 inherited commit(s)"],
        )
        assert "MERGE" in banner


# ── TestSessionIndexInheritedFields ────────────────────────────────


class TestSessionIndexInheritedFields:
    @patch("exo.orchestrator.session._git_head_sha", return_value="start_sha_000")
    @patch("exo.orchestrator.session._current_git_branch", return_value="main")
    def test_index_row_has_inherited_fields(self, _mock_branch: object, _mock_sha: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo)
        _acquire_lock(repo)

        from exo.orchestrator import AgentSessionManager
        from exo.orchestrator.session import SESSION_INDEX_PATH

        manager = AgentSessionManager(repo, actor="agent:test")
        manager.start(ticket_id="TICKET-111", vendor="anthropic", model="claude")

        classification = {
            "session_commits": ["s1"],
            "inherited_commits": ["i1"],
            "inherited_files": ["merged.py"],
        }
        with patch(
            "exo.orchestrator.session._classify_session_commits",
            return_value=classification,
        ):
            manager.finish(
                summary="test",
                ticket_id="TICKET-111",
                skip_check=True,
                break_glass_reason="test",
            )

        idx = (repo / SESSION_INDEX_PATH).read_text(encoding="utf-8").strip()
        row = json.loads(idx.splitlines()[-1])
        assert row["inherited_commit_count"] == 1
        assert row["inherited_files"] == ["merged.py"]


# ── TestEnforceHookGatesMerge ──────────────────────────────────────


class TestEnforceHookGatesMerge:
    def test_enforce_config_gates_git_merge(self) -> None:
        from exo.stdlib.hooks import generate_enforce_config

        config = generate_enforce_config()
        cmd = config["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert "git merge" in cmd

    def test_enforce_config_gates_git_pull(self) -> None:
        from exo.stdlib.hooks import generate_enforce_config

        config = generate_enforce_config()
        cmd = config["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert "git pull" in cmd

    def test_enforce_config_still_gates_commit_push(self) -> None:
        from exo.stdlib.hooks import generate_enforce_config

        config = generate_enforce_config()
        cmd = config["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert "git commit" in cmd
        assert "git push" in cmd
