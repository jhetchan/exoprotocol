"""Tool duplication detection and suggestion engine.

Analyzes the tool registry and session index to surface:
- Underused tools (registered but never used by another session)
- Unaware sessions (sessions that never interacted with the tool registry)
- Keyword-matched sessions (summaries mentioning utility patterns without tool registration)

Deterministic (no LLM). Called from CLI and MCP.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from exo.stdlib.tools import TOOLS_PATH, load_tools

SESSION_INDEX_PATH = Path(".exo/memory/sessions/index.jsonl")

# Keywords that suggest utility/tool creation in session summaries.
# Lowercase, matched as whole words within the summary.
UTILITY_KEYWORDS: tuple[str, ...] = (
    "helper",
    "utility",
    "parser",
    "formatter",
    "converter",
    "validator",
    "wrapper",
    "handler",
    "adapter",
    "serializer",
    "deserializer",
    "transformer",
    "generator",
    "builder",
    "factory",
    "decorator",
    "middleware",
    "normalizer",
    "sanitizer",
    "encoder",
    "decoder",
)


@dataclass(frozen=True)
class Suggestion:
    """A single tool-suggest recommendation."""

    kind: str  # "underused" | "unaware_session" | "keyword_match"
    message: str
    details: dict[str, Any]


def _load_session_index(repo: Path) -> list[dict[str, Any]]:
    """Load all rows from the session index."""
    index_path = repo / SESSION_INDEX_PATH
    if not index_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in index_path.read_text(encoding="utf-8").strip().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _find_underused_tools(repo: Path) -> list[Suggestion]:
    """Find tools registered but never used by another session."""
    tools = load_tools(repo) if (repo / TOOLS_PATH).exists() else []
    suggestions: list[Suggestion] = []
    for tool in tools:
        if not tool.used_by or (len(tool.used_by) == 1 and tool.created_by in tool.used_by):
            suggestions.append(
                Suggestion(
                    kind="underused",
                    message=f"Tool `{tool.id}` was registered but never used by another session",
                    details={
                        "tool_id": tool.id,
                        "description": tool.description,
                        "created_by": tool.created_by,
                        "used_count": len(tool.used_by),
                    },
                )
            )
    return suggestions


def _find_unaware_sessions(rows: list[dict[str, Any]]) -> list[Suggestion]:
    """Find sessions that never interacted with the tool registry."""
    suggestions: list[Suggestion] = []
    for row in rows:
        mode = str(row.get("mode", "work")).strip()
        if mode == "audit":
            continue  # Audit sessions are expected to skip tools
        created = row.get("tools_created")
        used = row.get("tools_used")
        # Only flag if both fields exist (tool tracking was active) and both are 0
        if created is not None and used is not None and created == 0 and used == 0:
            suggestions.append(
                Suggestion(
                    kind="unaware_session",
                    message=f"Session `{row.get('session_id', '?')}` did not register or use any tools",
                    details={
                        "session_id": row.get("session_id", ""),
                        "ticket_id": row.get("ticket_id", ""),
                        "summary": str(row.get("summary", ""))[:120],
                    },
                )
            )
    return suggestions


def _find_keyword_matches(rows: list[dict[str, Any]]) -> list[Suggestion]:
    """Find sessions whose summaries mention utility patterns without tool registration."""
    suggestions: list[Suggestion] = []
    for row in rows:
        mode = str(row.get("mode", "work")).strip()
        if mode == "audit":
            continue
        created = row.get("tools_created")
        if created is not None and created > 0:
            continue  # Session already registered tools — no suggestion needed
        summary = str(row.get("summary", "")).lower()
        if not summary:
            continue
        matched = [kw for kw in UTILITY_KEYWORDS if kw in summary]
        if matched:
            suggestions.append(
                Suggestion(
                    kind="keyword_match",
                    message=(
                        f"Session `{row.get('session_id', '?')}` summary mentions "
                        f"{', '.join(matched)} but registered no tools"
                    ),
                    details={
                        "session_id": row.get("session_id", ""),
                        "ticket_id": row.get("ticket_id", ""),
                        "matched_keywords": matched,
                        "summary": str(row.get("summary", ""))[:120],
                    },
                )
            )
    return suggestions


def suggest_tools(repo: Path) -> list[Suggestion]:
    """Analyze tool registry and session index for improvement suggestions.

    Returns a list of suggestions sorted by kind priority:
    underused > keyword_match > unaware_session.
    """
    repo = Path(repo).resolve()
    rows = _load_session_index(repo)

    suggestions: list[Suggestion] = []
    suggestions.extend(_find_underused_tools(repo))
    suggestions.extend(_find_keyword_matches(rows))
    suggestions.extend(_find_unaware_sessions(rows))

    return suggestions


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def suggestion_to_dict(s: Suggestion) -> dict[str, Any]:
    """Convert a Suggestion to a plain dict."""
    return {"kind": s.kind, "message": s.message, "details": s.details}


def suggestions_to_list(suggestions: list[Suggestion]) -> list[dict[str, Any]]:
    """Convert suggestion list to plain dicts for JSON."""
    return [suggestion_to_dict(s) for s in suggestions]


def format_suggestions_human(suggestions: list[Suggestion]) -> str:
    """Format suggestions as human-readable text."""
    if not suggestions:
        return "Tool Suggestions: (none — registry looks healthy)"
    lines = [f"Tool Suggestions: {len(suggestions)} found"]
    for s in suggestions:
        kind_label = {"underused": "UNDERUSED", "unaware_session": "UNAWARE", "keyword_match": "KEYWORD"}.get(
            s.kind, s.kind.upper()
        )
        lines.append(f"  [{kind_label}] {s.message}")
    return "\n".join(lines)
