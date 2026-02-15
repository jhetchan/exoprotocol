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
from exo.orchestrator.session import AgentSessionManager, SESSION_CACHE_DIR, SESSION_INDEX_PATH
from exo.stdlib.hooks import (
    HOOK_ACTOR,
    HOOK_VENDOR,
    generate_hook_config,
    handle_session_end,
    handle_session_start,
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
        settings = json.loads(
            (tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
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
