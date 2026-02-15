"""Claude Code hook integration: auto session-start/finish via native hooks."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

HOOK_ACTOR = "agent:claude-code"
HOOK_VENDOR = "anthropic"


def _write_env_vars(env_vars: dict[str, str]) -> None:
    """Write environment variables to $CLAUDE_ENV_FILE if set."""
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file:
        return
    with open(env_file, "a", encoding="utf-8") as fh:
        for key, value in env_vars.items():
            fh.write(f"{key}={value}\n")


def handle_session_start(hook_input: dict[str, Any]) -> dict[str, Any]:
    """Handle Claude Code SessionStart hook event.

    Auto-starts an ExoProtocol governed session from the active lock.
    Returns the bootstrap prompt for context injection.
    """
    try:
        cwd = hook_input.get("cwd") or os.getcwd()
        repo = Path(cwd).resolve()

        if not (repo / ".exo").is_dir():
            return {"skipped": True, "reason": "no_exo_dir"}

        from exo.kernel.tickets import load_lock

        lock = load_lock(repo)
        if not lock:
            return {"skipped": True, "reason": "no_lock"}

        ticket_id = str(lock.get("ticket_id", "")).strip()
        if not ticket_id:
            return {"skipped": True, "reason": "no_ticket_in_lock"}

        model = str(hook_input.get("model", "unknown")).strip() or "unknown"

        from exo.orchestrator.session import AgentSessionManager

        manager = AgentSessionManager(repo, actor=HOOK_ACTOR)
        result = manager.start(vendor=HOOK_VENDOR, model=model)

        reused = bool(result.get("reused"))
        session_data = result.get("session", {})
        session_id = str(session_data.get("session_id", "")).strip()

        # Get bootstrap prompt — present in fresh starts, read from disk for reused
        bootstrap_prompt = result.get("bootstrap_prompt", "")
        if not bootstrap_prompt and result.get("bootstrap_path"):
            bootstrap_path = repo / result["bootstrap_path"]
            if bootstrap_path.exists():
                bootstrap_prompt = bootstrap_path.read_text(encoding="utf-8")

        env_vars = {
            "EXO_SESSION_ID": session_id,
            "EXO_TICKET_ID": ticket_id,
            "EXO_ACTOR": HOOK_ACTOR,
        }
        _write_env_vars(env_vars)

        return {
            "started": not reused,
            "reused": reused,
            "session_id": session_id,
            "ticket_id": ticket_id,
            "bootstrap_prompt": bootstrap_prompt,
            "env_vars": env_vars,
        }
    except Exception as exc:
        return {"skipped": True, "reason": "error", "error": str(exc)}


def handle_session_end(hook_input: dict[str, Any]) -> dict[str, Any]:
    """Handle Claude Code SessionEnd hook event.

    Auto-finishes the active session with a generic summary.
    Uses set_status=keep and release_lock=False for gentle close.
    """
    try:
        cwd = hook_input.get("cwd") or os.getcwd()
        repo = Path(cwd).resolve()

        if not (repo / ".exo").is_dir():
            return {"skipped": True, "reason": "no_exo_dir"}

        from exo.orchestrator.session import AgentSessionManager

        manager = AgentSessionManager(repo, actor=HOOK_ACTOR)
        active = manager.get_active()
        if not active:
            return {"skipped": True, "reason": "no_active_session"}

        ticket_id = str(active.get("ticket_id", "")).strip()
        reason = str(hook_input.get("reason", "unknown")).strip()
        summary = f"Auto-closed by Claude Code SessionEnd hook (reason: {reason})"

        result = manager.finish(
            summary=summary,
            ticket_id=ticket_id,
            set_status="keep",
            skip_check=True,
            break_glass_reason="auto-close via Claude Code SessionEnd hook",
            release_lock=False,
        )

        return {
            "finished": True,
            "session_id": result.get("session_id", ""),
            "ticket_id": ticket_id,
        }
    except Exception as exc:
        return {"skipped": True, "reason": "error", "error": str(exc)}


def generate_hook_config() -> dict[str, Any]:
    """Generate Claude Code hook configuration for ExoProtocol governance."""
    return {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 -m exo.stdlib.hooks session-start",
                            "timeout": 30,
                        }
                    ],
                }
            ],
            "SessionEnd": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 -m exo.stdlib.hooks session-end",
                            "timeout": 30,
                        }
                    ],
                }
            ],
        }
    }


def install_hooks(repo: Path | str, *, dry_run: bool = False) -> dict[str, Any]:
    """Install ExoProtocol hooks into .claude/settings.json."""
    repo = Path(repo).resolve()
    settings_path = repo / ".claude" / "settings.json"
    config = generate_hook_config()

    if dry_run:
        return {"installed": False, "dry_run": True, "config": config, "path": str(settings_path)}

    existing: dict[str, Any] = {}
    if settings_path.exists():
        existing = json.loads(settings_path.read_text(encoding="utf-8"))

    existing["hooks"] = config["hooks"]

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )

    return {"installed": True, "dry_run": False, "config": config, "path": str(settings_path)}


def discover_tools() -> list[dict[str, Any]]:
    """Discover available ExoProtocol tools via importlib.metadata.

    Returns a list of tool descriptors including the core CLI, MCP server
    (if mcp extra is installed), Claude Code hooks, and any registered
    integration entry points under the ``exoprotocol.integrations`` group.
    """
    import importlib.metadata

    tools: list[dict[str, Any]] = []

    # Core CLI — always available
    tools.append(
        {
            "name": "exo",
            "type": "cli",
            "module": "exo.cli",
            "description": "ExoProtocol governance CLI",
        }
    )

    # MCP server — available when mcp extra installed
    try:
        importlib.metadata.distribution("mcp")
        tools.append(
            {
                "name": "exo-mcp",
                "type": "mcp",
                "module": "exo.mcp_server",
                "description": "ExoProtocol MCP server",
            }
        )
    except importlib.metadata.PackageNotFoundError:
        pass

    # Claude Code hooks — always available (part of core package)
    tools.append(
        {
            "name": "claude-hooks",
            "type": "hooks",
            "module": "exo.stdlib.hooks",
            "description": "Claude Code SessionStart/SessionEnd hooks",
        }
    )

    # Dynamically discovered integration entry points
    try:
        eps = importlib.metadata.entry_points()
        if hasattr(eps, "select"):
            exo_eps = list(eps.select(group="exoprotocol.integrations"))
        else:
            exo_eps = list(eps.get("exoprotocol.integrations", []))
        for ep in exo_eps:
            tools.append(
                {
                    "name": ep.name,
                    "type": "integration",
                    "module": str(ep.value),
                    "description": f"ExoProtocol {ep.name} integration",
                }
            )
    except Exception:  # noqa: BLE001
        pass  # Never crash on discovery failure

    return tools


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for hook invocation: python3 -m exo.stdlib.hooks <event>."""
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        return 0

    command = args[0]

    try:
        raw = sys.stdin.read()
        hook_input: dict[str, Any] = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        hook_input = {}

    try:
        if command == "session-start":
            result = handle_session_start(hook_input)
            bootstrap = result.get("bootstrap_prompt", "")
            if bootstrap:
                sys.stdout.write(bootstrap)
        elif command == "session-end":
            handle_session_end(hook_input)
    except Exception:
        pass  # Never crash Claude Code

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
