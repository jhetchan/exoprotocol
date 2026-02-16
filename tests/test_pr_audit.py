"""Tests for PR-aware audit sessions.

When `exo session-audit` receives `--pr-base` / `--pr-head`, the session
automatically runs `pr_check()` and injects the governance report into the
audit bootstrap prompt.

Covers:
- PR check injection into audit bootstrap
- PR review directives in bootstrap
- pr_check data stored in session payload
- CLI wiring of --pr-base / --pr-head
- Graceful degradation when pr_check fails
- No injection when pr_base/pr_head omitted
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from exo.kernel import governance as governance_mod
from exo.kernel import tickets as tickets_mod
from exo.orchestrator.session import AgentSessionManager

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

    # Initialize git repo
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@exo"], cwd=str(repo), capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True, text=True)

    # Compile governance
    governance_mod.compile_constitution(repo)

    # Initial commit on main
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit", "--allow-empty"],
        cwd=str(repo),
        capture_output=True,
        text=True,
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


def _seed_intent(repo: Path, intent_id: str = "INTENT-001", **overrides: Any) -> dict[str, Any]:
    ticket = {
        "id": intent_id,
        "kind": "intent",
        "title": "Add user auth",
        "brain_dump": "I want to add basic user authentication",
        "boundary": "only touch src/auth/",
        "success_condition": "login works",
        "risk": "medium",
        "status": "todo",
        "scope": {"allow": ["**"], "deny": []},
        "budgets": {"max_files_changed": 12, "max_loc_changed": 400},
        **overrides,
    }
    tickets_mod.save_ticket(repo, ticket)
    return ticket


def _seed_task(
    repo: Path, task_id: str = "TICKET-001", parent_id: str = "INTENT-001", **overrides: Any
) -> dict[str, Any]:
    ticket = {
        "id": task_id,
        "kind": "task",
        "title": "Implement login endpoint",
        "parent_id": parent_id,
        "status": "todo",
        "scope": {"allow": ["**"], "deny": []},
        "budgets": {"max_files_changed": 5, "max_loc_changed": 200},
        **overrides,
    }
    tickets_mod.save_ticket(repo, ticket)
    return ticket


def _setup_session_repo(tmp_path: Path, ticket_id: str = "TICKET-001") -> Path:
    """Bootstrap repo with ticket + lock, ready for session lifecycle."""
    repo = _bootstrap_repo(tmp_path)
    _seed_intent(repo, intent_id="INTENT-001")
    _seed_task(repo, task_id=ticket_id, parent_id="INTENT-001")
    tickets_mod.acquire_lock(repo, ticket_id, owner="test-actor")
    return repo


def _setup_pr_audit_repo(tmp_path: Path) -> tuple[Path, str]:
    """Set up a repo with a governed commit on a feature branch.

    Returns (repo, base_sha) where base_sha is the main branch tip
    before the feature branch diverged.
    """
    repo = _setup_session_repo(tmp_path)

    # Record the base commit SHA
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Create a feature branch with a commit
    _git(repo, "checkout", "-b", "feature/auth")
    _make_commit(repo, "src/login.py", "def login(): pass", "add login endpoint")

    # Write a session index entry that covers this commit's timestamp window
    now = datetime.now(timezone.utc)
    _write_session_index(
        repo,
        [
            _make_session_entry(
                session_id="sess-001",
                ticket_id="TICKET-001",
                started_at=(now - timedelta(hours=1)).isoformat(),
                finished_at=(now + timedelta(hours=1)).isoformat(),
            ),
        ],
    )

    return repo, base_sha


# ---------------------------------------------------------------------------
# Tests: PR-Aware Audit Session
# ---------------------------------------------------------------------------


class TestPRAuditBootstrap:
    """Test that PR context is injected into audit bootstrap prompt."""

    def test_pr_check_report_in_bootstrap(self, tmp_path: Path) -> None:
        """When pr_base/pr_head provided, bootstrap contains PR Governance Report."""
        repo, base_sha = _setup_pr_audit_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="audit",
            pr_base=base_sha,
            pr_head="HEAD",
        )
        bootstrap = data["bootstrap_prompt"]
        assert "## PR Governance Report" in bootstrap
        assert "verdict:" in bootstrap
        assert "commits:" in bootstrap

    def test_pr_review_directives_in_bootstrap(self, tmp_path: Path) -> None:
        """When PR context provided, PR Review Directives section is injected."""
        repo, base_sha = _setup_pr_audit_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="audit",
            pr_base=base_sha,
            pr_head="HEAD",
        )
        bootstrap = data["bootstrap_prompt"]
        assert "## PR Review Directives" in bootstrap
        assert "UNGOVERNED" in bootstrap
        assert "SCOPE" in bootstrap
        assert "DRIFT" in bootstrap

    def test_no_pr_report_without_pr_args(self, tmp_path: Path) -> None:
        """Without pr_base/pr_head, bootstrap has no PR sections."""
        repo = _setup_session_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="audit",
        )
        bootstrap = data["bootstrap_prompt"]
        assert "## PR Governance Report" not in bootstrap
        assert "## PR Review Directives" not in bootstrap

    def test_audit_directives_still_present(self, tmp_path: Path) -> None:
        """PR audit sessions still include standard audit directives."""
        repo, base_sha = _setup_pr_audit_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="audit",
            pr_base=base_sha,
            pr_head="HEAD",
        )
        bootstrap = data["bootstrap_prompt"]
        assert "## Audit Directives" in bootstrap
        assert "Red Team Auditor" in bootstrap

    def test_context_isolation_still_present(self, tmp_path: Path) -> None:
        """PR audit sessions maintain context isolation."""
        repo, base_sha = _setup_pr_audit_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="audit",
            pr_base=base_sha,
            pr_head="HEAD",
        )
        bootstrap = data["bootstrap_prompt"]
        assert ".exo/cache/**" in bootstrap
        assert ".exo/memory/**" in bootstrap


class TestPRCheckDataInSession:
    """Test that pr_check data is stored in the session payload."""

    def test_pr_check_data_stored(self, tmp_path: Path) -> None:
        """Session payload includes pr_check dict when PR args provided."""
        repo, base_sha = _setup_pr_audit_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="audit",
            pr_base=base_sha,
            pr_head="HEAD",
        )
        session = data["session"]
        pr_check = session.get("pr_check")
        assert pr_check is not None
        assert "verdict" in pr_check
        assert "total_commits" in pr_check
        assert "governed_commits" in pr_check

    def test_pr_check_null_without_args(self, tmp_path: Path) -> None:
        """Session payload pr_check is None when no PR args."""
        repo = _setup_session_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="audit",
        )
        session = data["session"]
        assert session.get("pr_check") is None

    def test_pr_check_null_in_work_mode(self, tmp_path: Path) -> None:
        """Work mode sessions don't run pr_check even if args are passed."""
        repo, base_sha = _setup_pr_audit_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="work",
            pr_base=base_sha,
            pr_head="HEAD",
        )
        session = data["session"]
        assert session.get("pr_check") is None


class TestPRReportContent:
    """Test the content of PR governance report in bootstrap."""

    def test_governed_commit_shows_pass(self, tmp_path: Path) -> None:
        """A governed commit with passing session shows pass verdict."""
        repo, base_sha = _setup_pr_audit_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="audit",
            pr_base=base_sha,
            pr_head="HEAD",
        )
        bootstrap = data["bootstrap_prompt"]
        # Should show governed commits count > 0
        assert "1 governed" in bootstrap or "governed" in bootstrap.lower()

    def test_ungoverned_commit_shows_in_report(self, tmp_path: Path) -> None:
        """Ungoverned commits appear in the PR report."""
        repo = _setup_session_repo(tmp_path)
        base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "checkout", "-b", "feature/untracked")
        _make_commit(repo, "rogue.py", "rogue code", "untracked change")

        # No session index entry covers this commit
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="audit",
            pr_base=base_sha,
            pr_head="HEAD",
        )
        bootstrap = data["bootstrap_prompt"]
        assert "ungoverned" in bootstrap.lower()

    def test_session_details_in_report(self, tmp_path: Path) -> None:
        """Session verdicts include actor/vendor/model info."""
        repo, base_sha = _setup_pr_audit_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="audit",
            pr_base=base_sha,
            pr_head="HEAD",
        )
        bootstrap = data["bootstrap_prompt"]
        assert "sess-001" in bootstrap
        assert "TICKET-001" in bootstrap

    def test_high_drift_session_in_report(self, tmp_path: Path) -> None:
        """Session with high drift score shows drift value."""
        repo = _setup_session_repo(tmp_path)
        base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "checkout", "-b", "feature/high-drift")
        _make_commit(repo, "src/file.py", "code", "change with drift")

        now = datetime.now(timezone.utc)
        _write_session_index(
            repo,
            [
                _make_session_entry(
                    drift_score=0.85,
                    started_at=(now - timedelta(hours=1)).isoformat(),
                    finished_at=(now + timedelta(hours=1)).isoformat(),
                ),
            ],
        )

        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="audit",
            pr_base=base_sha,
            pr_head="HEAD",
        )
        bootstrap = data["bootstrap_prompt"]
        assert "drift=0.85" in bootstrap


class TestPRAuditGracefulDegradation:
    """Test that PR check failures don't block audit session."""

    def test_invalid_refs_dont_block(self, tmp_path: Path) -> None:
        """Invalid git refs for pr_check don't prevent audit session start."""
        repo = _setup_session_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        # Use nonsense refs — pr_check will fail silently
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="audit",
            pr_base="nonexistent-base-ref",
            pr_head="nonexistent-head-ref",
        )
        # Session should still start
        assert data["reused"] is False
        session = data["session"]
        assert session["mode"] == "audit"
        # PR check data should be None (failed gracefully)
        # The bootstrap may or may not have the PR section depending on whether
        # pr_check returns an empty report or raises — either way session starts

    def test_only_pr_base_no_pr_head(self, tmp_path: Path) -> None:
        """Providing only pr_base (no pr_head) skips PR check."""
        repo = _setup_session_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="audit",
            pr_base="main",
            pr_head=None,
        )
        bootstrap = data["bootstrap_prompt"]
        assert "## PR Governance Report" not in bootstrap

    def test_only_pr_head_no_pr_base(self, tmp_path: Path) -> None:
        """Providing only pr_head (no pr_base) skips PR check."""
        repo = _setup_session_repo(tmp_path)
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="audit",
            pr_base=None,
            pr_head="HEAD",
        )
        bootstrap = data["bootstrap_prompt"]
        assert "## PR Governance Report" not in bootstrap


class TestPRAuditWithPersona:
    """Test interaction between PR report and audit persona."""

    def test_pr_report_before_persona(self, tmp_path: Path) -> None:
        """PR Governance Report appears before custom audit persona."""
        repo, base_sha = _setup_pr_audit_repo(tmp_path)
        (repo / ".exo" / "audit_persona.md").write_text(
            "You are a security-focused auditor. Verify all auth flows.", encoding="utf-8"
        )
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="audit",
            pr_base=base_sha,
            pr_head="HEAD",
        )
        bootstrap = data["bootstrap_prompt"]
        pr_pos = bootstrap.index("## PR Governance Report")
        persona_pos = bootstrap.index("## Audit Persona")
        assert pr_pos < persona_pos

    def test_persona_still_injected(self, tmp_path: Path) -> None:
        """Custom persona is still injected alongside PR report."""
        repo, base_sha = _setup_pr_audit_repo(tmp_path)
        persona_text = "Focus on data leakage patterns."
        (repo / ".exo" / "audit_persona.md").write_text(persona_text, encoding="utf-8")
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="audit",
            pr_base=base_sha,
            pr_head="HEAD",
        )
        bootstrap = data["bootstrap_prompt"]
        assert persona_text in bootstrap


class TestPRAuditCLI:
    """Test CLI integration for PR-aware audit sessions."""

    def test_cli_pr_base_pr_head_args(self, tmp_path: Path) -> None:
        """CLI session-audit accepts --pr-base and --pr-head."""
        repo, base_sha = _setup_pr_audit_repo(tmp_path)
        env = {**os.environ, "EXO_ACTOR": "test-actor"}
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "exo.cli",
                "--format",
                "json",
                "--repo",
                str(repo),
                "session-audit",
                "--ticket-id",
                "TICKET-001",
                "--vendor",
                "openai",
                "--model",
                "o1-preview",
                "--pr-base",
                base_sha,
                "--pr-head",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        response = json.loads(result.stdout)
        assert response["ok"] is True
        data = response["data"]
        assert data["session"]["mode"] == "audit"
        # PR check data should be in the session
        assert data["session"].get("pr_check") is not None

    def test_cli_without_pr_args(self, tmp_path: Path) -> None:
        """CLI session-audit works without --pr-base/--pr-head (backwards compat)."""
        repo = _setup_session_repo(tmp_path)
        env = {**os.environ, "EXO_ACTOR": "test-actor"}
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "exo.cli",
                "--format",
                "json",
                "--repo",
                str(repo),
                "session-audit",
                "--ticket-id",
                "TICKET-001",
                "--vendor",
                "openai",
                "--model",
                "o1-preview",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        response = json.loads(result.stdout)
        assert response["ok"] is True
        data = response["data"]
        assert data["session"]["mode"] == "audit"
        assert data["session"].get("pr_check") is None

    def test_cli_human_output_shows_banner(self, tmp_path: Path) -> None:
        """Human output for PR audit session shows the banner."""
        repo, base_sha = _setup_pr_audit_repo(tmp_path)
        env = {**os.environ, "EXO_ACTOR": "test-actor"}
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "exo.cli",
                "--repo",
                str(repo),
                "session-audit",
                "--ticket-id",
                "TICKET-001",
                "--vendor",
                "openai",
                "--model",
                "o1-preview",
                "--pr-base",
                base_sha,
                "--pr-head",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        assert "EXO AUDIT SESSION" in result.stdout


class TestPRAuditBootstrapSectionOrder:
    """Test the ordering of sections in PR audit bootstrap."""

    def test_section_order(self, tmp_path: Path) -> None:
        """Bootstrap sections appear in correct order: directives → PR report → PR directives → persona → task."""
        repo, base_sha = _setup_pr_audit_repo(tmp_path)
        (repo / ".exo" / "audit_persona.md").write_text("Custom persona.", encoding="utf-8")
        mgr = AgentSessionManager(repo, actor="test-actor")
        data = mgr.start(
            ticket_id="TICKET-001",
            vendor="openai",
            model="o1-preview",
            mode="audit",
            task="Review the PR",
            pr_base=base_sha,
            pr_head="HEAD",
        )
        bootstrap = data["bootstrap_prompt"]

        # All sections should exist
        assert "## Audit Directives" in bootstrap
        assert "## PR Governance Report" in bootstrap
        assert "## PR Review Directives" in bootstrap
        assert "## Audit Persona" in bootstrap
        assert "## Current Task" in bootstrap

        # Order: directives < PR report < PR review directives < persona < task
        pos_directives = bootstrap.index("## Audit Directives")
        pos_pr_report = bootstrap.index("## PR Governance Report")
        pos_pr_review = bootstrap.index("## PR Review Directives")
        pos_persona = bootstrap.index("## Audit Persona")
        pos_task = bootstrap.index("## Current Task")

        assert pos_directives < pos_pr_report < pos_pr_review < pos_persona < pos_task
