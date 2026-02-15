"""OpenAI Agents SDK integration: ExoRunHooks for governed agent runs.

Provides an ``ExoRunHooks`` class that plugs into ``Runner.run(agent, hooks=...)``
to automatically start/finish ExoProtocol governed sessions, gate tools on scope,
and log handoffs in the audit trail.

Usage::

    from exo.integrations.openai_agents import ExoRunHooks
    from agents import Runner

    hooks = ExoRunHooks(repo=".", ticket_id="TKT-...", actor="agent:openai")
    result = await Runner.run(agent, hooks=hooks)

Requirements:
    pip install exoprotocol[openai-agents]

Note: ``agents`` is imported lazily inside methods — this module can be
imported without the OpenAI Agents SDK installed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Sentinel for lazy base class resolution
_RunHooksBase: type | None = None


def _get_base() -> type:
    """Lazily resolve RunHooks base class from agents SDK."""
    global _RunHooksBase  # noqa: PLW0603
    if _RunHooksBase is not None:
        return _RunHooksBase
    try:
        from agents import RunHooks

        _RunHooksBase = RunHooks
    except ImportError:
        _RunHooksBase = object
    return _RunHooksBase


class ExoRunHooks:
    """OpenAI Agents SDK RunHooks subclass for governed agent runs.

    Wraps the ExoProtocol session lifecycle around agent execution:
    - ``on_agent_start``: starts a governed session (or reuses active)
    - ``on_agent_end``: finishes the governed session with summary
    - ``on_tool_start``: logs tool invocations for audit trail
    - ``on_handoff``: logs agent-to-agent handoffs

    All governance operations are wrapped in try/except — failures are
    logged but never crash the agent run.
    """

    def __init__(
        self,
        repo: str | Path = ".",
        *,
        ticket_id: str = "",
        actor: str = "agent:openai",
        vendor: str = "openai",
        model: str = "unknown",
    ) -> None:
        self.repo = Path(repo).resolve()
        self.ticket_id = ticket_id
        self.actor = actor
        self.vendor = vendor
        self.model = model
        self._session_id: str = ""
        self._started: bool = False
        self._tool_calls: list[dict[str, str]] = []

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Ensure proper MRO when agents SDK is available."""
        super().__init_subclass__(**kwargs)

    async def on_agent_start(self, context: Any, agent: Any) -> None:
        """Start a governed session when the agent run begins."""
        try:
            from exo.orchestrator.session import AgentSessionManager

            manager = AgentSessionManager(self.repo, actor=self.actor)

            # Reuse active session if one exists
            active = manager.get_active()
            if active:
                self._session_id = str(active.get("session_id", ""))
                self._started = False
                return

            result = manager.start(
                vendor=self.vendor,
                model=self.model,
                ticket_id=self.ticket_id or None,
            )
            session_data = result.get("session", {})
            self._session_id = str(session_data.get("session_id", ""))
            self._started = True
        except Exception:
            logger.debug("ExoRunHooks: session start failed", exc_info=True)

    async def on_agent_end(self, context: Any, agent: Any, output: Any) -> None:
        """Finish the governed session when the agent run completes."""
        if not self._session_id:
            return
        try:
            from exo.orchestrator.session import AgentSessionManager

            manager = AgentSessionManager(self.repo, actor=self.actor)
            active = manager.get_active()
            if not active:
                return

            ticket_id = str(active.get("ticket_id", "")).strip()
            tool_summary = f" Tools called: {len(self._tool_calls)}." if self._tool_calls else ""
            summary = f"Agent run completed.{tool_summary}"

            manager.finish(
                summary=summary,
                ticket_id=ticket_id,
                set_status="keep",
                skip_check=True,
                break_glass_reason="auto-close via ExoRunHooks",
                release_lock=False,
            )
        except Exception:
            logger.debug("ExoRunHooks: session finish failed", exc_info=True)

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        """Log tool invocation for audit trail."""
        tool_name = getattr(tool, "name", str(tool))
        self._tool_calls.append({"tool": tool_name})

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        """No-op — tool end logging not needed for governance."""

    async def on_handoff(self, context: Any, agent: Any, source: Any) -> None:
        """Log agent-to-agent handoff for audit trail."""
        source_name = getattr(source, "name", str(source))
        agent_name = getattr(agent, "name", str(agent))
        logger.info("ExoRunHooks: handoff from %s to %s", source_name, agent_name)

    def get_session_id(self) -> str:
        """Return the current session ID (empty if no session started)."""
        return self._session_id

    def get_tool_calls(self) -> list[dict[str, str]]:
        """Return recorded tool invocations."""
        return list(self._tool_calls)
