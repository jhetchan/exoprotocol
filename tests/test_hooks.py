"""Tests for Claude Code hook integration (auto session-start/finish).

Covers:
- SessionStart hook: skip conditions, session creation, bootstrap injection, env vars
- SessionEnd hook: skip conditions, session finish, gentle close behavior
- Hook config generation and installation
- CLI entry point (main)
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

from exo.kernel import governance as governance_mod
from exo.kernel import tickets
from exo.orchestrator.session import SESSION_INDEX_PATH, AgentSessionManager
from exo.stdlib.hooks import (
    HOOK_ACTOR,
    HOOK_VENDOR,
    _auto_format_python,
    _budget_tracker_path,
    _hook_actor,
    _load_budget_tracker,
    _save_budget_tracker,
    _track_budget,
    generate_hook_config,
    generate_notification_config,
    generate_post_tool_config,
    generate_stop_config,
    handle_notification,
    handle_post_tool_use,
    handle_session_end,
    handle_session_start,
    handle_stop,
    install_all_hooks,
    install_enforce_hooks,
    install_hooks,
    main,
)


def _policy_block(rule: dict[str, Any]) -> str:
    return f"\n```yaml exo-policy\n{json.dumps(rule)}\n```\n"


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
    return repo


def _create_ticket_and_lock(repo: Path, ticket_id: str = "TICKET-001") -> dict[str, Any]:
    ticket_data = {
        "id": ticket_id,
        "title": f"Test ticket {ticket_id}",
        "intent": f"Test intent {ticket_id}",
        "status": "active",
        "priority": 3,
        "checks": [],
        "scope": {"allow": ["**"], "deny": []},
    }
    tickets.save_ticket(repo, ticket_data)
    tickets.acquire_lock(repo, ticket_id, owner=HOOK_ACTOR, role="developer")
    return ticket_data


# ── SessionStart Hook ───────────────────────────────────────────────


class TestHandleSessionStart:
    def test_skips_no_exo_dir(self, tmp_path: Path) -> None:
        result = handle_session_start({"cwd": str(tmp_path)})
        assert result["skipped"] is True
        assert result["reason"] == "no_exo_dir"

    def test_skips_no_lock(self, tmp_path: Path) -> None:
        _bootstrap_repo(tmp_path)
        result = handle_session_start({"cwd": str(tmp_path)})
        assert result["skipped"] is True
        assert result["reason"] == "no_lock"

    def test_starts_with_lock(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_and_lock(repo)
        result = handle_session_start({"cwd": str(repo), "model": "claude-opus-4-6"})
        assert result.get("started") is True or result.get("reused") is True
        assert result["session_id"]
        assert result["ticket_id"] == "TICKET-001"

    def test_returns_bootstrap_prompt(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_and_lock(repo)
        result = handle_session_start({"cwd": str(repo)})
        bootstrap = result.get("bootstrap_prompt", "")
        assert "Exo Agent Session Bootstrap" in bootstrap
        assert "TICKET-001" in bootstrap

    def test_writes_env_vars(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_and_lock(repo)
        env_file = tmp_path / "claude_env"
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
        result = handle_session_start({"cwd": str(repo)})
        assert not result.get("skipped")
        contents = env_file.read_text(encoding="utf-8")
        assert "EXO_SESSION_ID=" in contents
        assert "EXO_TICKET_ID=TICKET-001" in contents
        assert f"EXO_ACTOR={HOOK_ACTOR}" in contents

    def test_handles_reused_session(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_and_lock(repo)
        # Start session directly first
        manager = AgentSessionManager(repo, actor=HOOK_ACTOR)
        manager.start(vendor=HOOK_VENDOR, model="test-model")
        # Hook call should reuse
        result = handle_session_start({"cwd": str(repo)})
        assert result.get("reused") is True
        assert result["bootstrap_prompt"]  # still has bootstrap from disk

    def test_passes_model_from_input(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_and_lock(repo)
        result = handle_session_start({"cwd": str(repo), "model": "claude-opus-4-6"})
        assert not result.get("skipped")
        # Verify the active session has the model
        manager = AgentSessionManager(repo, actor=HOOK_ACTOR)
        active = manager.get_active()
        assert active is not None
        assert active["model"] == "claude-opus-4-6"


# ── SessionEnd Hook ─────────────────────────────────────────────────


class TestHandleSessionEnd:
    def test_skips_no_exo_dir(self, tmp_path: Path) -> None:
        result = handle_session_end({"cwd": str(tmp_path)})
        assert result["skipped"] is True
        assert result["reason"] == "no_exo_dir"

    def test_skips_no_active_session(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = handle_session_end({"cwd": str(repo)})
        assert result["skipped"] is True
        assert result["reason"] == "no_active_session"

    def test_finishes_session(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_and_lock(repo)
        handle_session_start({"cwd": str(repo)})
        result = handle_session_end({"cwd": str(repo), "reason": "prompt_input_exit"})
        assert result["finished"] is True
        assert result["session_id"]
        # Active session file should be removed
        manager = AgentSessionManager(repo, actor=HOOK_ACTOR)
        assert manager.get_active() is None

    def test_keeps_status_preserves_lock(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_and_lock(repo)
        handle_session_start({"cwd": str(repo)})
        handle_session_end({"cwd": str(repo)})
        # Ticket status unchanged (set_status="keep" means no change)
        ticket = tickets.load_ticket(repo, "TICKET-001")
        assert ticket["status"] == "active"
        # Lock preserved
        lock = tickets.load_lock(repo)
        assert lock is not None
        assert lock["ticket_id"] == "TICKET-001"

    def test_auto_close_summary(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_and_lock(repo)
        handle_session_start({"cwd": str(repo)})
        handle_session_end({"cwd": str(repo), "reason": "logout"})
        # Read the session index to find the memento
        index_path = repo / SESSION_INDEX_PATH
        assert index_path.exists()
        lines = index_path.read_text(encoding="utf-8").strip().split("\n")
        last_entry = json.loads(lines[-1])
        assert "Auto-closed by Claude Code SessionEnd hook" in last_entry["summary"]
        assert "logout" in last_entry["summary"]


# ── Config Generation ───────────────────────────────────────────────


class TestGenerateHookConfig:
    def test_has_session_start_and_end(self) -> None:
        config = generate_hook_config()
        assert "hooks" in config
        assert "SessionStart" in config["hooks"]
        assert "SessionEnd" in config["hooks"]

    def test_start_command(self) -> None:
        config = generate_hook_config()
        start_hooks = config["hooks"]["SessionStart"][0]["hooks"]
        assert start_hooks[0]["command"] == "python3 -m exo.stdlib.hooks session-start"

    def test_start_matcher(self) -> None:
        config = generate_hook_config()
        assert config["hooks"]["SessionStart"][0]["matcher"] == "startup"


# ── Install Hooks ───────────────────────────────────────────────────


class TestInstallHooks:
    def test_creates_settings_file(self, tmp_path: Path) -> None:
        result = install_hooks(tmp_path)
        assert result["installed"] is True
        settings_path = tmp_path / ".claude" / "settings.json"
        assert settings_path.exists()
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert "hooks" in settings
        assert "SessionStart" in settings["hooks"]

    def test_merges_existing(self, tmp_path: Path) -> None:
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir(parents=True)
        (settings_dir / "settings.json").write_text(
            json.dumps({"customKey": "preserved", "otherStuff": 42}),
            encoding="utf-8",
        )
        install_hooks(tmp_path)
        settings = json.loads((settings_dir / "settings.json").read_text(encoding="utf-8"))
        assert settings["customKey"] == "preserved"
        assert settings["otherStuff"] == 42
        assert "SessionStart" in settings["hooks"]

    def test_dry_run(self, tmp_path: Path) -> None:
        result = install_hooks(tmp_path, dry_run=True)
        assert result["dry_run"] is True
        assert result["installed"] is False
        assert not (tmp_path / ".claude" / "settings.json").exists()

    def test_idempotent(self, tmp_path: Path) -> None:
        install_hooks(tmp_path)
        install_hooks(tmp_path)
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
        # Only one entry per event
        assert len(settings["hooks"]["SessionStart"]) == 1
        assert len(settings["hooks"]["SessionEnd"]) == 1


# ── Main Entry Point ────────────────────────────────────────────────


class TestMain:
    def test_session_start_outputs_bootstrap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_and_lock(repo)
        hook_input = json.dumps({"cwd": str(repo), "model": "test-model"})
        monkeypatch.setattr("sys.stdin", io.StringIO(hook_input))
        exit_code = main(["session-start"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Exo Agent Session Bootstrap" in captured.out

    def test_unknown_command_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
        assert main(["unknown-thing"]) == 0

    def test_empty_stdin_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        assert main(["session-start"]) == 0


# ── Tool Auto-Discovery ──────────────────────────────────────────


class TestDiscoverTools:
    """Tests for discover_tools() auto-discovery via importlib.metadata."""

    def test_returns_list(self) -> None:
        from exo.stdlib.hooks import discover_tools

        tools = discover_tools()
        assert isinstance(tools, list)
        assert len(tools) >= 2  # at least core CLI + hooks

    def test_core_cli_always_present(self) -> None:
        from exo.stdlib.hooks import discover_tools

        tools = discover_tools()
        names = [t["name"] for t in tools]
        assert "exo" in names

    def test_claude_hooks_always_present(self) -> None:
        from exo.stdlib.hooks import discover_tools

        tools = discover_tools()
        names = [t["name"] for t in tools]
        assert "claude-hooks" in names

    def test_tool_descriptor_shape(self) -> None:
        from exo.stdlib.hooks import discover_tools

        tools = discover_tools()
        for tool in tools:
            assert "name" in tool
            assert "type" in tool
            assert "module" in tool
            assert "description" in tool

    def test_mcp_present_when_installed(self) -> None:
        """MCP tool appears when mcp package is installed."""
        from exo.stdlib.hooks import discover_tools

        tools = discover_tools()
        names = [t["name"] for t in tools]
        # mcp is installed in our test venv
        try:
            import importlib.metadata

            importlib.metadata.distribution("mcp")
            assert "exo-mcp" in names
        except importlib.metadata.PackageNotFoundError:
            assert "exo-mcp" not in names

    def test_tool_types_valid(self) -> None:
        from exo.stdlib.hooks import discover_tools

        tools = discover_tools()
        valid_types = {"cli", "mcp", "hooks", "integration"}
        for tool in tools:
            assert tool["type"] in valid_types


# ── Git Pre-Commit Hook ─────────────────────────────────────────


class TestInstallGitHook:
    def test_requires_git_dir(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import install_git_hook

        result = install_git_hook(tmp_path)
        assert result["installed"] is False
        assert result["error"] == "no_git_dir"

    def test_installs_hook(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import install_git_hook

        (tmp_path / ".git").mkdir()
        result = install_git_hook(tmp_path)
        assert result["installed"] is True
        hook = Path(result["path"])
        assert hook.exists()
        assert "ExoProtocol pre-commit hook" in hook.read_text(encoding="utf-8")

    def test_hook_is_executable(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import install_git_hook

        (tmp_path / ".git").mkdir()
        install_git_hook(tmp_path)
        hook = tmp_path / ".git" / "hooks" / "pre-commit"
        assert os.access(hook, os.X_OK)

    def test_hook_runs_exo_check(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import install_git_hook

        (tmp_path / ".git").mkdir()
        install_git_hook(tmp_path)
        hook = tmp_path / ".git" / "hooks" / "pre-commit"
        content = hook.read_text(encoding="utf-8")
        assert "--format human" in content
        assert "check" in content

    def test_dry_run(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import install_git_hook

        (tmp_path / ".git").mkdir()
        result = install_git_hook(tmp_path, dry_run=True)
        assert result["dry_run"] is True
        assert result["installed"] is False
        assert not (tmp_path / ".git" / "hooks" / "pre-commit").exists()

    def test_backs_up_existing_non_exo_hook(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import install_git_hook

        git_hooks = tmp_path / ".git" / "hooks"
        git_hooks.mkdir(parents=True)
        existing_hook = git_hooks / "pre-commit"
        existing_hook.write_text("#!/bin/bash\necho custom hook\n", encoding="utf-8")

        result = install_git_hook(tmp_path)
        assert result["installed"] is True
        assert result["backed_up"]
        backup = Path(result["backed_up"])
        assert backup.exists()
        assert "custom hook" in backup.read_text(encoding="utf-8")

    def test_no_backup_for_exo_hook(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import install_git_hook

        git_hooks = tmp_path / ".git" / "hooks"
        git_hooks.mkdir(parents=True)
        # Install once
        install_git_hook(tmp_path)
        # Install again (idempotent, no backup needed)
        result = install_git_hook(tmp_path)
        assert result["installed"] is True
        assert result["backed_up"] == ""

    def test_idempotent(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import install_git_hook

        (tmp_path / ".git").mkdir()
        install_git_hook(tmp_path)
        install_git_hook(tmp_path)
        hook = tmp_path / ".git" / "hooks" / "pre-commit"
        content = hook.read_text(encoding="utf-8")
        assert content.count("ExoProtocol pre-commit hook") == 1

    def test_creates_hooks_dir(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import install_git_hook

        (tmp_path / ".git").mkdir()
        result = install_git_hook(tmp_path)
        assert result["installed"] is True
        assert (tmp_path / ".git" / "hooks").is_dir()


# ── Claude Code PreToolUse Enforce Hook ──────────────────────────


class TestInstallEnforceHooks:
    def test_generates_pretooluse_config(self) -> None:
        from exo.stdlib.hooks import generate_enforce_config

        config = generate_enforce_config()
        assert "hooks" in config
        assert "PreToolUse" in config["hooks"]
        entries = config["hooks"]["PreToolUse"]
        assert len(entries) == 1
        assert entries[0]["matcher"] == "Bash"

    def test_enforce_command_contains_exo_check(self) -> None:
        from exo.stdlib.hooks import generate_enforce_config

        config = generate_enforce_config()
        cmd = config["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert "exo check" in cmd
        assert "git commit" in cmd
        assert "git push" in cmd

    def test_installs_enforce_hooks(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import install_enforce_hooks

        result = install_enforce_hooks(tmp_path)
        assert result["installed"] is True
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert "PreToolUse" in settings["hooks"]

    def test_preserves_existing_session_hooks(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import install_enforce_hooks

        # Install session hooks first
        install_hooks(tmp_path)
        # Then install enforce hooks
        install_enforce_hooks(tmp_path)
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert "SessionStart" in settings["hooks"]
        assert "SessionEnd" in settings["hooks"]
        assert "PreToolUse" in settings["hooks"]

    def test_preserves_existing_settings(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import install_enforce_hooks

        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir(parents=True)
        (settings_dir / "settings.json").write_text(json.dumps({"customKey": "kept"}), encoding="utf-8")
        install_enforce_hooks(tmp_path)
        settings = json.loads((settings_dir / "settings.json").read_text(encoding="utf-8"))
        assert settings["customKey"] == "kept"

    def test_dry_run(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import install_enforce_hooks

        result = install_enforce_hooks(tmp_path, dry_run=True)
        assert result["dry_run"] is True
        assert result["installed"] is False
        assert not (tmp_path / ".claude" / "settings.json").exists()

    def test_idempotent(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import install_enforce_hooks

        install_enforce_hooks(tmp_path)
        install_enforce_hooks(tmp_path)
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
        # 2 entries: Bash (git commit/push gating) + Write|Edit (scope enforcement)
        assert len(settings["hooks"]["PreToolUse"]) == 2

    def test_enforce_config_timeout(self) -> None:
        from exo.stdlib.hooks import generate_enforce_config

        config = generate_enforce_config()
        hook = config["hooks"]["PreToolUse"][0]["hooks"][0]
        assert hook["timeout"] == 30


# ── Governed Push (exo push) ─────────────────────────────────────


class TestGovernedPush:
    def _setup_repo(self, tmp_path: Path) -> Path:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_and_lock(repo)
        return repo

    def test_push_blocks_on_failed_checks(self, tmp_path: Path) -> None:
        """Push should be blocked when checks fail."""
        from exo.stdlib.engine import KernelEngine

        repo = self._setup_repo(tmp_path)
        # Add a check that will fail
        ticket = tickets.load_ticket(repo, "TICKET-001")
        ticket["checks"] = ["false"]  # always-fail command
        tickets.save_ticket(repo, ticket)
        # Allowlist it
        config_path = repo / ".exo" / "config.yaml"
        config_path.write_text("checks_allowlist:\n  - 'false'\n", encoding="utf-8")

        engine = KernelEngine(str(repo))
        result = engine.push()
        assert result.get("blocked") is True
        data = result.get("data", {})
        assert data.get("pushed") is False
        assert data.get("reason") == "checks_failed"

    def test_push_proceeds_on_passed_checks(self, tmp_path: Path) -> None:
        """Push should proceed when checks pass (git push may still fail without remote)."""
        from exo.stdlib.engine import KernelEngine

        repo = self._setup_repo(tmp_path)
        # No checks = auto-pass
        engine = KernelEngine(str(repo))
        result = engine.push()
        # Checks passed, but git push may fail (no remote in test)
        data = result.get("data", {})
        assert data.get("checks", {}).get("passed") is True

    def test_push_returns_check_results(self, tmp_path: Path) -> None:
        """Push result should include check results."""
        from exo.stdlib.engine import KernelEngine

        repo = self._setup_repo(tmp_path)
        engine = KernelEngine(str(repo))
        result = engine.push()
        data = result.get("data", {})
        assert "checks" in data
        assert "passed" in data["checks"]

    def test_push_with_ticket_id(self, tmp_path: Path) -> None:
        """Push should accept explicit ticket_id."""
        from exo.stdlib.engine import KernelEngine

        repo = self._setup_repo(tmp_path)
        engine = KernelEngine(str(repo))
        result = engine.push("TICKET-001")
        data = result.get("data", {})
        assert "checks" in data


# ── Notification Hook: Audit Trail Logging ──────────────────────


class TestHandleNotification:
    def test_skips_no_exo_dir(self, tmp_path: Path) -> None:
        result = handle_notification({"cwd": str(tmp_path)})
        assert result["skipped"] is True
        assert result["reason"] == "no_exo_dir"

    def test_logs_permission_prompt(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        hook_input = {
            "cwd": str(repo),
            "session_id": "abc123",
            "notification_type": "permission_prompt",
            "message": "Claude needs permission to use Bash",
            "title": "Permission needed",
        }
        result = handle_notification(hook_input)
        assert result["logged"] is True
        assert result["notification_type"] == "permission_prompt"
        # Check audit file was written
        audit_file = repo / ".exo" / "audit" / "notifications.jsonl"
        assert audit_file.exists()
        entry = json.loads(audit_file.read_text(encoding="utf-8").strip())
        assert entry["notification_type"] == "permission_prompt"
        assert entry["message"] == "Claude needs permission to use Bash"
        assert entry["session_id"] == "abc123"
        assert "timestamp" in entry

    def test_logs_idle_prompt(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        hook_input = {
            "cwd": str(repo),
            "session_id": "sess-1",
            "notification_type": "idle_prompt",
            "message": "Claude has been idle",
        }
        result = handle_notification(hook_input)
        assert result["logged"] is True
        assert result["notification_type"] == "idle_prompt"

    def test_appends_multiple_entries(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        for i in range(3):
            handle_notification(
                {
                    "cwd": str(repo),
                    "session_id": f"sess-{i}",
                    "notification_type": "permission_prompt",
                    "message": f"Permission {i}",
                }
            )
        audit_file = repo / ".exo" / "audit" / "notifications.jsonl"
        lines = audit_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3
        for i, line in enumerate(lines):
            entry = json.loads(line)
            assert entry["session_id"] == f"sess-{i}"

    def test_handles_missing_fields_gracefully(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = handle_notification({"cwd": str(repo)})
        assert result["logged"] is True
        audit_file = repo / ".exo" / "audit" / "notifications.jsonl"
        entry = json.loads(audit_file.read_text(encoding="utf-8").strip())
        assert entry["notification_type"] == ""
        assert entry["message"] == ""

    def test_never_crashes(self, tmp_path: Path) -> None:
        # Even with completely invalid input, should not raise
        result = handle_notification({"cwd": "/nonexistent/path/that/does/not/exist"})
        assert result["skipped"] is True

    def test_includes_title_in_entry(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        handle_notification(
            {
                "cwd": str(repo),
                "notification_type": "auth_success",
                "title": "Auth OK",
                "message": "Authenticated",
            }
        )
        audit_file = repo / ".exo" / "audit" / "notifications.jsonl"
        entry = json.loads(audit_file.read_text(encoding="utf-8").strip())
        assert entry["title"] == "Auth OK"


class TestGenerateNotificationConfig:
    def test_has_notification_key(self) -> None:
        config = generate_notification_config()
        assert "hooks" in config
        assert "Notification" in config["hooks"]

    def test_command_invokes_hooks_module(self) -> None:
        config = generate_notification_config()
        entries = config["hooks"]["Notification"]
        assert len(entries) >= 1
        cmd = entries[0]["hooks"][0]["command"]
        assert "exo.stdlib.hooks" in cmd
        assert "notification" in cmd

    def test_timeout_set(self) -> None:
        config = generate_notification_config()
        hook = config["hooks"]["Notification"][0]["hooks"][0]
        assert "timeout" in hook


class TestInstallHooksIncludesNotification:
    def test_notification_hook_installed(self, tmp_path: Path) -> None:
        install_hooks(tmp_path)
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert "Notification" in settings["hooks"]

    def test_notification_idempotent(self, tmp_path: Path) -> None:
        install_hooks(tmp_path)
        install_hooks(tmp_path)
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert len(settings["hooks"]["Notification"]) == 1


class TestMainNotification:
    def test_notification_command(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _bootstrap_repo(tmp_path)
        hook_input = json.dumps(
            {
                "cwd": str(repo),
                "session_id": "main-test",
                "notification_type": "permission_prompt",
                "message": "Test",
            }
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(hook_input))
        exit_code = main(["notification"])
        assert exit_code == 0
        audit_file = repo / ".exo" / "audit" / "notifications.jsonl"
        assert audit_file.exists()


# ── Stop Hook: Session Close Hygiene ─────────────────────────────


class TestHandleStop:
    def test_skips_no_exo_dir(self, tmp_path: Path) -> None:
        result = handle_stop({"cwd": str(tmp_path)})
        assert result["skipped"] is True
        assert result["reason"] == "no_exo_dir"

    def test_no_active_session(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = handle_stop({"cwd": str(repo)})
        assert result["has_active_session"] is False

    def test_warns_on_active_session(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_and_lock(repo)
        handle_session_start({"cwd": str(repo)})
        result = handle_stop({"cwd": str(repo)})
        assert result["has_active_session"] is True
        assert result["ticket_id"] == "TICKET-001"
        assert result["session_id"]
        assert "warning" in result
        assert "exo session-finish" in result["warning"]

    def test_warning_includes_session_and_ticket(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_and_lock(repo)
        handle_session_start({"cwd": str(repo)})
        result = handle_stop({"cwd": str(repo)})
        warning = result["warning"]
        assert result["session_id"] in warning
        assert "TICKET-001" in warning

    def test_does_not_close_session(self, tmp_path: Path) -> None:
        """Stop hook warns but does NOT auto-close the session."""
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_and_lock(repo)
        handle_session_start({"cwd": str(repo)})
        handle_stop({"cwd": str(repo)})
        # Session should still be active
        manager = AgentSessionManager(repo, actor=HOOK_ACTOR)
        assert manager.get_active() is not None

    def test_never_crashes(self, tmp_path: Path) -> None:
        result = handle_stop({"cwd": "/nonexistent/path/that/does/not/exist"})
        assert result["skipped"] is True


class TestGenerateStopConfig:
    def test_has_stop_key(self) -> None:
        config = generate_stop_config()
        assert "hooks" in config
        assert "Stop" in config["hooks"]

    def test_command_invokes_hooks_module(self) -> None:
        config = generate_stop_config()
        entries = config["hooks"]["Stop"]
        assert len(entries) >= 1
        cmd = entries[0]["hooks"][0]["command"]
        assert "exo.stdlib.hooks" in cmd
        assert "stop" in cmd

    def test_timeout_set(self) -> None:
        config = generate_stop_config()
        hook = config["hooks"]["Stop"][0]["hooks"][0]
        assert hook["timeout"] == 10


class TestInstallHooksIncludesStop:
    def test_stop_hook_installed(self, tmp_path: Path) -> None:
        install_hooks(tmp_path)
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert "Stop" in settings["hooks"]

    def test_stop_idempotent(self, tmp_path: Path) -> None:
        install_hooks(tmp_path)
        install_hooks(tmp_path)
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert len(settings["hooks"]["Stop"]) == 1


class TestMainStop:
    def test_stop_outputs_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_and_lock(repo)
        # Start a session first
        start_input = json.dumps({"cwd": str(repo), "model": "test-model"})
        monkeypatch.setattr("sys.stdin", io.StringIO(start_input))
        main(["session-start"])

        # Now run the stop hook
        stop_input = json.dumps({"cwd": str(repo)})
        monkeypatch.setattr("sys.stdin", io.StringIO(stop_input))
        exit_code = main(["stop"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "ExoProtocol" in captured.out
        assert "session-finish" in captured.out

    def test_stop_silent_when_no_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _bootstrap_repo(tmp_path)
        stop_input = json.dumps({"cwd": str(repo)})
        monkeypatch.setattr("sys.stdin", io.StringIO(stop_input))
        exit_code = main(["stop"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert captured.out == ""


# ── PostToolUse: Auto-Format + Budget Tracking ───────────────────


class TestAutoFormatPython:
    def test_skips_non_python(self) -> None:
        result = _auto_format_python("readme.md")
        assert result["formatted"] is False
        assert result["reason"] == "not_python"

    def test_formats_python_file(self, tmp_path: Path) -> None:
        py_file = tmp_path / "ugly.py"
        py_file.write_text("x=1\n", encoding="utf-8")
        result = _auto_format_python(str(py_file))
        assert result["formatted"] is True

    def test_handles_ruff_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*args: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError("ruff")

        monkeypatch.setattr("exo.stdlib.hooks.subprocess.run", fake_run)
        result = _auto_format_python("/tmp/test.py")
        assert result["formatted"] is False
        assert result["reason"] == "ruff_not_found"

    def test_handles_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        def fake_run(*args: Any, **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired("ruff", 15)

        monkeypatch.setattr("exo.stdlib.hooks.subprocess.run", fake_run)
        result = _auto_format_python("/tmp/test.py")
        assert result["formatted"] is False
        assert result["reason"] == "timeout"


class TestBudgetTracker:
    def test_load_missing_returns_empty(self, tmp_path: Path) -> None:
        tracker = _load_budget_tracker(tmp_path)
        assert tracker == {"files": [], "loc": 0, "session_id": ""}

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        data = {"files": ["a.py"], "loc": 42, "session_id": "SES-123"}
        _save_budget_tracker(tmp_path, data)
        loaded = _load_budget_tracker(tmp_path)
        assert loaded["files"] == ["a.py"]
        assert loaded["loc"] == 42
        assert loaded["session_id"] == "SES-123"

    def test_corrupt_json_returns_empty(self, tmp_path: Path) -> None:
        path = _budget_tracker_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("NOT JSON", encoding="utf-8")
        tracker = _load_budget_tracker(tmp_path)
        assert tracker == {"files": [], "loc": 0, "session_id": ""}


class TestTrackBudget:
    def _setup_session(self, repo: Path, ticket_id: str = "TKT-TEST-001") -> str:
        """Create a repo with active session and ticket, return session_id."""
        _bootstrap_repo(repo)
        # Create a ticket with small budgets
        ticket = {
            "id": ticket_id,
            "title": "Test budget",
            "intent": "test",
            "kind": "task",
            "priority": 3,
            "labels": [],
            "scope": {"allow": ["**"], "deny": []},
            "budgets": {"max_files_changed": 3, "max_loc_changed": 50},
            "type": "feature",
            "status": "in-progress",
        }
        tickets.save_ticket(repo, ticket)
        tickets.acquire_lock(repo, ticket_id, owner="agent:test")
        manager = AgentSessionManager(repo, actor="agent:test")
        result = manager.start(vendor="test", model="test-model")
        return str(result["session"]["session_id"])

    def test_tracks_new_file(self, tmp_path: Path) -> None:
        self._setup_session(tmp_path)
        result = _track_budget(tmp_path, str(tmp_path / "foo.py"), "line1\nline2\n")
        assert result["tracked"] is True
        assert result["files_used"] == 1
        assert result["max_files"] == 3
        assert result["loc_used"] == 2

    def test_warns_at_threshold(self, tmp_path: Path) -> None:
        self._setup_session(tmp_path)
        # Touch 3 files (budget is 3 → ratio = 1.0 → exceeded)
        _track_budget(tmp_path, str(tmp_path / "a.py"), "x\n")
        _track_budget(tmp_path, str(tmp_path / "b.py"), "x\n")
        result = _track_budget(tmp_path, str(tmp_path / "c.py"), "x\n")
        assert result.get("warnings")
        assert any("BUDGET EXCEEDED" in w or "Budget warning" in w for w in result["warnings"])

    def test_resets_on_new_session(self, tmp_path: Path) -> None:
        self._setup_session(tmp_path)
        _track_budget(tmp_path, str(tmp_path / "a.py"), "x\n")
        # Manually set a stale session_id in the tracker
        tracker = _load_budget_tracker(tmp_path)
        tracker["session_id"] = "SES-OLD"
        _save_budget_tracker(tmp_path, tracker)
        # Next track should reset
        result = _track_budget(tmp_path, str(tmp_path / "b.py"), "y\n")
        assert result["tracked"] is True
        assert result["files_used"] == 1  # reset, not 2

    def test_no_active_session_skips(self, tmp_path: Path) -> None:
        _bootstrap_repo(tmp_path)
        result = _track_budget(tmp_path, str(tmp_path / "a.py"), "x\n")
        assert result["tracked"] is False
        assert result["reason"] == "no_active_session"


class TestHandlePostToolUse:
    def test_skips_no_exo_dir(self, tmp_path: Path) -> None:
        result = handle_post_tool_use({"cwd": str(tmp_path)})
        assert result["skipped"] is True
        assert result["reason"] == "no_exo_dir"

    def test_skips_no_file_path(self, tmp_path: Path) -> None:
        _bootstrap_repo(tmp_path)
        result = handle_post_tool_use({"cwd": str(tmp_path), "input": {}})
        assert result["skipped"] is True
        assert result["reason"] == "no_file_path"

    def test_returns_format_and_budget(self, tmp_path: Path) -> None:
        _bootstrap_repo(tmp_path)
        result = handle_post_tool_use(
            {
                "cwd": str(tmp_path),
                "tool_name": "Write",
                "input": {"file_path": str(tmp_path / "test.py"), "content": "x = 1\n"},
            }
        )
        assert "format" in result
        assert "budget" in result


class TestGeneratePostToolConfig:
    def test_has_post_tool_use_key(self) -> None:
        config = generate_post_tool_config()
        assert "PostToolUse" in config["hooks"]

    def test_matcher_is_write_edit(self) -> None:
        config = generate_post_tool_config()
        assert config["hooks"]["PostToolUse"][0]["matcher"] == "Write|Edit"

    def test_command_invokes_hooks_module(self) -> None:
        config = generate_post_tool_config()
        cmd = config["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        assert "exo.stdlib.hooks" in cmd
        assert "post-tool" in cmd


class TestMainPostTool:
    def test_post_tool_exits_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _bootstrap_repo(tmp_path)
        hook_input = json.dumps(
            {
                "cwd": str(tmp_path),
                "tool_name": "Write",
                "input": {"file_path": str(tmp_path / "x.txt"), "content": "hi"},
            }
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(hook_input))
        assert main(["post-tool"]) == 0


# ── Instance-Unique Actor Identity ───────────────────────────────


class TestHookActor:
    def test_default_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EXO_INSTANCE_ID", raising=False)
        assert _hook_actor() == HOOK_ACTOR

    def test_unique_with_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EXO_INSTANCE_ID", "abc12345")
        result = _hook_actor()
        assert result == f"{HOOK_ACTOR}:abc12345"
        assert result != HOOK_ACTOR

    def test_session_start_generates_instance_id(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_and_lock(repo)
        monkeypatch.delenv("EXO_INSTANCE_ID", raising=False)
        env_file = tmp_path / "claude_env"
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
        result = handle_session_start({"cwd": str(repo), "model": "test"})
        assert not result.get("skipped")
        # Check EXO_INSTANCE_ID was written to env file
        env_content = env_file.read_text(encoding="utf-8")
        assert "EXO_INSTANCE_ID=" in env_content
        # Actor should be instance-unique
        assert HOOK_ACTOR in result["env_vars"]["EXO_ACTOR"]
        assert ":" in result["env_vars"]["EXO_ACTOR"].replace(HOOK_ACTOR, "")

    def test_parallel_instances_get_different_actors(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _bootstrap_repo(tmp_path)
        monkeypatch.delenv("EXO_INSTANCE_ID", raising=False)
        # Enable instance ID generation (requires CLAUDE_ENV_FILE)
        env_file = tmp_path / "claude_env"
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
        # First instance
        _create_ticket_and_lock(repo, "TKT-A")
        r1 = handle_session_start({"cwd": str(repo), "model": "test"})
        actor1 = r1["env_vars"]["EXO_ACTOR"]
        # Finish the first session
        mgr1 = AgentSessionManager(repo, actor=actor1)
        mgr1.finish(summary="done", ticket_id="TKT-A", skip_check=True, break_glass_reason="test")
        # Second instance with different ticket
        tickets.release_lock(repo)
        ticket2_data = {
            "id": "TKT-B",
            "title": "Test B",
            "intent": "test B",
            "status": "active",
            "priority": 3,
            "checks": [],
            "scope": {"allow": ["**"], "deny": []},
        }
        tickets.save_ticket(repo, ticket2_data)
        tickets.acquire_lock(repo, "TKT-B", owner=HOOK_ACTOR, role="developer")
        r2 = handle_session_start({"cwd": str(repo), "model": "test"})
        actor2 = r2["env_vars"]["EXO_ACTOR"]
        # Different instances should get different actors
        assert actor1 != actor2


# ── Install All Hooks ────────────────────────────────────────────


class TestInstallAllHooks:
    def test_installs_session_enforce_git(self, tmp_path: Path) -> None:
        # Create a git dir for git hook
        (tmp_path / ".git" / "hooks").mkdir(parents=True)
        result = install_all_hooks(tmp_path)
        assert not result["dry_run"]
        assert len(result["hooks"]) == 3
        targets = [h["target"] for h in result["hooks"]]
        assert "session" in targets
        assert "enforce" in targets
        assert "git" in targets

    def test_settings_has_all_hook_types(self, tmp_path: Path) -> None:
        (tmp_path / ".git" / "hooks").mkdir(parents=True)
        install_all_hooks(tmp_path)
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
        hooks = settings["hooks"]
        assert "SessionStart" in hooks
        assert "SessionEnd" in hooks
        assert "Stop" in hooks
        assert "Notification" in hooks
        assert "PreToolUse" in hooks
        assert "PostToolUse" in hooks

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        (tmp_path / ".git" / "hooks").mkdir(parents=True)
        result = install_all_hooks(tmp_path, dry_run=True)
        assert result["dry_run"]
        assert not (tmp_path / ".claude" / "settings.json").exists()


class TestInstallEnforceHooksComplete:
    def test_includes_pretooluse_bash_and_scope(self, tmp_path: Path) -> None:
        install_enforce_hooks(tmp_path)
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
        pre = settings["hooks"]["PreToolUse"]
        matchers = [e.get("matcher", "") for e in pre]
        assert "Bash" in matchers
        assert "Write|Edit" in matchers

    def test_includes_posttooluse(self, tmp_path: Path) -> None:
        install_enforce_hooks(tmp_path)
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert "PostToolUse" in settings["hooks"]
        post = settings["hooks"]["PostToolUse"]
        assert post[0]["matcher"] == "Write|Edit"

    def test_preserves_existing_session_hooks(self, tmp_path: Path) -> None:
        # Install session hooks first
        install_hooks(tmp_path)
        # Then install enforce hooks
        install_enforce_hooks(tmp_path)
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert "SessionStart" in settings["hooks"]
        assert "SessionEnd" in settings["hooks"]
        assert "PreToolUse" in settings["hooks"]
        assert "PostToolUse" in settings["hooks"]
