"""Tool Awareness Registry.

Manages a registry of reusable tools that AI agents have built. Agents can
register tools, search for existing tools before writing new code, and track
tool usage across sessions.

Storage: ``.exo/tools.yaml`` — a single YAML file with a ``tools:`` list.
Missing file means empty registry (no error).

Deterministic (no LLM). Called from CLI and MCP.
"""
# @feature:tool-awareness

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from exo.kernel.errors import ExoError
from exo.kernel.utils import dump_yaml, load_yaml, now_iso

TOOLS_PATH = Path(".exo/tools.yaml")


@dataclass(frozen=True)
class ToolDef:
    """A registered reusable tool."""

    id: str
    module: str
    function: str
    description: str
    signature: str = ""
    tags: tuple[str, ...] = ()
    created_by: str = ""
    used_by: tuple[str, ...] = ()
    last_used: str = ""
    created_at: str = ""


# ---------------------------------------------------------------------------
# ID derivation
# ---------------------------------------------------------------------------


def _derive_tool_id(module: str, function: str) -> str:
    """Derive tool ID from module path + function name.

    ``"lib/parsers/csv_utils.py"`` + ``"parse_csv"``
    → ``"lib.parsers.csv_utils:parse_csv"``
    """
    mod_part = module.replace("/", ".").replace("\\", ".")
    if mod_part.endswith(".py"):
        mod_part = mod_part[:-3]
    return f"{mod_part}:{function}"


# ---------------------------------------------------------------------------
# Load / Save
# ---------------------------------------------------------------------------


def load_tools(repo: Path) -> list[ToolDef]:
    """Load tool definitions from ``.exo/tools.yaml``.

    Returns empty list when the file is missing (empty registry is valid).
    """
    repo = Path(repo).resolve()
    tools_path = repo / TOOLS_PATH
    if not tools_path.exists():
        return []

    raw = load_yaml(tools_path)
    entries = raw.get("tools", [])
    if not isinstance(entries, list):
        raise ExoError(
            code="TOOLS_MANIFEST_INVALID",
            message="tools.yaml 'tools' must be a list",
            blocked=True,
        )

    tools: list[ToolDef] = []
    seen_ids: set[str] = set()

    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ExoError(
                code="TOOLS_ENTRY_INVALID",
                message=f"tools[{i}] must be a mapping",
                blocked=True,
            )

        tid = str(entry.get("id", "")).strip()
        module = str(entry.get("module", "")).strip()
        function = str(entry.get("function", "")).strip()

        if not tid and module and function:
            tid = _derive_tool_id(module, function)

        if not tid:
            raise ExoError(
                code="TOOLS_ENTRY_MISSING_ID",
                message=f"tools[{i}] missing 'id' (and insufficient data to derive one)",
                blocked=True,
            )

        if tid in seen_ids:
            raise ExoError(
                code="TOOLS_DUPLICATE_ID",
                message=f"duplicate tool id: {tid}",
                blocked=True,
            )
        seen_ids.add(tid)

        tags_raw = entry.get("tags", [])
        tags = tuple(str(t).strip() for t in tags_raw if str(t).strip()) if isinstance(tags_raw, list) else ()

        used_raw = entry.get("used_by", [])
        used_by = tuple(str(u).strip() for u in used_raw if str(u).strip()) if isinstance(used_raw, list) else ()

        tools.append(
            ToolDef(
                id=tid,
                module=module,
                function=function,
                description=str(entry.get("description", "")).strip(),
                signature=str(entry.get("signature", "")).strip(),
                tags=tags,
                created_by=str(entry.get("created_by", "")).strip(),
                used_by=used_by,
                last_used=str(entry.get("last_used", "")).strip(),
                created_at=str(entry.get("created_at", "")).strip(),
            )
        )

    return tools


def _save_tools(repo: Path, tools: list[ToolDef]) -> Path:
    """Write tool definitions back to ``.exo/tools.yaml``."""
    tools_path = repo / TOOLS_PATH
    data = {"tools": [_tool_to_yaml_dict(t) for t in tools]}
    dump_yaml(tools_path, data)
    return tools_path


def _tool_to_yaml_dict(tool: ToolDef) -> dict[str, Any]:
    """Convert ToolDef to dict for YAML serialization."""
    return {
        "id": tool.id,
        "module": tool.module,
        "function": tool.function,
        "description": tool.description,
        "signature": tool.signature,
        "tags": list(tool.tags),
        "created_by": tool.created_by,
        "used_by": list(tool.used_by),
        "last_used": tool.last_used,
        "created_at": tool.created_at,
    }


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------


def register_tool(
    repo: Path,
    *,
    module: str,
    function: str,
    description: str,
    signature: str = "",
    tags: list[str] | None = None,
    session_id: str = "",
) -> ToolDef:
    """Register a new tool in ``.exo/tools.yaml``. Returns the created ToolDef."""
    repo = Path(repo).resolve()

    module_val = module.strip()
    if not module_val:
        raise ExoError(code="TOOLS_MODULE_REQUIRED", message="module is required", blocked=True)

    function_val = function.strip()
    if not function_val:
        raise ExoError(code="TOOLS_FUNCTION_REQUIRED", message="function is required", blocked=True)

    description_val = description.strip()
    if not description_val:
        raise ExoError(code="TOOLS_DESCRIPTION_REQUIRED", message="description is required", blocked=True)

    tool_id = _derive_tool_id(module_val, function_val)

    tools = load_tools(repo)
    if any(t.id == tool_id for t in tools):
        raise ExoError(
            code="TOOLS_DUPLICATE_ID",
            message=f"tool already registered: {tool_id}",
            blocked=True,
        )

    tag_tuple = tuple(str(t).strip() for t in (tags or []) if str(t).strip())

    tool = ToolDef(
        id=tool_id,
        module=module_val,
        function=function_val,
        description=description_val,
        signature=signature.strip() if signature else "",
        tags=tag_tuple,
        created_by=session_id,
        used_by=(),
        last_used="",
        created_at=now_iso(),
    )

    tools.append(tool)
    _save_tools(repo, tools)
    return tool


def remove_tool(repo: Path, *, tool_id: str) -> None:
    """Remove a tool from the registry by ID."""
    repo = Path(repo).resolve()
    tools = load_tools(repo)

    new_tools = [t for t in tools if t.id != tool_id]
    if len(new_tools) == len(tools):
        raise ExoError(
            code="TOOLS_NOT_FOUND",
            message=f"tool not found: {tool_id}",
            blocked=True,
        )

    _save_tools(repo, new_tools)


def mark_tool_used(repo: Path, *, tool_id: str, session_id: str = "") -> ToolDef:
    """Record that a tool was used in a session.

    Updates ``last_used`` and appends ``session_id`` to ``used_by``.
    """
    repo = Path(repo).resolve()
    tools = load_tools(repo)

    updated: list[ToolDef] = []
    found: ToolDef | None = None
    for tool in tools:
        if tool.id == tool_id:
            used_set = set(tool.used_by)
            if session_id:
                used_set.add(session_id)
            found = ToolDef(
                id=tool.id,
                module=tool.module,
                function=tool.function,
                description=tool.description,
                signature=tool.signature,
                tags=tool.tags,
                created_by=tool.created_by,
                used_by=tuple(sorted(used_set)),
                last_used=now_iso(),
                created_at=tool.created_at,
            )
            updated.append(found)
        else:
            updated.append(tool)

    if found is None:
        raise ExoError(code="TOOLS_NOT_FOUND", message=f"tool not found: {tool_id}", blocked=True)

    _save_tools(repo, updated)
    return found


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def search_tools(repo: Path, *, query: str) -> list[ToolDef]:
    """Search tools by keyword matching on description, tags, module, function, id.

    Splits query into words, scores each tool by number of matching words.
    Returns tools sorted by score descending (ties broken by id).
    """
    repo = Path(repo).resolve()
    tools = load_tools(repo)

    query_words = [w.lower() for w in query.strip().split() if w.strip()]
    if not query_words:
        return tools

    scored: list[tuple[int, ToolDef]] = []
    for tool in tools:
        searchable = " ".join(
            [
                tool.description.lower(),
                " ".join(t.lower() for t in tool.tags),
                tool.module.lower(),
                tool.function.lower(),
                tool.id.lower(),
            ]
        )
        score = sum(1 for word in query_words if word in searchable)
        if score > 0:
            scored.append((score, tool))

    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    return [tool for _, tool in scored]


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def tool_to_dict(tool: ToolDef) -> dict[str, Any]:
    """Convert a single ToolDef to a plain dict for JSON serialization."""
    return _tool_to_yaml_dict(tool)


def tools_to_list(tools: list[ToolDef]) -> list[dict[str, Any]]:
    """Convert tool definitions to plain dicts for serialization."""
    return [_tool_to_yaml_dict(t) for t in tools]


def format_tools_human(tools: list[ToolDef]) -> str:
    """Format tool list as human-readable text."""
    if not tools:
        return "Tools: (none registered)"
    lines = [f"Tools: {len(tools)} registered"]
    for tool in tools:
        usage = len(tool.used_by)
        tag_str = f" [{', '.join(tool.tags)}]" if tool.tags else ""
        lines.append(f"  {tool.id}{tag_str}")
        lines.append(f"    {tool.description}")
        if tool.signature:
            lines.append(f"    sig: {tool.signature}")
        lines.append(f"    usage: {usage} session(s)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bootstrap injection
# ---------------------------------------------------------------------------


def format_bootstrap_tools(tools: list[ToolDef]) -> list[str]:
    """Generate bootstrap lines for the Tool Reuse Protocol.

    Injected into session bootstrap so agents see registered tools and
    the search-before-build directive at session start.
    """
    lines = [
        "## Tool Reuse Protocol",
        "",
        "Before writing new utility functions, SEARCH the tool registry:",
        '  exo tool-search "<keywords>"',
        "",
        "After building a reusable utility, REGISTER it:",
        '  exo tool-register <module> <function> --description "..."',
        "",
    ]
    if tools:
        lines.append(f"### Registered Tools ({len(tools)})")
        for tool in tools:
            tag_str = f" [{', '.join(tool.tags)}]" if tool.tags else ""
            sig_str = f" — `{tool.signature}`" if tool.signature else ""
            lines.append(f"- `{tool.id}`{tag_str}: {tool.description}{sig_str}")
        lines.append("")
    else:
        lines.append("No tools registered yet. Register reusable utilities as you build them.")
        lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Session-finish tool tracking
# ---------------------------------------------------------------------------


def tools_session_summary(repo: Path, *, session_id: str) -> dict[str, Any]:
    """Compute tool registry stats for a session.

    Returns a dict with total count, tools created by this session,
    and tools used by this session.
    """
    repo = Path(repo).resolve()
    tools = load_tools(repo)

    created = [t for t in tools if t.created_by == session_id]
    used = [t for t in tools if session_id in t.used_by]

    return {
        "total_tools": len(tools),
        "tools_created": len(created),
        "tools_used": len(used),
        "created_ids": [t.id for t in created],
        "used_ids": [t.id for t in used],
    }


def format_tools_memento(summary: dict[str, Any]) -> str:
    """Format tool session summary as a memento section."""
    lines = ["## Tool Registry"]
    lines.append(f"- total: {summary['total_tools']}")
    lines.append(f"- created this session: {summary['tools_created']}")
    lines.append(f"- used this session: {summary['tools_used']}")
    if summary["created_ids"]:
        lines.append("- created:")
        for tid in summary["created_ids"]:
            lines.append(f"  - {tid}")
    if summary["used_ids"]:
        lines.append("- used:")
        for tid in summary["used_ids"]:
            lines.append(f"  - {tid}")
    return "\n".join(lines)
