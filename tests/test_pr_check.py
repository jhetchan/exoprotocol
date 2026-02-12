"""Tests for PR governance check: exo pr-check.

Covers:
- Commit listing from git log
- Commit-to-session matching by timestamp windows
- Scope coverage checking
- Governance integrity verification
- Verdict logic (pass / warn / fail)
- Human-readable and dict formatting
- CLI integration
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from exo.kernel import governance as governance_mod
from exo.kernel import tickets as tickets_mod
from exo.stdlib.pr_check import (
    CommitInfo,
    CommitVerdict,
    PRCheckReport,
    SessionVerdict,
    _check_scope_coverage,
    _list_commits,
    _load_session_index,
    _match_commits_to_sessions,
    format_pr_check_human,
    pr_check,
    pr_check_to_dict,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _policy_block(rule: dict[str, Any]) -> str:
    return f"\n```yaml exo-policy\n{json.dumps(rule)}\n```\n"


def _bootstrap_repo(tmp_path: Path) -> Path:
    """Create a minimal repo with .exo governance and a git history."""
    repo = tmp_path
    exo_dir = repo / ".exo"
    exo_dir.mkdir(parents=True, exist_ok=True)

    constitution = (
        "# Test Constitution\n\n"
        + _policy_block({
            "id": "RULE-SEC-001",
            "type": "filesystem_deny",
            "patterns": ["**/.env*"],
            "actions": ["read", "write"],
            "message": "Secret deny",
        })
    )
    (exo_dir / "CONSTITUTION.md").write_text(constitution, encoding="utf-8")

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@exo"], cwd=str(repo), capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True, text=True)

    # Compile governance
    governance_mod.compile_constitution(repo)

    # Initial commit on main
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit", "--allow-empty"],
        cwd=str(repo), capture_output=True, text=True,
    )
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(repo), capture_output=True, text=True)

    return repo


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )


def _make_commit(repo: Path, filename: str, content: str, message: str) -> str:
    """Create a file and commit it. Returns the commit SHA."""
    (repo / filename).parent.mkdir(parents=True, exist_ok=True)
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", message)
    proc = _git(repo, "rev-parse", "HEAD")
    return proc.stdout.strip()


def _write_session_index(repo: Path, entries: list[dict[str, Any]]) -> None:
    """Write session index entries as JSONL."""
    index_dir = repo / ".exo" / "memory" / "sessions"
    index_dir.mkdir(parents=True, exist_ok=True)
    with (index_dir / "index.jsonl").open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _make_session_entry(
    session_id: str = "sess-001",
    ticket_id: str = "TICKET-001",
    actor: str = "agent:test",
    vendor: str = "anthropic",
    model: str = "claude-3",
    mode: str = "work",
    verify: str = "passed",
    drift_score: float | None = 0.25,
    started_at: str = "",
    finished_at: str = "",
) -> dict[str, Any]:
    """Create a session index entry with sensible defaults."""
    now = datetime.now(timezone.utc)
    return {
        "session_id": session_id,
        "ticket_id": ticket_id,
        "actor": actor,
        "vendor": vendor,
        "model": model,
        "mode": mode,
        "verify": verify,
        "drift_score": drift_score,
        "started_at": started_at or (now - timedelta(hours=2)).isoformat(),
        "finished_at": finished_at or now.isoformat(),
    }


def _seed_ticket(repo: Path, ticket_id: str = "TICKET-001", **overrides: Any) -> dict[str, Any]:
    """Save a ticket with default scope."""
    ticket = {
        "id": ticket_id,
        "title": "Test task",
        "kind": "task",
        "status": "active",
        "scope": {"allow": ["**"], "deny": []},
        "budgets": {"max_files_changed": 10, "max_loc_changed": 300},
        **overrides,
    }
    tickets_mod.save_ticket(repo, ticket)
    return ticket


# ---------------------------------------------------------------------------
# Unit tests: internal functions
# ---------------------------------------------------------------------------


class TestListCommits:
    def test_lists_commits_in_range(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _make_commit(repo, "a.txt", "hello", "commit A")
        _make_commit(repo, "b.txt", "world", "commit B")

        commits = _list_commits(repo, base_sha, "HEAD")
        assert len(commits) == 2
        messages = [c.message for c in commits]
        assert "commit A" in messages
        assert "commit B" in messages

    def test_empty_range_returns_empty(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        commits = _list_commits(repo, "HEAD", "HEAD")
        assert commits == []

    def test_commit_info_fields_populated(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()
        sha = _make_commit(repo, "c.txt", "data", "commit C")

        commits = _list_commits(repo, base, "HEAD")
        assert len(commits) == 1
        c = commits[0]
        assert c.sha == sha
        assert c.message == "commit C"
        assert c.author == "Test"
        assert c.timestamp  # non-empty ISO string


class TestMatchCommitsToSessions:
    def test_commit_within_session_window_matches(self) -> None:
        now = datetime.now(timezone.utc)
        commit = CommitInfo(
            sha="abc123",
            timestamp=(now - timedelta(minutes=30)).isoformat(),
            author="Test",
            message="work",
        )
        session = {
            "session_id": "sess-001",
            "ticket_id": "T-1",
            "started_at": (now - timedelta(hours=1)).isoformat(),
            "finished_at": now.isoformat(),
        }
        result = _match_commits_to_sessions([commit], [session])
        assert result["abc123"] is not None
        assert result["abc123"]["session_id"] == "sess-001"

    def test_commit_outside_window_unmatched(self) -> None:
        now = datetime.now(timezone.utc)
        commit = CommitInfo(
            sha="abc123",
            timestamp=(now - timedelta(hours=5)).isoformat(),
            author="Test",
            message="work",
        )
        session = {
            "session_id": "sess-001",
            "started_at": (now - timedelta(hours=1)).isoformat(),
            "finished_at": now.isoformat(),
        }
        result = _match_commits_to_sessions([commit], [session])
        assert result["abc123"] is None

    def test_multiple_sessions_first_match_wins(self) -> None:
        now = datetime.now(timezone.utc)
        commit = CommitInfo(
            sha="abc123",
            timestamp=(now - timedelta(minutes=30)).isoformat(),
            author="Test",
            message="work",
        )
        session_a = {
            "session_id": "sess-a",
            "started_at": (now - timedelta(hours=2)).isoformat(),
            "finished_at": now.isoformat(),
        }
        session_b = {
            "session_id": "sess-b",
            "started_at": (now - timedelta(hours=1)).isoformat(),
            "finished_at": now.isoformat(),
        }
        result = _match_commits_to_sessions([commit], [session_a, session_b])
        assert result["abc123"]["session_id"] == "sess-a"

    def test_no_sessions_all_unmatched(self) -> None:
        now = datetime.now(timezone.utc)
        commit = CommitInfo(
            sha="abc123",
            timestamp=now.isoformat(),
            author="Test",
            message="work",
        )
        result = _match_commits_to_sessions([commit], [])
        assert result["abc123"] is None

    def test_session_with_missing_timestamps_skipped(self) -> None:
        commit = CommitInfo(sha="abc", timestamp=datetime.now(timezone.utc).isoformat(), author="T", message="x")
        session = {"session_id": "s1", "started_at": "", "finished_at": ""}
        result = _match_commits_to_sessions([commit], [session])
        assert result["abc"] is None


class TestLoadSessionIndex:
    def test_loads_valid_jsonl(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _write_session_index(repo, [
            {"session_id": "s1", "ticket_id": "T-1"},
            {"session_id": "s2", "ticket_id": "T-2"},
        ])
        entries = _load_session_index(repo)
        assert len(entries) == 2
        assert entries[0]["session_id"] == "s1"

    def test_skips_invalid_json_lines(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        index_dir = repo / ".exo" / "memory" / "sessions"
        index_dir.mkdir(parents=True, exist_ok=True)
        (index_dir / "index.jsonl").write_text(
            '{"session_id":"s1"}\nNOT-JSON\n{"session_id":"s2"}\n',
            encoding="utf-8",
        )
        entries = _load_session_index(repo)
        assert len(entries) == 2

    def test_missing_index_returns_empty(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        entries = _load_session_index(repo)
        assert entries == []


# ---------------------------------------------------------------------------
# Integration tests: full pr_check()
# ---------------------------------------------------------------------------


class TestPRCheckIntegration:
    def test_all_commits_governed_passes(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-001")

        base = _git(repo, "rev-parse", "HEAD").stdout.strip()

        # Create session window covering the commits
        now = datetime.now(timezone.utc)
        session_start = (now - timedelta(minutes=5)).isoformat()

        _make_commit(repo, "src/a.py", "print('a')", "add a")
        _make_commit(repo, "src/b.py", "print('b')", "add b")

        session_finish = (now + timedelta(minutes=5)).isoformat()

        _write_session_index(repo, [
            _make_session_entry(
                session_id="sess-001",
                ticket_id="TICKET-001",
                started_at=session_start,
                finished_at=session_finish,
            ),
        ])

        report = pr_check(repo, base_ref=base, head_ref="HEAD")
        assert report.verdict == "pass"
        assert report.total_commits == 2
        assert report.governed_commits == 2
        assert report.ungoverned_commits == 0
        assert report.ungoverned_shas == []
        assert len(report.sessions) == 1
        assert report.sessions[0].session_id == "sess-001"

    def test_ungoverned_commits_fail(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()

        _make_commit(repo, "rogue.txt", "rogue", "ungoverned commit")
        # No session index at all

        report = pr_check(repo, base_ref=base, head_ref="HEAD")
        assert report.verdict == "fail"
        assert report.ungoverned_commits == 1
        assert len(report.ungoverned_shas) == 1
        assert any("outside any governed session" in r for r in report.reasons)

    def test_mixed_governed_and_ungoverned(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-001")

        base = _git(repo, "rev-parse", "HEAD").stdout.strip()

        now = datetime.now(timezone.utc)
        session_start = (now - timedelta(minutes=5)).isoformat()
        sha1 = _make_commit(repo, "governed.py", "x", "governed commit")
        session_finish = (now + timedelta(minutes=5)).isoformat()

        # Second commit outside any session (far future)
        _git(repo, "commit", "--allow-empty", "-m", "ungoverned commit",
             "--date", (now + timedelta(hours=10)).isoformat())

        _write_session_index(repo, [
            _make_session_entry(
                started_at=session_start,
                finished_at=session_finish,
            ),
        ])

        report = pr_check(repo, base_ref=base, head_ref="HEAD")
        assert report.verdict == "fail"
        assert report.total_commits == 2
        assert report.governed_commits == 1
        assert report.ungoverned_commits == 1

    def test_no_commits_in_range_passes(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = pr_check(repo, base_ref="HEAD", head_ref="HEAD")
        assert report.total_commits == 0
        assert report.verdict == "pass"

    def test_high_drift_session_warns(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-001")
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()

        now = datetime.now(timezone.utc)
        session_start = (now - timedelta(minutes=5)).isoformat()
        _make_commit(repo, "drift.py", "x", "drifty work")
        session_finish = (now + timedelta(minutes=5)).isoformat()

        _write_session_index(repo, [
            _make_session_entry(
                drift_score=0.85,
                started_at=session_start,
                finished_at=session_finish,
            ),
        ])

        report = pr_check(repo, base_ref=base, head_ref="HEAD", drift_threshold=0.7)
        assert report.verdict == "warn"
        assert any("drift score" in r for r in report.reasons)

    def test_failed_verify_session_fails(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-001")
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()

        now = datetime.now(timezone.utc)
        session_start = (now - timedelta(minutes=5)).isoformat()
        _make_commit(repo, "x.py", "x", "work")
        session_finish = (now + timedelta(minutes=5)).isoformat()

        _write_session_index(repo, [
            _make_session_entry(
                verify="failed",
                started_at=session_start,
                finished_at=session_finish,
            ),
        ])

        report = pr_check(repo, base_ref=base, head_ref="HEAD")
        assert report.verdict == "fail"
        assert any("verification failed" in r for r in report.reasons)

    def test_bypassed_verify_noted(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-001")
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()

        now = datetime.now(timezone.utc)
        session_start = (now - timedelta(minutes=5)).isoformat()
        _make_commit(repo, "bg.py", "x", "break glass work")
        session_finish = (now + timedelta(minutes=5)).isoformat()

        _write_session_index(repo, [
            _make_session_entry(
                verify="bypassed",
                started_at=session_start,
                finished_at=session_finish,
            ),
        ])

        report = pr_check(repo, base_ref=base, head_ref="HEAD")
        assert any("bypassed" in r for r in report.reasons)

    def test_scope_violations_warn(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        # Ticket only allows src/auth/**
        _seed_ticket(repo, "TICKET-001", scope={"allow": ["src/auth/**"], "deny": []})
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()

        now = datetime.now(timezone.utc)
        session_start = (now - timedelta(minutes=5)).isoformat()
        # Commit touches file outside scope
        _make_commit(repo, "docs/readme.md", "hello", "update docs")
        session_finish = (now + timedelta(minutes=5)).isoformat()

        _write_session_index(repo, [
            _make_session_entry(
                started_at=session_start,
                finished_at=session_finish,
            ),
        ])

        report = pr_check(repo, base_ref=base, head_ref="HEAD")
        assert report.verdict == "warn"
        assert len(report.scope_violations) > 0
        assert "docs/readme.md" in report.scope_violations

    def test_governance_integrity_failure(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()

        # Corrupt governance lock
        lock_path = repo / ".exo" / "governance.lock.json"
        lock_path.write_text("{}", encoding="utf-8")

        _make_commit(repo, "x.py", "x", "work")

        report = pr_check(repo, base_ref=base, head_ref="HEAD")
        assert report.governance_intact is False
        assert report.verdict == "fail"
        assert any("integrity" in r.lower() for r in report.reasons)

    def test_changed_files_tracked(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _make_commit(repo, "one.py", "1", "add one")
        _make_commit(repo, "two.py", "2", "add two")

        report = pr_check(repo, base_ref=base, head_ref="HEAD")
        assert "one.py" in report.changed_files
        assert "two.py" in report.changed_files

    def test_session_commit_count_tracked(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-001")
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()

        now = datetime.now(timezone.utc)
        session_start = (now - timedelta(minutes=5)).isoformat()
        _make_commit(repo, "a.py", "a", "first")
        _make_commit(repo, "b.py", "b", "second")
        _make_commit(repo, "c.py", "c", "third")
        session_finish = (now + timedelta(minutes=5)).isoformat()

        _write_session_index(repo, [
            _make_session_entry(started_at=session_start, finished_at=session_finish),
        ])

        report = pr_check(repo, base_ref=base, head_ref="HEAD")
        assert report.sessions[0].commit_count == 3

    def test_multiple_sessions_tracked(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-001")
        _seed_ticket(repo, "TICKET-002")
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()

        now = datetime.now(timezone.utc)

        # Session 1 window
        s1_start = (now - timedelta(minutes=10)).isoformat()
        s1_finish = (now - timedelta(minutes=5)).isoformat()
        # Commit in session 1 window
        _git(repo, "commit", "--allow-empty", "-m", "sess1-commit",
             "--date", (now - timedelta(minutes=7)).isoformat())

        # Session 2 window
        s2_start = (now - timedelta(minutes=4)).isoformat()
        s2_finish = now.isoformat()
        # Commit in session 2 window
        _git(repo, "commit", "--allow-empty", "-m", "sess2-commit",
             "--date", (now - timedelta(minutes=2)).isoformat())

        _write_session_index(repo, [
            _make_session_entry(
                session_id="sess-001", ticket_id="TICKET-001",
                started_at=s1_start, finished_at=s1_finish,
            ),
            _make_session_entry(
                session_id="sess-002", ticket_id="TICKET-002",
                started_at=s2_start, finished_at=s2_finish,
            ),
        ])

        report = pr_check(repo, base_ref=base, head_ref="HEAD")
        assert report.total_commits == 2
        assert report.governed_commits == 2
        session_ids = {s.session_id for s in report.sessions}
        assert "sess-001" in session_ids
        assert "sess-002" in session_ids


# ---------------------------------------------------------------------------
# Formatting tests
# ---------------------------------------------------------------------------


class TestFormatting:
    def _make_report(self, **overrides: Any) -> PRCheckReport:
        defaults = dict(
            base_ref="main",
            head_ref="HEAD",
            total_commits=2,
            governed_commits=2,
            ungoverned_commits=0,
            sessions=[],
            commits=[],
            ungoverned_shas=[],
            governance_intact=True,
            governance_hash="abc123",
            changed_files=["a.py", "b.py"],
            scope_violations=[],
            verdict="pass",
            reasons=[],
            checked_at="2026-02-11T12:00:00+00:00",
        )
        defaults.update(overrides)
        return PRCheckReport(**defaults)

    def test_human_format_pass(self) -> None:
        report = self._make_report(verdict="pass")
        text = format_pr_check_human(report)
        assert "PASS" in text
        assert "main..HEAD" in text
        assert "2 total" in text

    def test_human_format_fail(self) -> None:
        report = self._make_report(verdict="fail", reasons=["governance broken"])
        text = format_pr_check_human(report)
        assert "FAIL" in text
        assert "governance broken" in text

    def test_human_format_sessions(self) -> None:
        sv = SessionVerdict(
            session_id="sess-001",
            ticket_id="T-1",
            actor="agent:test",
            vendor="anthropic",
            model="claude-3",
            mode="work",
            verify="passed",
            drift_score=0.25,
            started_at="2026-02-11T10:00:00+00:00",
            finished_at="2026-02-11T12:00:00+00:00",
            commit_count=3,
        )
        report = self._make_report(sessions=[sv])
        text = format_pr_check_human(report)
        assert "sess-001" in text
        assert "agent:test" in text
        assert "drift=0.25" in text
        assert "commits=3" in text

    def test_human_format_scope_violations(self) -> None:
        report = self._make_report(
            scope_violations=["rogue.txt", "bad.py"],
            verdict="warn",
        )
        text = format_pr_check_human(report)
        assert "scope violations" in text.lower()

    def test_dict_conversion(self) -> None:
        report = self._make_report()
        d = pr_check_to_dict(report)
        assert isinstance(d, dict)
        assert d["verdict"] == "pass"
        assert d["total_commits"] == 2
        assert d["base_ref"] == "main"
        assert d["governance_intact"] is True

    def test_dict_roundtrip_sessions(self) -> None:
        sv = SessionVerdict(
            session_id="s1", ticket_id="T-1", actor="a", vendor="v",
            model="m", mode="work", verify="passed", drift_score=0.5,
            started_at="", finished_at="", commit_count=1,
        )
        report = self._make_report(sessions=[sv])
        d = pr_check_to_dict(report)
        assert len(d["sessions"]) == 1
        assert d["sessions"][0]["session_id"] == "s1"
        assert d["sessions"][0]["drift_score"] == 0.5


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestCLI:
    def test_pr_check_json_output(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _make_commit(repo, "f.py", "x", "add file")

        proc = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo),
             "pr-check", "--base", base, "--head", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode in (0, 1)  # may fail due to ungoverned commits
        data = json.loads(proc.stdout)
        assert "ok" in data
        assert "data" in data
        report_data = data["data"]
        assert "verdict" in report_data
        assert "total_commits" in report_data

    def test_pr_check_human_output(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _make_commit(repo, "f.py", "x", "add file")

        proc = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "human", "--repo", str(repo),
             "pr-check", "--base", base, "--head", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        # Should contain human-readable output
        assert "PR Governance Check" in proc.stdout

    def test_pr_check_custom_threshold(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        proc = subprocess.run(
            ["python3", "-m", "exo.cli", "--format", "json", "--repo", str(repo),
             "pr-check", "--base", "HEAD", "--head", "HEAD", "--drift-threshold", "0.5"],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0
        data = json.loads(proc.stdout)
        assert data["ok"] is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_commit_at_exact_session_start_matches(self) -> None:
        now = datetime.now(timezone.utc)
        commit = CommitInfo(sha="x", timestamp=now.isoformat(), author="T", message="m")
        session = {
            "session_id": "s1",
            "started_at": now.isoformat(),
            "finished_at": (now + timedelta(hours=1)).isoformat(),
        }
        result = _match_commits_to_sessions([commit], [session])
        assert result["x"] is not None

    def test_commit_at_exact_session_end_matches(self) -> None:
        now = datetime.now(timezone.utc)
        commit = CommitInfo(sha="x", timestamp=now.isoformat(), author="T", message="m")
        session = {
            "session_id": "s1",
            "started_at": (now - timedelta(hours=1)).isoformat(),
            "finished_at": now.isoformat(),
        }
        result = _match_commits_to_sessions([commit], [session])
        assert result["x"] is not None

    def test_empty_session_index_all_ungoverned(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _make_commit(repo, "x.py", "x", "work")

        report = pr_check(repo, base_ref=base, head_ref="HEAD")
        assert report.ungoverned_commits == 1
        assert report.verdict == "fail"

    def test_drift_threshold_boundary(self, tmp_path: Path) -> None:
        """Drift exactly at threshold should not trigger warning."""
        repo = _bootstrap_repo(tmp_path)
        _seed_ticket(repo, "TICKET-001")
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()

        now = datetime.now(timezone.utc)
        session_start = (now - timedelta(minutes=5)).isoformat()
        _make_commit(repo, "x.py", "x", "work")
        session_finish = (now + timedelta(minutes=5)).isoformat()

        _write_session_index(repo, [
            _make_session_entry(
                drift_score=0.7,  # exactly at threshold
                started_at=session_start,
                finished_at=session_finish,
            ),
        ])

        report = pr_check(repo, base_ref=base, head_ref="HEAD", drift_threshold=0.7)
        # drift_score (0.7) is NOT > threshold (0.7), so no warning
        assert report.verdict == "pass"

    def test_report_checked_at_populated(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        report = pr_check(repo, base_ref="HEAD", head_ref="HEAD")
        assert report.checked_at
        # Should be valid ISO timestamp
        datetime.fromisoformat(report.checked_at)
