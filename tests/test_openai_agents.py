"""Tests for OpenAI Agents SDK integration (ExoRunHooks).

Since we don't depend on the openai-agents package in tests, we test
the ExoRunHooks class directly using asyncio and mock agent/context objects.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from exo.integrations.openai_agents import ExoRunHooks
from exo.kernel import governance as governance_mod
from exo.kernel import tickets


def _policy_block(rule: dict[str, Any]) -> str:
    return f"\n```yaml exo-policy\n{json.dumps(rule)}\n```\n"


def _bootstrap_repo(tmp_path: Path) -> Path:
    """Bootstrap repo with constitution + governance for testing."""
    repo = tmp_path
    exo_dir = repo / ".exo"
    exo_dir.mkdir(parents=True, exist_ok=True)

    deny_rule = {
        "id": "RULE-SEC-001",
        "type": "filesystem_deny",
        "patterns": ["**/.env*"],
        "actions": ["read", "write"],
        "message": "Secret deny",
    }
    lock_rule = {
        "id": "RULE-LOCK-001",
        "type": "require_lock",
        "message": "Lock required",
    }
    constitution = "# Constitution\n" + _policy_block(deny_rule) + _policy_block(lock_rule)
    (exo_dir / "CONSTITUTION.md").write_text(constitution, encoding="utf-8")
    governance_mod.compile_constitution(repo)
    return repo


def _create_ticket_with_lock(repo: Path, ticket_id: str, owner: str = "agent:openai") -> None:
    ticket = {
        "id": ticket_id,
        "title": "Test",
        "intent": "Test",
        "priority": 2,
        "labels": [],
        "type": "feature",
        "status": "todo",
    }
    tickets.save_ticket(repo, ticket)
    tickets.acquire_lock(repo, ticket_id, owner=owner, role="developer")


class MockAgent:
    """Minimal mock for OpenAI Agents SDK Agent."""

    def __init__(self, name: str = "test-agent") -> None:
        self.name = name


class MockContext:
    """Minimal mock for OpenAI Agents SDK RunContext."""

    pass


class MockTool:
    """Minimal mock for a tool."""

    def __init__(self, name: str = "test-tool") -> None:
        self.name = name


# ── Construction ────────────────────────────────────────────────


class TestExoRunHooksInit:
    def test_default_values(self) -> None:
        hooks = ExoRunHooks()
        assert hooks.actor == "agent:openai"
        assert hooks.vendor == "openai"
        assert hooks.model == "unknown"
        assert hooks.get_session_id() == ""

    def test_custom_values(self) -> None:
        hooks = ExoRunHooks(repo="/tmp", ticket_id="TKT-1", actor="agent:x", vendor="x", model="gpt-4")
        assert hooks.ticket_id == "TKT-1"
        assert hooks.actor == "agent:x"
        assert hooks.vendor == "x"
        assert hooks.model == "gpt-4"

    def test_importable_without_agents_sdk(self) -> None:
        """Module imports cleanly without openai-agents installed."""
        from exo.integrations import openai_agents  # noqa: F401


# ── Agent Lifecycle ─────────────────────────────────────────────


class TestExoRunHooksLifecycle:
    def test_start_creates_session(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TKT-1", owner="agent:openai")
        hooks = ExoRunHooks(repo=repo, actor="agent:openai")

        asyncio.run(hooks.on_agent_start(MockContext(), MockAgent()))
        assert hooks.get_session_id() != ""

    def test_start_reuses_active_session(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TKT-1", owner="agent:openai")

        from exo.orchestrator.session import AgentSessionManager

        mgr = AgentSessionManager(repo, actor="agent:openai")
        result = mgr.start(ticket_id="TKT-1")
        existing_id = result["session"]["session_id"]

        hooks = ExoRunHooks(repo=repo, actor="agent:openai")
        asyncio.run(hooks.on_agent_start(MockContext(), MockAgent()))
        assert hooks.get_session_id() == existing_id
        assert hooks._started is False

    def test_end_finishes_session(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TKT-1", owner="agent:openai")
        hooks = ExoRunHooks(repo=repo, actor="agent:openai")

        async def _run() -> None:
            await hooks.on_agent_start(MockContext(), MockAgent())
            assert hooks.get_session_id() != ""
            await hooks.on_agent_end(MockContext(), MockAgent(), "done")

        asyncio.run(_run())

        from exo.orchestrator.session import AgentSessionManager

        mgr = AgentSessionManager(repo, actor="agent:openai")
        assert mgr.get_active() is None

    def test_end_no_session_is_noop(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        hooks = ExoRunHooks(repo=repo, actor="agent:openai")
        # Should not raise even with no session
        asyncio.run(hooks.on_agent_end(MockContext(), MockAgent(), "done"))

    def test_start_no_exo_dir_is_graceful(self, tmp_path: Path) -> None:
        hooks = ExoRunHooks(repo=tmp_path, actor="agent:openai")
        asyncio.run(hooks.on_agent_start(MockContext(), MockAgent()))
        assert hooks.get_session_id() == ""


# ── Tool Tracking ───────────────────────────────────────────────


class TestExoRunHooksToolTracking:
    def test_tool_start_records_call(self) -> None:
        hooks = ExoRunHooks()
        asyncio.run(hooks.on_tool_start(MockContext(), MockAgent(), MockTool("search")))
        calls = hooks.get_tool_calls()
        assert len(calls) == 1
        assert calls[0]["tool"] == "search"

    def test_multiple_tools_tracked(self) -> None:
        hooks = ExoRunHooks()

        async def _run() -> None:
            await hooks.on_tool_start(MockContext(), MockAgent(), MockTool("search"))
            await hooks.on_tool_start(MockContext(), MockAgent(), MockTool("fetch"))
            await hooks.on_tool_start(MockContext(), MockAgent(), MockTool("write"))

        asyncio.run(_run())
        assert len(hooks.get_tool_calls()) == 3

    def test_tool_end_is_noop(self) -> None:
        hooks = ExoRunHooks()
        asyncio.run(hooks.on_tool_end(MockContext(), MockAgent(), MockTool(), "result"))

    def test_tool_calls_in_summary(self, tmp_path: Path) -> None:
        """Finished session summary includes tool call count."""
        repo = _bootstrap_repo(tmp_path)
        _create_ticket_with_lock(repo, "TKT-1", owner="agent:openai")
        hooks = ExoRunHooks(repo=repo, actor="agent:openai")

        async def _run() -> None:
            await hooks.on_agent_start(MockContext(), MockAgent())
            await hooks.on_tool_start(MockContext(), MockAgent(), MockTool("a"))
            await hooks.on_tool_start(MockContext(), MockAgent(), MockTool("b"))
            await hooks.on_agent_end(MockContext(), MockAgent(), "done")

        asyncio.run(_run())


# ── Handoff Logging ─────────────────────────────────────────────


class TestExoRunHooksHandoff:
    def test_handoff_does_not_raise(self) -> None:
        hooks = ExoRunHooks()
        asyncio.run(hooks.on_handoff(MockContext(), MockAgent("target"), MockAgent("source")))

    def test_get_tool_calls_returns_copy(self) -> None:
        hooks = ExoRunHooks()
        asyncio.run(hooks.on_tool_start(MockContext(), MockAgent(), MockTool("x")))
        calls = hooks.get_tool_calls()
        calls.clear()
        assert len(hooks.get_tool_calls()) == 1  # original not affected
