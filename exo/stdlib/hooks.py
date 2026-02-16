"""Claude Code hook integration: auto session-start/finish via native hooks.

Also provides self-healing enforcement:
- Hook integrity verification against sealed policy
- Scope-gated Write/Edit blocking via PreToolUse
- Auto-reinstall on tamper detection
"""

from __future__ import annotations

import hashlib
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


# ── Git pre-commit hook ───────────────────────────────────────────


GIT_HOOK_SCRIPT = """\
#!/usr/bin/env bash
# ExoProtocol pre-commit hook — runs composed governance checks before commit.
# Installed by: exo hook-install --git
set -euo pipefail

# Find the exo CLI
EXO_CMD=""
if command -v exo >/dev/null 2>&1; then
    EXO_CMD="exo"
elif command -v python3 >/dev/null 2>&1 && python3 -c "import exo" 2>/dev/null; then
    EXO_CMD="python3 -m exo.cli"
fi

if [ -n "$EXO_CMD" ] && [ -d ".exo" ]; then
    $EXO_CMD check --format human 2>/dev/null
    EXIT_CODE=$?
    if [ $EXIT_CODE -ne 0 ]; then
        echo ""
        echo "exo pre-commit: governance checks failed. Fix issues or use --no-verify to bypass."
        exit 1
    fi
fi
"""


def install_git_hook(repo: Path | str, *, dry_run: bool = False) -> dict[str, Any]:
    """Install a git pre-commit hook that runs ``exo check`` before each commit.

    The hook script exits non-zero when ``exo check`` fails, blocking the commit.
    Falls through silently if ``exo`` is not on PATH.
    """
    repo = Path(repo).resolve()
    git_dir = repo / ".git"
    if not git_dir.is_dir():
        return {"installed": False, "error": "no_git_dir", "path": ""}

    hooks_dir = git_dir / "hooks"
    hook_path = hooks_dir / "pre-commit"

    if dry_run:
        return {
            "installed": False,
            "dry_run": True,
            "path": str(hook_path),
            "script": GIT_HOOK_SCRIPT,
        }

    hooks_dir.mkdir(parents=True, exist_ok=True)

    # If existing hook exists and is NOT ours, back it up
    backed_up = ""
    if hook_path.exists():
        existing = hook_path.read_text(encoding="utf-8")
        if "ExoProtocol pre-commit hook" not in existing:
            backup = hook_path.with_suffix(".pre-exo")
            backup.write_text(existing, encoding="utf-8")
            backed_up = str(backup)

    hook_path.write_text(GIT_HOOK_SCRIPT, encoding="utf-8")
    hook_path.chmod(0o755)

    return {
        "installed": True,
        "dry_run": False,
        "path": str(hook_path),
        "backed_up": backed_up,
    }


# ── Claude Code PreToolUse enforcement hook ───────────────────────


ENFORCE_HOOK_COMMAND = "exo check --format json"


def generate_enforce_config() -> dict[str, Any]:
    """Generate Claude Code PreToolUse enforcement hook config.

    Intercepts ``Bash`` tool calls matching ``git commit`` or ``git push``
    and runs ``exo check``. If checks fail the tool call is blocked.
    """
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                "bash -c '"
                                "INPUT=$(cat); "
                                'CMD=$(echo "$INPUT" | python3 -c "import sys,json; '
                                "d=json.load(sys.stdin); "
                                "print(d.get('input',{}).get('command',''))"
                                '"); '
                                'case "$CMD" in '
                                "*git commit*|*git push*) "
                                f"{ENFORCE_HOOK_COMMAND} || "
                                "exit 2;; "
                                "esac'"
                            ),
                            "timeout": 30,
                        }
                    ],
                }
            ],
        }
    }


def install_enforce_hooks(repo: Path | str, *, dry_run: bool = False) -> dict[str, Any]:
    """Install Claude Code PreToolUse enforcement hooks.

    Merges a ``PreToolUse`` entry into ``.claude/settings.json`` that
    intercepts ``git commit``/``git push`` bash commands and gates them
    on ``exo check`` passing.  Existing session lifecycle hooks and other
    settings are preserved.
    """
    repo = Path(repo).resolve()
    settings_path = repo / ".claude" / "settings.json"
    config = generate_enforce_config()

    if dry_run:
        return {
            "installed": False,
            "dry_run": True,
            "config": config,
            "path": str(settings_path),
        }

    existing: dict[str, Any] = {}
    if settings_path.exists():
        existing = json.loads(settings_path.read_text(encoding="utf-8"))

    hooks = existing.setdefault("hooks", {})

    # Merge: keep existing SessionStart/SessionEnd, add/replace PreToolUse
    hooks["PreToolUse"] = config["hooks"]["PreToolUse"]

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )

    return {
        "installed": True,
        "dry_run": False,
        "config": config,
        "path": str(settings_path),
    }


# ── Self-healing enforcement ─────────────────────────────────────


def _compute_current_hooks_hash(repo: Path) -> str:
    """SHA-256 of .claude/settings.json hooks section."""
    settings_path = repo / ".claude" / "settings.json"
    if not settings_path.exists():
        return ""
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = data.get("hooks")
        if not hooks:
            return ""
        canonical = json.dumps(hooks, sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except (json.JSONDecodeError, OSError):
        return ""


def verify_hook_integrity(repo: Path) -> dict[str, Any]:
    """Check .claude/settings.json hooks hash matches sealed policy.

    Returns dict with verified (bool) and reason (str).
    """
    repo = Path(repo).resolve()
    from exo.stdlib.compose import load_sealed_policy

    policy = load_sealed_policy(repo)
    if not policy:
        return {"verified": False, "reason": "no_sealed_policy"}
    expected = policy.get("hooks_hash", "")
    if not expected:
        return {"verified": True, "reason": "no_hooks_hash_in_policy"}
    actual = _compute_current_hooks_hash(repo)
    match = expected == actual
    return {"verified": match, "reason": "match" if match else "tamper_detected"}


def check_scope_for_tool(repo: Path, tool_name: str, file_path: str) -> dict[str, Any]:
    """Check if a tool call target path is within governed scope.

    Reads active session's ticket scope + sealed policy global deny.
    Returns {allowed: bool, reason: str, ...}.
    Fail-open: if no session is active, returns allowed=True.
    """
    repo = Path(repo).resolve()

    # Load sealed policy for global deny patterns
    from exo.stdlib.compose import load_sealed_policy

    policy = load_sealed_policy(repo)
    global_deny = policy.get("deny_patterns", []) if policy else []

    # Check global deny first
    if global_deny and file_path:
        from exo.kernel.utils import any_pattern_matches

        target = repo / file_path
        if any_pattern_matches(target, global_deny, repo):
            return {
                "allowed": False,
                "reason": "global_deny",
                "file_path": file_path,
                "tool": tool_name,
                "deny_match": True,
            }

    # Load active session to get ticket scope
    # Scan all actors for any active session, not just HOOK_ACTOR
    try:
        from exo.orchestrator.session import scan_sessions

        scan = scan_sessions(repo, stale_hours=9999)
        active_list = scan.get("active_sessions", [])
        active = active_list[0] if active_list else None
        if not active:
            return {"allowed": True, "reason": "no_active_session", "file_path": file_path, "tool": tool_name}

        ticket_id = str(active.get("ticket_id", "")).strip()
        if not ticket_id:
            return {"allowed": True, "reason": "no_ticket", "file_path": file_path, "tool": tool_name}

        from exo.kernel.tickets import load_ticket

        ticket = load_ticket(repo, ticket_id)
        scope = ticket.get("scope", {})
        allow_patterns = scope.get("allow", ["**"])
        deny_patterns = scope.get("deny", [])

        from exo.kernel.utils import any_pattern_matches

        target = repo / file_path

        # Check ticket deny first (always wins)
        if deny_patterns and any_pattern_matches(target, deny_patterns, repo):
            return {
                "allowed": False,
                "reason": "ticket_deny",
                "file_path": file_path,
                "tool": tool_name,
                "ticket_id": ticket_id,
                "deny_match": True,
            }

        # Check ticket allow
        if allow_patterns and not any_pattern_matches(target, allow_patterns, repo):
            return {
                "allowed": False,
                "reason": "out_of_scope",
                "file_path": file_path,
                "tool": tool_name,
                "ticket_id": ticket_id,
                "deny_match": False,
            }

        return {
            "allowed": True,
            "reason": "in_scope",
            "file_path": file_path,
            "tool": tool_name,
            "ticket_id": ticket_id,
        }
    except Exception:
        # Fail-open on error
        return {"allowed": True, "reason": "error", "file_path": file_path, "tool": tool_name}


def handle_scope_check(hook_input: dict[str, Any]) -> dict[str, Any]:
    """Handle PreToolUse scope check for Write/Edit tool calls.

    Parses the tool input to extract the target file path, then
    checks it against the active session's scope.
    """
    try:
        cwd = hook_input.get("cwd") or os.getcwd()
        repo = Path(cwd).resolve()

        if not (repo / ".exo").is_dir():
            return {"allowed": True, "reason": "no_exo_dir"}

        tool_name = str(hook_input.get("tool_name", "")).strip()
        tool_input = hook_input.get("input", {})

        # Extract file path from Write/Edit tool input
        file_path = str(tool_input.get("file_path", "")).strip()
        if not file_path:
            return {"allowed": True, "reason": "no_file_path"}

        # Make path relative to repo
        try:
            abs_path = Path(file_path).resolve()
            rel_path = str(abs_path.relative_to(repo))
        except (ValueError, OSError):
            rel_path = file_path

        return check_scope_for_tool(repo, tool_name, rel_path)
    except Exception:
        return {"allowed": True, "reason": "error"}


def generate_scope_enforce_config() -> dict[str, Any]:
    """Generate PreToolUse config that blocks Write/Edit outside scope."""
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Write|Edit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 -m exo.stdlib.hooks scope-check",
                            "timeout": 10,
                        }
                    ],
                }
            ],
        }
    }


def _log_tamper_event(repo: Path, check: dict[str, Any]) -> None:
    """Append tamper event to .exo/audit/tamper.jsonl."""
    from exo.kernel.utils import now_iso

    audit_dir = repo / ".exo" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    event = {
        "event": "hook_tamper_detected",
        "reason": check.get("reason", "unknown"),
        "detected_at": now_iso(),
    }
    tamper_log = audit_dir / "tamper.jsonl"
    with tamper_log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=True) + "\n")


def auto_heal_hooks(repo: Path) -> dict[str, Any]:
    """Re-install hooks from governance state if tampering detected.

    Checks hook integrity against sealed policy. If tampered:
    1. Logs tamper event to .exo/audit/tamper.jsonl
    2. Reinstalls session lifecycle + enforcement hooks
    3. Re-composes sealed policy to update hooks_hash

    Returns dict with healed (bool) and details.
    """
    repo = Path(repo).resolve()
    check = verify_hook_integrity(repo)
    if check.get("verified"):
        return {"healed": False, "reason": "no_tamper"}

    _log_tamper_event(repo, check)

    # Reinstall hooks
    install_hooks(repo)
    install_enforce_hooks(repo)

    # Re-compose to update hooks_hash
    from exo.stdlib.compose import compose

    compose(repo)

    return {"healed": True, "tamper_reason": check.get("reason", "")}


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
        elif command == "scope-check":
            result = handle_scope_check(hook_input)
            if not result.get("allowed"):
                reason = result.get("reason", "blocked")
                sys.stderr.write(f"BLOCKED: {reason} — {result.get('file_path', '')}\n")
                return 2
    except Exception:
        pass  # Never crash Claude Code

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
