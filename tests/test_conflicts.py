"""Tests for session-start intelligence: scope conflicts, unmerged work, ticket gating.

Verifies:
- StartAdvisory dataclass
- _scopes_overlap() algorithm
- detect_scope_conflicts() with sibling sessions
- detect_unmerged_work() with session index + git branch merging
- detect_ticket_issues() for branch mismatch and ticket contention
- format_advisories() and advisories_to_dicts()
- Integration: advisories injected into session bootstrap prompt
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from exo.kernel import governance as governance_mod
from exo.kernel import tickets as tickets_mod
from exo.kernel.utils import ensure_dir
from exo.orchestrator import AgentSessionManager
from exo.orchestrator.session import (
    SESSION_CACHE_DIR,
    SESSION_INDEX_PATH,
)
from exo.stdlib.conflicts import (
    RESOURCE_PROFILES,
    StartAdvisory,
    _base_divergence,
    _merged_branches,
    _read_session_index,
    _scopes_overlap,
    _share_directory_prefix,
    _upstream_status,
    advisories_to_dicts,
    detect_base_divergence,
    detect_machine_load,
    detect_scope_conflicts,
    detect_stale_branch,
    detect_ticket_issues,
    detect_unmerged_work,
    format_advisories,
    format_git_workflow,
    format_machine_context,
    machine_snapshot,
)

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


def _write_sibling(repo: Path, actor: str, ticket_id: str, branch: str = "") -> None:
    """Write a fake sibling active session file."""
    cache_dir = repo / SESSION_CACHE_DIR
    ensure_dir(cache_dir)
    safe_actor = actor.replace(":", "-")
    payload = {
        "session_id": f"SES-{safe_actor}",
        "actor": actor,
        "ticket_id": ticket_id,
        "git_branch": branch,
        "status": "active",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "pid": 99999,
    }
    (cache_dir / f"{safe_actor}.active.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_index_row(repo: Path, row: dict) -> None:
    """Append a row to the session index."""
    idx_path = repo / SESSION_INDEX_PATH
    ensure_dir(idx_path.parent)
    with idx_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=True) + "\n")


# ── TestStartAdvisory ────────────────────────────────────────────────


class TestStartAdvisory:
    def test_fields(self) -> None:
        adv = StartAdvisory(
            kind="scope_conflict",
            severity="warning",
            message="overlap detected",
            detail={"patterns": ["src/**"]},
        )
        assert adv.kind == "scope_conflict"
        assert adv.severity == "warning"
        assert adv.message == "overlap detected"
        assert adv.detail == {"patterns": ["src/**"]}

    def test_default_detail(self) -> None:
        adv = StartAdvisory(kind="info", severity="info", message="test")
        assert adv.detail == {}


# ── TestScopesOverlap ────────────────────────────────────────────────


class TestScopesOverlap:
    def test_both_default_no_overlap(self) -> None:
        """Both tickets with default ["**"] → no warning (too noisy)."""
        overlaps, patterns = _scopes_overlap({"allow": ["**"]}, {"allow": ["**"]})
        assert not overlaps
        assert patterns == []

    def test_one_default_one_specific(self) -> None:
        """One default, one specific → overlap = specific patterns."""
        overlaps, patterns = _scopes_overlap({"allow": ["**"]}, {"allow": ["src/**", "tests/**"]})
        assert overlaps
        assert patterns == ["src/**", "tests/**"]

    def test_one_specific_one_default(self) -> None:
        """Reverse of above."""
        overlaps, patterns = _scopes_overlap({"allow": ["src/api/**"]}, {"allow": ["**"]})
        assert overlaps
        assert patterns == ["src/api/**"]

    def test_both_specific_matching(self) -> None:
        """Both specific with exact matching patterns → overlap."""
        overlaps, patterns = _scopes_overlap({"allow": ["src/**"]}, {"allow": ["src/**"]})
        assert overlaps
        assert "src/**" in patterns

    def test_both_specific_disjoint(self) -> None:
        """Both specific with completely disjoint patterns → no overlap."""
        overlaps, patterns = _scopes_overlap({"allow": ["src/**"]}, {"allow": ["docs/**"]})
        assert not overlaps
        assert patterns == []

    def test_fnmatch_parent_child(self) -> None:
        """src/** vs src/api/** → overlap (parent subsumes child)."""
        overlaps, patterns = _scopes_overlap({"allow": ["src/**"]}, {"allow": ["src/api/**"]})
        assert overlaps

    def test_directory_prefix_sharing(self) -> None:
        """src/api/auth.py vs src/api/routes.py → overlap via shared prefix."""
        overlaps, patterns = _scopes_overlap({"allow": ["src/api/auth.py"]}, {"allow": ["src/api/routes.py"]})
        assert overlaps

    def test_empty_allow_no_overlap(self) -> None:
        """Empty allow lists → no overlap."""
        overlaps, patterns = _scopes_overlap({"allow": []}, {"allow": []})
        assert not overlaps


# ── TestShareDirectoryPrefix ─────────────────────────────────────────


class TestShareDirectoryPrefix:
    def test_same_dir(self) -> None:
        assert _share_directory_prefix("src/foo.py", "src/bar.py")

    def test_different_top_dirs(self) -> None:
        assert not _share_directory_prefix("src/foo.py", "docs/bar.py")

    def test_wildcard_after_prefix(self) -> None:
        assert _share_directory_prefix("src/**", "src/api/foo.py")


# ── TestDetectScopeConflicts ─────────────────────────────────────────


class TestDetectScopeConflicts:
    def test_no_siblings(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = detect_scope_conflicts(repo, "TICKET-111", {"allow": ["src/**"]}, [])
        assert result == []

    def test_sibling_with_overlap(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-111", scope_allow=["src/**"])
        _seed_ticket(repo, "TICKET-222", scope_allow=["src/api/**"])
        siblings = [
            {
                "actor": "agent:other",
                "ticket_id": "TICKET-222",
                "git_branch": "feature/api",
                "session_id": "SES-OTHER",
            }
        ]
        result = detect_scope_conflicts(
            repo,
            "TICKET-111",
            {"allow": ["src/**"]},
            siblings,
        )
        assert len(result) == 1
        assert result[0].kind == "scope_conflict"
        assert result[0].severity == "warning"
        assert "agent:other" in result[0].message
        assert "TICKET-222" in result[0].message

    def test_sibling_disjoint_scope(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-111", scope_allow=["src/**"])
        _seed_ticket(repo, "TICKET-222", scope_allow=["docs/**"])
        siblings = [
            {
                "actor": "agent:other",
                "ticket_id": "TICKET-222",
                "git_branch": "feature/docs",
            }
        ]
        result = detect_scope_conflicts(
            repo,
            "TICKET-111",
            {"allow": ["src/**"]},
            siblings,
        )
        assert result == []

    def test_sibling_missing_ticket(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        siblings = [
            {
                "actor": "agent:other",
                "ticket_id": "TICKET-MISSING",
                "git_branch": "feature/x",
            }
        ]
        result = detect_scope_conflicts(
            repo,
            "TICKET-111",
            {"allow": ["src/**"]},
            siblings,
        )
        assert result == []  # gracefully skipped

    def test_multiple_siblings_mixed(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-111", scope_allow=["src/**"])
        _seed_ticket(repo, "TICKET-222", scope_allow=["src/api/**"])
        _seed_ticket(repo, "TICKET-333", scope_allow=["docs/**"])
        siblings = [
            {"actor": "agent:a", "ticket_id": "TICKET-222", "git_branch": "f/a"},
            {"actor": "agent:b", "ticket_id": "TICKET-333", "git_branch": "f/b"},
        ]
        result = detect_scope_conflicts(
            repo,
            "TICKET-111",
            {"allow": ["src/**"]},
            siblings,
        )
        assert len(result) == 1  # Only TICKET-222 overlaps
        assert "agent:a" in result[0].message


# ── TestDetectUnmergedWork ───────────────────────────────────────────


class TestDetectUnmergedWork:
    def test_no_index(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = detect_unmerged_work(repo, "main", "TICKET-111", {"allow": ["src/**"]})
        assert result == []

    @patch("exo.stdlib.conflicts._merged_branches", return_value={"main", "feature/merged"})
    def test_merged_branch_no_advisory(self, _mock: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-222", scope_allow=["src/**"])
        _write_index_row(
            repo,
            {
                "session_id": "SES-OLD",
                "ticket_id": "TICKET-222",
                "actor": "agent:other",
                "git_branch": "feature/merged",
                "mode": "work",
                "finished_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        result = detect_unmerged_work(repo, "main", "TICKET-111", {"allow": ["src/**"]})
        assert result == []

    @patch("exo.stdlib.conflicts._merged_branches", return_value={"main"})
    def test_unmerged_branch_with_overlap(self, _mock: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-222", scope_allow=["src/**"])
        _write_index_row(
            repo,
            {
                "session_id": "SES-UNMERGED",
                "ticket_id": "TICKET-222",
                "actor": "agent:other",
                "git_branch": "feature/unmerged",
                "mode": "work",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "summary": "added API endpoint",
            },
        )
        result = detect_unmerged_work(repo, "main", "TICKET-111", {"allow": ["src/**"]})
        assert len(result) == 1
        assert result[0].kind == "unmerged_work"
        assert result[0].severity == "info"
        assert "feature/unmerged" in result[0].message

    @patch("exo.stdlib.conflicts._merged_branches", return_value={"main"})
    def test_unmerged_branch_no_overlap(self, _mock: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-222", scope_allow=["docs/**"])
        _write_index_row(
            repo,
            {
                "session_id": "SES-UNMERGED",
                "ticket_id": "TICKET-222",
                "actor": "agent:other",
                "git_branch": "feature/docs",
                "mode": "work",
                "finished_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        result = detect_unmerged_work(repo, "main", "TICKET-111", {"allow": ["src/**"]})
        assert result == []

    @patch("exo.stdlib.conflicts._merged_branches", return_value={"main"})
    def test_old_session_skipped(self, _mock: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-222", scope_allow=["src/**"])
        old_date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        _write_index_row(
            repo,
            {
                "session_id": "SES-OLD",
                "ticket_id": "TICKET-222",
                "actor": "agent:other",
                "git_branch": "feature/old",
                "mode": "work",
                "finished_at": old_date,
            },
        )
        result = detect_unmerged_work(
            repo,
            "main",
            "TICKET-111",
            {"allow": ["src/**"]},
            max_age_days=14,
        )
        assert result == []

    def test_empty_branch_skipped(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = detect_unmerged_work(repo, "", "TICKET-111", {"allow": ["src/**"]})
        assert result == []


# ── TestDetectTicketIssues ───────────────────────────────────────────


class TestDetectTicketIssues:
    def test_no_prior_sessions(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = detect_ticket_issues(repo, "TICKET-111", "feature/new", [])
        assert result == []

    def test_prior_same_branch(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_index_row(
            repo,
            {
                "session_id": "SES-PRIOR",
                "ticket_id": "TICKET-111",
                "actor": "agent:old",
                "git_branch": "feature/auth",
                "mode": "work",
            },
        )
        result = detect_ticket_issues(repo, "TICKET-111", "feature/auth", [])
        assert result == []

    def test_prior_different_branch(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_index_row(
            repo,
            {
                "session_id": "SES-PRIOR",
                "ticket_id": "TICKET-111",
                "actor": "agent:old",
                "git_branch": "feature/auth-v1",
                "mode": "work",
            },
        )
        result = detect_ticket_issues(repo, "TICKET-111", "feature/auth-v2", [])
        assert len(result) >= 1
        mismatch = [a for a in result if a.kind == "ticket_branch_mismatch"]
        assert len(mismatch) == 1
        assert "feature/auth-v1" in mismatch[0].message
        assert "feature/auth-v2" in mismatch[0].message

    def test_sibling_same_ticket_contention(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        siblings = [
            {
                "actor": "agent:cursor",
                "ticket_id": "TICKET-111",
                "git_branch": "feature/auth",
                "session_id": "SES-SIB",
            }
        ]
        result = detect_ticket_issues(repo, "TICKET-111", "feature/auth", siblings)
        contention = [a for a in result if a.kind == "ticket_contention"]
        assert len(contention) == 1
        assert "agent:cursor" in contention[0].message
        assert contention[0].severity == "warning"

    def test_both_mismatch_and_contention(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_index_row(
            repo,
            {
                "session_id": "SES-PRIOR",
                "ticket_id": "TICKET-111",
                "actor": "agent:old",
                "git_branch": "feature/v1",
                "mode": "work",
            },
        )
        siblings = [
            {
                "actor": "agent:cursor",
                "ticket_id": "TICKET-111",
                "git_branch": "feature/v2",
                "session_id": "SES-SIB",
            }
        ]
        result = detect_ticket_issues(repo, "TICKET-111", "feature/v2", siblings)
        kinds = {a.kind for a in result}
        assert "ticket_branch_mismatch" in kinds
        assert "ticket_contention" in kinds

    def test_no_ticket_id(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = detect_ticket_issues(repo, "", "main", [])
        assert result == []

    def test_audit_sessions_ignored(self, tmp_path: Path) -> None:
        """Audit sessions shouldn't trigger branch mismatch."""
        repo = _bootstrap_repo(tmp_path)
        _write_index_row(
            repo,
            {
                "session_id": "SES-AUDIT",
                "ticket_id": "TICKET-111",
                "actor": "agent:auditor",
                "git_branch": "feature/other",
                "mode": "audit",
            },
        )
        result = detect_ticket_issues(repo, "TICKET-111", "feature/main", [])
        mismatch = [a for a in result if a.kind == "ticket_branch_mismatch"]
        assert mismatch == []


# ── TestFormatAdvisories ─────────────────────────────────────────────


class TestFormatAdvisories:
    def test_empty_list(self) -> None:
        assert format_advisories([]) == ""

    def test_single_advisory(self) -> None:
        adv = StartAdvisory(kind="scope_conflict", severity="warning", message="overlap")
        result = format_advisories([adv])
        assert "## Start Advisories" in result
        assert "[WARNING] overlap" in result

    def test_mixed_severity_ordering(self) -> None:
        warnings = StartAdvisory(kind="scope_conflict", severity="warning", message="warn msg")
        info = StartAdvisory(kind="unmerged_work", severity="info", message="info msg")
        result = format_advisories([info, warnings])
        lines = result.strip().split("\n")
        # Warnings should come before info
        warn_idx = next(i for i, line in enumerate(lines) if "WARNING" in line)
        info_idx = next(i for i, line in enumerate(lines) if "INFO" in line)
        assert warn_idx < info_idx


# ── TestAdvisoriesToDicts ────────────────────────────────────────────


class TestAdvisoriesToDicts:
    def test_serialization(self) -> None:
        adv = StartAdvisory(
            kind="scope_conflict",
            severity="warning",
            message="test",
            detail={"foo": "bar"},
        )
        result = advisories_to_dicts([adv])
        assert len(result) == 1
        assert result[0]["kind"] == "scope_conflict"
        assert result[0]["severity"] == "warning"
        assert result[0]["message"] == "test"
        assert result[0]["detail"] == {"foo": "bar"}

    def test_empty_list(self) -> None:
        assert advisories_to_dicts([]) == []


# ── TestSessionStartAdvisories (integration) ────────────────────────


class TestSessionStartAdvisories:
    @patch("exo.orchestrator.session._current_git_branch", return_value="feature/api")
    def test_scope_conflict_in_bootstrap(self, _mock_branch: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-111", scope_allow=["src/api/**"])
        _seed_ticket(repo, "TICKET-222", scope_allow=["src/api/**"])
        _acquire_lock(repo, "TICKET-111")
        _write_sibling(repo, "agent:other", "TICKET-222", "feature/models")

        manager = AgentSessionManager(repo, actor="agent:test")
        result = manager.start(
            ticket_id="TICKET-111",
            vendor="anthropic",
            model="claude",
        )
        bootstrap = result["bootstrap_prompt"]
        assert "Start Advisories" in bootstrap
        assert "scope_conflict" in str(result.get("start_advisories") or "") or "agent:other" in bootstrap

    @patch(
        "exo.stdlib.conflicts.machine_snapshot",
        return_value={
            "cpu_count": 8,
            "load_avg_1m": 1.0,
            "ram_total_gb": 16.0,
            "ram_available_gb": 12.0,
        },
    )
    @patch("exo.orchestrator.session._current_git_branch", return_value="main")
    def test_no_conflicts_no_section(self, _mock_branch: object, _mock_snap: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-111", scope_allow=["src/**"])
        _acquire_lock(repo, "TICKET-111")
        # No siblings, no index entries

        manager = AgentSessionManager(repo, actor="agent:test")
        result = manager.start(
            ticket_id="TICKET-111",
            vendor="anthropic",
            model="claude",
        )
        bootstrap = result["bootstrap_prompt"]
        assert "Start Advisories" not in bootstrap
        assert result.get("start_advisories") is None

    @patch("exo.orchestrator.session._current_git_branch", return_value="feature/api")
    def test_advisories_in_return_dict(self, _mock_branch: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-111", scope_allow=["src/api/**"])
        _seed_ticket(repo, "TICKET-222", scope_allow=["src/api/**"])
        _acquire_lock(repo, "TICKET-111")
        _write_sibling(repo, "agent:other", "TICKET-222", "feature/models")

        manager = AgentSessionManager(repo, actor="agent:test")
        result = manager.start(
            ticket_id="TICKET-111",
            vendor="anthropic",
            model="claude",
        )
        advisories = result.get("start_advisories")
        assert advisories is not None
        assert isinstance(advisories, list)
        assert len(advisories) >= 1
        assert advisories[0]["kind"] == "scope_conflict"

    @patch("exo.orchestrator.session._current_git_branch", return_value="feature/api")
    def test_advisories_in_session_payload(self, _mock_branch: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-111", scope_allow=["src/api/**"])
        _seed_ticket(repo, "TICKET-222", scope_allow=["src/api/**"])
        _acquire_lock(repo, "TICKET-111")
        _write_sibling(repo, "agent:other", "TICKET-222", "feature/models")

        manager = AgentSessionManager(repo, actor="agent:test")
        result = manager.start(
            ticket_id="TICKET-111",
            vendor="anthropic",
            model="claude",
        )
        session = result["session"]
        advisories = session.get("start_advisories")
        assert advisories is not None
        assert len(advisories) >= 1


# ── TestMergedBranches ───────────────────────────────────────────────


class TestMergedBranches:
    @patch("exo.stdlib.conflicts.subprocess.run")
    def test_parses_git_output(self, mock_run: object, tmp_path: Path) -> None:
        mock_run.return_value = type(
            "R",
            (),
            {
                "returncode": 0,
                "stdout": "  main\n* feature/current\n  feature/old\n",
            },
        )()
        result = _merged_branches(tmp_path, "main")
        assert "main" in result
        assert "feature/current" in result
        assert "feature/old" in result

    @patch("exo.stdlib.conflicts.subprocess.run")
    def test_git_failure_returns_empty(self, mock_run: object, tmp_path: Path) -> None:
        mock_run.return_value = type("R", (), {"returncode": 1, "stdout": ""})()
        result = _merged_branches(tmp_path, "main")
        assert result == set()

    @patch("exo.stdlib.conflicts.subprocess.run", side_effect=FileNotFoundError)
    def test_no_git_returns_empty(self, _mock: object, tmp_path: Path) -> None:
        result = _merged_branches(tmp_path, "main")
        assert result == set()


# ── TestReadSessionIndex ─────────────────────────────────────────────


class TestReadSessionIndex:
    def test_no_file(self, tmp_path: Path) -> None:
        assert _read_session_index(tmp_path) == []

    def test_reads_rows(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_index_row(repo, {"session_id": "SES-1", "ticket_id": "T-1"})
        _write_index_row(repo, {"session_id": "SES-2", "ticket_id": "T-2"})
        rows = _read_session_index(repo)
        assert len(rows) == 2
        assert rows[0]["session_id"] == "SES-1"
        assert rows[1]["session_id"] == "SES-2"

    def test_skips_bad_json(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        idx_path = repo / SESSION_INDEX_PATH
        ensure_dir(idx_path.parent)
        idx_path.write_text('{"ok":true}\nnot-json\n{"ok":false}\n', encoding="utf-8")
        rows = _read_session_index(repo)
        assert len(rows) == 2


# ── TestUpstreamStatus ───────────────────────────────────────────────


class TestUpstreamStatus:
    @patch("exo.stdlib.conflicts.subprocess.run")
    def test_parses_behind_ahead(self, mock_run: object, tmp_path: Path) -> None:
        mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "3\t5\n"})()
        behind, ahead = _upstream_status(tmp_path, "main")
        assert behind == 5
        assert ahead == 3

    @patch("exo.stdlib.conflicts.subprocess.run")
    def test_git_failure(self, mock_run: object, tmp_path: Path) -> None:
        mock_run.return_value = type("R", (), {"returncode": 128, "stdout": ""})()
        behind, ahead = _upstream_status(tmp_path, "main")
        assert behind == 0
        assert ahead == 0

    @patch("exo.stdlib.conflicts.subprocess.run", side_effect=FileNotFoundError)
    def test_no_git(self, _mock: object, tmp_path: Path) -> None:
        behind, ahead = _upstream_status(tmp_path, "main")
        assert behind == 0
        assert ahead == 0


# ── TestDetectStaleBranch ────────────────────────────────────────────


class TestDetectStaleBranch:
    @patch("exo.stdlib.conflicts._upstream_status", return_value=(0, 0))
    def test_up_to_date(self, _mock: object, tmp_path: Path) -> None:
        result = detect_stale_branch(tmp_path, "feature/x")
        assert result == []

    @patch("exo.stdlib.conflicts._upstream_status", return_value=(5, 0))
    def test_behind_upstream(self, _mock: object, tmp_path: Path) -> None:
        result = detect_stale_branch(tmp_path, "feature/x")
        assert len(result) == 1
        assert result[0].kind == "stale_branch"
        assert result[0].severity == "warning"
        assert "5 commit(s) behind" in result[0].message
        assert "git pull --rebase" in result[0].message
        assert result[0].detail["behind"] == 5
        assert result[0].detail["diverged"] is False

    @patch("exo.stdlib.conflicts._upstream_status", return_value=(3, 2))
    def test_diverged(self, _mock: object, tmp_path: Path) -> None:
        result = detect_stale_branch(tmp_path, "feature/x")
        assert len(result) == 1
        assert result[0].kind == "stale_branch"
        assert "diverged" in result[0].message
        assert "2 local commit(s)" in result[0].message
        assert "3 upstream commit(s)" in result[0].message
        assert result[0].detail["diverged"] is True

    def test_empty_branch(self, tmp_path: Path) -> None:
        result = detect_stale_branch(tmp_path, "")
        assert result == []

    @patch("exo.stdlib.conflicts._upstream_status", return_value=(0, 5))
    def test_ahead_only_no_warning(self, _mock: object, tmp_path: Path) -> None:
        """Ahead-only (local commits, nothing upstream) → no warning."""
        result = detect_stale_branch(tmp_path, "feature/x")
        assert result == []


# ── TestMachineSnapshot ──────────────────────────────────────────────


class TestMachineSnapshot:
    def test_returns_dict_with_keys(self) -> None:
        snap = machine_snapshot()
        assert isinstance(snap, dict)
        assert "cpu_count" in snap
        assert "load_avg_1m" in snap
        assert "ram_total_gb" in snap
        assert "ram_available_gb" in snap

    def test_cpu_count_positive(self) -> None:
        snap = machine_snapshot()
        assert snap["cpu_count"] > 0

    @patch("exo.stdlib.conflicts.os.cpu_count", return_value=None)
    @patch("exo.stdlib.conflicts.os.getloadavg", side_effect=OSError)
    @patch("exo.stdlib.conflicts.platform.system", return_value="Unknown")
    def test_graceful_fallback(self, _sys: object, _load: object, _cpu: object) -> None:
        snap = machine_snapshot()
        assert snap["cpu_count"] == 0
        assert snap["load_avg_1m"] is None
        assert snap["ram_total_gb"] is None
        assert snap["ram_available_gb"] is None


# ── TestFormatMachineContext ─────────────────────────────────────────


class TestFormatMachineContext:
    def test_default_profile(self) -> None:
        snap = {"cpu_count": 8, "load_avg_1m": 2.5, "ram_total_gb": 16.0, "ram_available_gb": 8.0}
        result = format_machine_context(snap)
        assert "## Machine Context" in result
        assert "cpu_cores: 8" in result
        assert "load_avg_1m: 2.5" in result
        assert "8.0GB available / 16.0GB total" in result
        assert "resource_profile" not in result  # default is omitted
        assert "DIRECTIVE" not in result

    def test_heavy_profile(self) -> None:
        snap = {"cpu_count": 4, "load_avg_1m": 1.0, "ram_total_gb": 8.0, "ram_available_gb": 4.0}
        result = format_machine_context(snap, resource_profile="heavy")
        assert "resource_profile: heavy" in result
        assert "DIRECTIVE" in result
        assert "Serialize" in result

    def test_light_profile(self) -> None:
        snap = {"cpu_count": 4, "load_avg_1m": None, "ram_total_gb": None, "ram_available_gb": None}
        result = format_machine_context(snap, resource_profile="light")
        assert "resource_profile: light" in result
        assert "DIRECTIVE" not in result  # only heavy gets directive

    def test_missing_ram(self) -> None:
        snap = {"cpu_count": 4, "load_avg_1m": 1.0, "ram_total_gb": None, "ram_available_gb": None}
        result = format_machine_context(snap)
        assert "ram" not in result.lower() or "ram_total" not in result


# ── TestDetectMachineLoad ────────────────────────────────────────────


class TestDetectMachineLoad:
    def test_normal_load_no_advisory(self) -> None:
        snap = {"cpu_count": 8, "load_avg_1m": 2.0, "ram_available_gb": 8.0}
        result = detect_machine_load(snap)
        assert result == []

    def test_high_cpu_load(self) -> None:
        snap = {"cpu_count": 8, "load_avg_1m": 7.0, "ram_available_gb": 8.0}
        result = detect_machine_load(snap)
        assert len(result) == 1
        assert result[0].kind == "machine_load"
        assert result[0].severity == "warning"
        assert "CPU load" in result[0].message
        assert result[0].detail["load_high"] is True

    def test_low_ram(self) -> None:
        snap = {"cpu_count": 8, "load_avg_1m": 1.0, "ram_available_gb": 1.5}
        result = detect_machine_load(snap)
        assert len(result) == 1
        assert result[0].kind == "machine_load"
        assert "RAM" in result[0].message
        assert result[0].detail["ram_low"] is True

    def test_both_high_cpu_and_low_ram(self) -> None:
        snap = {"cpu_count": 4, "load_avg_1m": 4.0, "ram_available_gb": 1.0}
        result = detect_machine_load(snap)
        assert len(result) == 1
        assert result[0].detail["load_high"] is True
        assert result[0].detail["ram_low"] is True

    def test_sibling_count_in_message(self) -> None:
        snap = {"cpu_count": 4, "load_avg_1m": 4.0, "ram_available_gb": 8.0}
        result = detect_machine_load(snap, sibling_count=3)
        assert len(result) == 1
        assert "3 sibling session(s)" in result[0].message

    def test_heavy_profile_info_advisory(self) -> None:
        """Heavy profile emits info even when load is normal."""
        snap = {"cpu_count": 8, "load_avg_1m": 1.0, "ram_available_gb": 16.0}
        result = detect_machine_load(snap, resource_profile="heavy")
        assert len(result) == 1
        assert result[0].severity == "info"
        assert "resource_profile" in result[0].message
        assert result[0].detail["resource_profile"] == "heavy"

    def test_heavy_profile_under_load_is_warning(self) -> None:
        """Heavy profile + high load → warning (not info)."""
        snap = {"cpu_count": 4, "load_avg_1m": 4.0, "ram_available_gb": 1.0}
        result = detect_machine_load(snap, resource_profile="heavy")
        assert len(result) == 1
        assert result[0].severity == "warning"

    def test_none_values_no_crash(self) -> None:
        """All None metrics → no advisory, no crash."""
        snap = {"cpu_count": 0, "load_avg_1m": None, "ram_available_gb": None}
        result = detect_machine_load(snap)
        assert result == []


# ── TestResourceProfiles ─────────────────────────────────────────────


class TestResourceProfiles:
    def test_valid_profiles(self) -> None:
        assert {"default", "light", "heavy"} == RESOURCE_PROFILES

    def test_ticket_resource_profile_default(self, tmp_path: Path) -> None:
        """Tickets default to resource_profile='default'."""
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-111")
        ticket = tickets_mod.load_ticket(repo, "TICKET-111")
        assert ticket["resource_profile"] == "default"

    def test_ticket_resource_profile_heavy(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        tickets_mod.save_ticket(
            repo,
            {
                "id": "TICKET-222",
                "type": "feature",
                "title": "Heavy task",
                "status": "active",
                "resource_profile": "heavy",
            },
        )
        ticket = tickets_mod.load_ticket(repo, "TICKET-222")
        assert ticket["resource_profile"] == "heavy"

    def test_ticket_resource_profile_invalid_defaults(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        tickets_mod.save_ticket(
            repo,
            {
                "id": "TICKET-333",
                "type": "feature",
                "title": "Bad profile",
                "status": "active",
                "resource_profile": "ultra",
            },
        )
        ticket = tickets_mod.load_ticket(repo, "TICKET-333")
        assert ticket["resource_profile"] == "default"


# ── TestMachineContextIntegration ────────────────────────────────────


class TestMachineContextIntegration:
    @patch("exo.orchestrator.session._current_git_branch", return_value="main")
    def test_machine_context_in_bootstrap(self, _mock_branch: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-111")
        _acquire_lock(repo, "TICKET-111")

        manager = AgentSessionManager(repo, actor="agent:test")
        result = manager.start(
            ticket_id="TICKET-111",
            vendor="anthropic",
            model="claude",
        )
        bootstrap = result["bootstrap_prompt"]
        assert "## Machine Context" in bootstrap
        assert "cpu_cores:" in bootstrap

    @patch("exo.orchestrator.session._current_git_branch", return_value="main")
    def test_machine_snapshot_in_payload(self, _mock_branch: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-111")
        _acquire_lock(repo, "TICKET-111")

        manager = AgentSessionManager(repo, actor="agent:test")
        result = manager.start(
            ticket_id="TICKET-111",
            vendor="anthropic",
            model="claude",
        )
        session = result["session"]
        snap = session.get("machine_snapshot")
        assert snap is not None
        assert "cpu_count" in snap

    @patch("exo.orchestrator.session._current_git_branch", return_value="main")
    def test_heavy_ticket_directive_in_bootstrap(self, _mock_branch: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        tickets_mod.save_ticket(
            repo,
            {
                "id": "TICKET-HEAVY",
                "type": "feature",
                "title": "Heavy pipeline",
                "status": "active",
                "resource_profile": "heavy",
            },
        )
        _acquire_lock(repo, "TICKET-HEAVY")

        manager = AgentSessionManager(repo, actor="agent:test")
        result = manager.start(
            ticket_id="TICKET-HEAVY",
            vendor="anthropic",
            model="claude",
        )
        bootstrap = result["bootstrap_prompt"]
        assert "DIRECTIVE" in bootstrap
        assert "Serialize" in bootstrap


# ── TestBaseDivergence ───────────────────────────────────────────────


class TestBaseDivergence:
    @patch("exo.stdlib.conflicts.subprocess.run")
    def test_parses_git_output(self, mock_run: object, tmp_path: Path) -> None:
        mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "5\t20\n"})()
        behind, ahead = _base_divergence(tmp_path, "feature/x", "main")
        assert behind == 20
        assert ahead == 5

    @patch("exo.stdlib.conflicts.subprocess.run")
    def test_git_failure(self, mock_run: object, tmp_path: Path) -> None:
        mock_run.return_value = type("R", (), {"returncode": 128, "stdout": ""})()
        behind, ahead = _base_divergence(tmp_path, "feature/x", "main")
        assert behind == 0
        assert ahead == 0

    @patch("exo.stdlib.conflicts.subprocess.run", side_effect=FileNotFoundError)
    def test_no_git(self, _mock: object, tmp_path: Path) -> None:
        behind, ahead = _base_divergence(tmp_path, "feature/x", "main")
        assert behind == 0
        assert ahead == 0


class TestDetectBaseDivergence:
    @patch("exo.stdlib.conflicts._base_divergence", return_value=(0, 0))
    def test_no_divergence(self, _mock: object, tmp_path: Path) -> None:
        result = detect_base_divergence(tmp_path, "feature/x", "main")
        assert result == []

    @patch("exo.stdlib.conflicts._base_divergence", return_value=(10, 5))
    def test_below_threshold(self, _mock: object, tmp_path: Path) -> None:
        """10 commits behind but threshold is 15 → no advisory."""
        result = detect_base_divergence(tmp_path, "feature/x", "main", threshold=15)
        assert result == []

    @patch("exo.stdlib.conflicts._base_divergence", return_value=(20, 5))
    def test_behind_with_local_commits(self, _mock: object, tmp_path: Path) -> None:
        result = detect_base_divergence(tmp_path, "feature/x", "main", threshold=15)
        assert len(result) == 1
        assert result[0].kind == "base_divergence"
        assert result[0].severity == "warning"
        assert "20 commit(s) behind main" in result[0].message
        assert "5 local commit(s)" in result[0].message
        assert "git pull --rebase origin main" in result[0].message
        assert result[0].detail["base_branch"] == "main"

    @patch("exo.stdlib.conflicts._base_divergence", return_value=(30, 0))
    def test_behind_no_local_commits(self, _mock: object, tmp_path: Path) -> None:
        result = detect_base_divergence(tmp_path, "feature/x", "sandbox", threshold=15)
        assert len(result) == 1
        assert "30 commit(s) behind sandbox" in result[0].message
        assert "git pull --rebase origin sandbox" in result[0].message
        assert result[0].detail["base_branch"] == "sandbox"

    def test_same_branch_skipped(self, tmp_path: Path) -> None:
        """Working directly on main → no advisory."""
        result = detect_base_divergence(tmp_path, "main", "main")
        assert result == []

    def test_empty_branch_skipped(self, tmp_path: Path) -> None:
        result = detect_base_divergence(tmp_path, "", "main")
        assert result == []

    def test_empty_base_skipped(self, tmp_path: Path) -> None:
        result = detect_base_divergence(tmp_path, "feature/x", "")
        assert result == []

    @patch("exo.stdlib.conflicts._base_divergence", return_value=(16, 3))
    def test_custom_threshold(self, _mock: object, tmp_path: Path) -> None:
        # At threshold=16, 16 behind fires
        result = detect_base_divergence(tmp_path, "feature/x", "develop", threshold=16)
        assert len(result) == 1
        assert "develop" in result[0].message


# ── TestFormatGitWorkflow ────────────────────────────────────────────


class TestFormatGitWorkflow:
    def test_uses_base_branch(self) -> None:
        result = format_git_workflow("main")
        assert "## Git Workflow" in result
        assert "git pull --rebase origin main" in result
        assert "Keep commits atomic" in result

    def test_custom_base_branch(self) -> None:
        result = format_git_workflow("sandbox")
        assert "git pull --rebase origin sandbox" in result

    def test_develop_branch(self) -> None:
        result = format_git_workflow("develop")
        assert "git pull --rebase origin develop" in result


# ── TestGitWorkflowIntegration ───────────────────────────────────────


class TestGitWorkflowIntegration:
    @patch(
        "exo.stdlib.conflicts.machine_snapshot",
        return_value={
            "cpu_count": 8,
            "load_avg_1m": 1.0,
            "ram_total_gb": 16.0,
            "ram_available_gb": 12.0,
        },
    )
    @patch("exo.orchestrator.session._current_git_branch", return_value="feature/api")
    def test_git_workflow_in_bootstrap(self, _mock_branch: object, _mock_snap: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-111")
        _acquire_lock(repo, "TICKET-111")

        manager = AgentSessionManager(repo, actor="agent:test")
        result = manager.start(
            ticket_id="TICKET-111",
            vendor="anthropic",
            model="claude",
        )
        bootstrap = result["bootstrap_prompt"]
        assert "## Git Workflow" in bootstrap
        assert "git pull --rebase origin main" in bootstrap

    @patch(
        "exo.stdlib.conflicts.machine_snapshot",
        return_value={
            "cpu_count": 8,
            "load_avg_1m": 1.0,
            "ram_total_gb": 16.0,
            "ram_available_gb": 12.0,
        },
    )
    @patch("exo.orchestrator.session._current_git_branch", return_value="feature/api")
    def test_git_workflow_uses_lock_base(self, _mock_branch: object, _mock_snap: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-111")
        tickets_mod.acquire_lock(
            repo,
            "TICKET-111",
            owner="agent:test",
            role="developer",
            duration_hours=1,
            base="sandbox",
        )

        manager = AgentSessionManager(repo, actor="agent:test")
        result = manager.start(
            ticket_id="TICKET-111",
            vendor="anthropic",
            model="claude",
        )
        bootstrap = result["bootstrap_prompt"]
        assert "git pull --rebase origin sandbox" in bootstrap

    @patch(
        "exo.stdlib.conflicts.machine_snapshot",
        return_value={
            "cpu_count": 8,
            "load_avg_1m": 1.0,
            "ram_total_gb": 16.0,
            "ram_available_gb": 12.0,
        },
    )
    @patch("exo.orchestrator.session._current_git_branch", return_value="feature/api")
    def test_git_workflow_not_in_audit_mode(self, _mock_branch: object, _mock_snap: object, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-111")
        _acquire_lock(repo, "TICKET-111")

        manager = AgentSessionManager(repo, actor="agent:test")
        result = manager.start(
            ticket_id="TICKET-111",
            vendor="anthropic",
            model="claude",
            mode="audit",
        )
        bootstrap = result["bootstrap_prompt"]
        assert "## Git Workflow" not in bootstrap

    @patch("exo.stdlib.conflicts._base_divergence", return_value=(25, 3))
    @patch(
        "exo.stdlib.conflicts.machine_snapshot",
        return_value={
            "cpu_count": 8,
            "load_avg_1m": 1.0,
            "ram_total_gb": 16.0,
            "ram_available_gb": 12.0,
        },
    )
    @patch("exo.orchestrator.session._current_git_branch", return_value="feature/api")
    def test_base_divergence_advisory_in_bootstrap(
        self,
        _mock_branch: object,
        _mock_snap: object,
        _mock_div: object,
        tmp_path: Path,
    ) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-111")
        _acquire_lock(repo, "TICKET-111")

        manager = AgentSessionManager(repo, actor="agent:test")
        result = manager.start(
            ticket_id="TICKET-111",
            vendor="anthropic",
            model="claude",
        )
        bootstrap = result["bootstrap_prompt"]
        assert "Start Advisories" in bootstrap
        advisories = result.get("start_advisories") or []
        kinds = [a["kind"] for a in advisories]
        assert "base_divergence" in kinds
