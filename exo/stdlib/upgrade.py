"""Schema Migration (``exo upgrade``).

Upgrades an existing ``.exo/`` directory to the latest schema version:
- Adds missing config keys with defaults
- Creates missing directories
- Recompiles governance (constitution → lock)
- Regenerates adapters (CLAUDE.md, .cursorrules, AGENTS.md)
- Bumps config version

Migrations are sequential: v1→v2→v3→...  Each migration is a function
that receives (repo, config) and returns the updated config dict.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

from exo.kernel.errors import ExoError
from exo.kernel.utils import dump_yaml, ensure_dir, load_yaml, now_iso
from exo.stdlib.config_schema import CURRENT_VERSION
from exo.stdlib.defaults import DEFAULT_CONFIG

# ── Migration Registry ─────────────────────────────────────────────

# Each migration takes (repo, config) and returns updated config.
# Keyed by source version: migration[1] upgrades v1 → v2.
_MIGRATIONS: dict[int, Callable[[Path, dict[str, Any]], dict[str, Any]]] = {}


def _register_migration(from_version: int) -> Callable:
    """Decorator to register a migration function."""

    def decorator(fn: Callable[[Path, dict[str, Any]], dict[str, Any]]) -> Callable:
        _MIGRATIONS[from_version] = fn
        return fn

    return decorator


# ── Migrations ─────────────────────────────────────────────────────

# Example: when we bump to v2, add a migration here:
# @_register_migration(1)
# def _migrate_v1_to_v2(repo: Path, config: dict[str, Any]) -> dict[str, Any]:
#     config.setdefault("new_key", "default_value")
#     config["version"] = 2
#     return config


# ── Directory Scaffold ─────────────────────────────────────────────

_REQUIRED_DIRS = [
    ".exo",
    ".exo/tickets",
    ".exo/tickets/ARCHIVE",
    ".exo/locks",
    ".exo/scratchpad",
    ".exo/scratchpad/threads",
    ".exo/logs",
    ".exo/scripts",
    ".exo/cache",
    ".exo/cache/distill",
    ".exo/specs",
    ".exo/observations",
    ".exo/patches",
    ".exo/proposals",
    ".exo/reviews",
    ".exo/practices",
    ".exo/roles",
    ".exo/memory",
    ".exo/memory/reflections",
    ".exo/memory/sessions",
    ".exo/templates",
    ".exo/schemas",
    ".exo/cache/orchestrator",
    ".exo/cache/sessions",
]


# ── Core API ───────────────────────────────────────────────────────


def upgrade(
    repo: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Upgrade .exo/ to the latest schema version.

    Returns a dict with upgrade actions taken.
    """
    repo = Path(repo).resolve()
    exo_dir = repo / ".exo"

    if not exo_dir.is_dir():
        raise ExoError(
            code="UPGRADE_NO_EXO",
            message="No .exo directory found. Run 'exo init' first.",
            blocked=True,
        )

    actions: list[str] = []

    # 1. Load config
    config_path = exo_dir / "config.yaml"
    if not config_path.exists():
        raise ExoError(
            code="UPGRADE_NO_CONFIG",
            message="No .exo/config.yaml found. Run 'exo init' first.",
            blocked=True,
        )

    config = load_yaml(config_path)
    if not isinstance(config, dict):
        raise ExoError(
            code="UPGRADE_BAD_CONFIG",
            message="config.yaml is not a valid YAML mapping.",
            blocked=True,
        )

    current_version = int(config.get("version", 0))
    original_version = current_version

    # 2. Backfill missing config keys from DEFAULT_CONFIG
    backfilled = _backfill_config(config, DEFAULT_CONFIG)
    if backfilled:
        actions.extend(f"config: added missing key '{k}'" for k in backfilled)

    # 3. Run sequential migrations
    while current_version in _MIGRATIONS:
        migration_fn = _MIGRATIONS[current_version]
        if not dry_run:
            config = migration_fn(repo, config)
        current_version = config.get("version", current_version + 1)
        actions.append(f"migration: v{current_version - 1} → v{current_version}")

    # 4. Ensure version is set
    if "version" not in config:
        config["version"] = CURRENT_VERSION
        actions.append(f"config: set version to {CURRENT_VERSION}")

    # 5. Create missing directories
    dirs_created: list[str] = []
    for rel_dir in _REQUIRED_DIRS:
        dir_path = repo / rel_dir
        if not dir_path.is_dir():
            if not dry_run:
                ensure_dir(dir_path)
            dirs_created.append(rel_dir)
            actions.append(f"directory: created {rel_dir}")

    # 6. Write updated config
    if not dry_run and (backfilled or current_version != original_version):
        dump_yaml(config_path, config)

    # 7. Recompile governance
    recompiled = False
    constitution_path = exo_dir / "CONSTITUTION.md"
    if constitution_path.exists() and not dry_run:
        try:
            from exo.kernel import governance

            governance.compile_constitution(repo)
            recompiled = True
            actions.append("governance: recompiled constitution → lock")
        except Exception:  # noqa: BLE001
            actions.append("governance: recompile FAILED (non-blocking)")

    # 8. Regenerate adapters
    adapters_written: list[str] = []
    if not dry_run:
        try:
            from exo.stdlib.adapters import generate_adapters

            adapter_result = generate_adapters(repo)
            adapters_written = adapter_result.get("written", [])
            if adapters_written:
                actions.append(f"adapters: regenerated {len(adapters_written)} file(s)")
        except Exception:  # noqa: BLE001
            actions.append("adapters: regeneration FAILED (non-blocking)")

    return {
        "upgraded": True,
        "dry_run": dry_run,
        "from_version": original_version,
        "to_version": config.get("version", CURRENT_VERSION),
        "actions": actions,
        "dirs_created": dirs_created,
        "adapters_written": adapters_written,
        "recompiled": recompiled,
        "upgraded_at": now_iso(),
    }


def _backfill_config(
    config: dict[str, Any],
    defaults: dict[str, Any],
    _prefix: str = "",
) -> list[str]:
    """Recursively add missing keys from defaults to config.

    Returns list of dotted key paths that were added.
    """
    added: list[str] = []
    for key, default_value in defaults.items():
        path = f"{_prefix}.{key}" if _prefix else key
        if key not in config:
            config[key] = copy.deepcopy(default_value)
            added.append(path)
        elif isinstance(default_value, dict) and isinstance(config.get(key), dict):
            added.extend(_backfill_config(config[key], default_value, path))
    return added


# ── Serialization ──────────────────────────────────────────────────


def format_upgrade_human(result: dict[str, Any]) -> str:
    """Format upgrade result as human-readable text."""
    dry_tag = " (DRY RUN)" if result.get("dry_run") else ""
    lines = [
        f"Upgrade{dry_tag}: v{result['from_version']} → v{result['to_version']}",
    ]
    actions = result.get("actions", [])
    if actions:
        lines.append(f"  {len(actions)} action(s):")
        for action in actions:
            lines.append(f"    - {action}")
    else:
        lines.append("  No changes needed.")
    return "\n".join(lines)
