"""Tests for tool duplication detection and suggestion engine.

Covers: underused tools, unaware sessions, keyword-matched summaries,
        serialization, human formatting, and CLI integration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from exo.kernel.utils import dump_yaml
from exo.stdlib.suggest import (
    UTILITY_KEYWORDS,
    Suggestion,
    format_suggestions_human,
    suggest_tools,
    suggestion_to_dict,
    suggestions_to_list,
)
from exo.stdlib.tools import TOOLS_PATH

SESSION_INDEX_PATH = Path(".exo/memory/sessions/index.jsonl")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_tools_yaml(repo: Path, tools: list[dict[str, Any]]) -> None:
    tools_path = repo / TOOLS_PATH
    tools_path.parent.mkdir(parents=True, exist_ok=True)
    dump_yaml(tools_path, {"tools": tools})


def _write_session_index(repo: Path, rows: list[dict[str, Any]]) -> None:
    index_path = repo / SESSION_INDEX_PATH
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )


def _make_session_row(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "session_id": "SES-001",
        "actor": "agent:test",
        "ticket_id": "TICKET-001",
        "mode": "work",
        "summary": "Did some work",
        "tools_created": 0,
        "tools_used": 0,
    }
    defaults.update(overrides)
    return defaults


def _make_tool_entry(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "id": "lib.utils:helper",
        "module": "lib/utils.py",
        "function": "helper",
        "description": "A helper function",
        "tags": [],
        "created_by": "SES-001",
        "used_by": [],
        "last_used": "",
        "created_at": "2026-01-01T00:00:00Z",
    }
    defaults.update(overrides)
    return defaults


# ===========================================================================
# TestUnderusedTools
# ===========================================================================


class TestUnderusedTools:
    def test_tool_never_used(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        _write_tools_yaml(tmp_path, [_make_tool_entry(used_by=[])])
        suggestions = suggest_tools(tmp_path)
        underused = [s for s in suggestions if s.kind == "underused"]
        assert len(underused) == 1
        assert "lib.utils:helper" in underused[0].message

    def test_tool_used_only_by_creator(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        _write_tools_yaml(tmp_path, [_make_tool_entry(created_by="SES-001", used_by=["SES-001"])])
        suggestions = suggest_tools(tmp_path)
        underused = [s for s in suggestions if s.kind == "underused"]
        assert len(underused) == 1

    def test_tool_used_by_others_not_flagged(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        _write_tools_yaml(
            tmp_path,
            [_make_tool_entry(created_by="SES-001", used_by=["SES-001", "SES-002"])],
        )
        suggestions = suggest_tools(tmp_path)
        underused = [s for s in suggestions if s.kind == "underused"]
        assert len(underused) == 0

    def test_no_tools_no_underused(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        suggestions = suggest_tools(tmp_path)
        underused = [s for s in suggestions if s.kind == "underused"]
        assert len(underused) == 0


# ===========================================================================
# TestUnawareSessions
# ===========================================================================


class TestUnawareSessions:
    def test_session_with_no_tool_interaction(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        _write_session_index(
            tmp_path,
            [_make_session_row(tools_created=0, tools_used=0)],
        )
        suggestions = suggest_tools(tmp_path)
        unaware = [s for s in suggestions if s.kind == "unaware_session"]
        assert len(unaware) == 1
        assert "SES-001" in unaware[0].message

    def test_session_that_created_tools_not_flagged(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        _write_session_index(
            tmp_path,
            [_make_session_row(tools_created=1, tools_used=0)],
        )
        suggestions = suggest_tools(tmp_path)
        unaware = [s for s in suggestions if s.kind == "unaware_session"]
        assert len(unaware) == 0

    def test_session_that_used_tools_not_flagged(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        _write_session_index(
            tmp_path,
            [_make_session_row(tools_created=0, tools_used=1)],
        )
        suggestions = suggest_tools(tmp_path)
        unaware = [s for s in suggestions if s.kind == "unaware_session"]
        assert len(unaware) == 0

    def test_audit_sessions_skipped(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        _write_session_index(
            tmp_path,
            [_make_session_row(mode="audit", tools_created=0, tools_used=0)],
        )
        suggestions = suggest_tools(tmp_path)
        unaware = [s for s in suggestions if s.kind == "unaware_session"]
        assert len(unaware) == 0

    def test_no_index_no_suggestions(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        suggestions = suggest_tools(tmp_path)
        unaware = [s for s in suggestions if s.kind == "unaware_session"]
        assert len(unaware) == 0

    def test_null_tools_fields_not_flagged(self, tmp_path: Path) -> None:
        """Sessions without tool tracking fields (pre-tool era) are not flagged."""
        (tmp_path / ".exo").mkdir()
        _write_session_index(
            tmp_path,
            [_make_session_row(tools_created=None, tools_used=None)],
        )
        suggestions = suggest_tools(tmp_path)
        unaware = [s for s in suggestions if s.kind == "unaware_session"]
        assert len(unaware) == 0


# ===========================================================================
# TestKeywordMatches
# ===========================================================================


class TestKeywordMatches:
    def test_summary_with_helper_keyword(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        _write_session_index(
            tmp_path,
            [_make_session_row(summary="Built a helper function for CSV parsing", tools_created=0)],
        )
        suggestions = suggest_tools(tmp_path)
        keyword = [s for s in suggestions if s.kind == "keyword_match"]
        assert len(keyword) == 1
        assert "helper" in keyword[0].details["matched_keywords"]

    def test_summary_with_multiple_keywords(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        _write_session_index(
            tmp_path,
            [_make_session_row(summary="Created a parser and validator for input data", tools_created=0)],
        )
        suggestions = suggest_tools(tmp_path)
        keyword = [s for s in suggestions if s.kind == "keyword_match"]
        assert len(keyword) == 1
        assert "parser" in keyword[0].details["matched_keywords"]
        assert "validator" in keyword[0].details["matched_keywords"]

    def test_session_that_registered_tools_not_flagged(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        _write_session_index(
            tmp_path,
            [_make_session_row(summary="Built a helper function", tools_created=1)],
        )
        suggestions = suggest_tools(tmp_path)
        keyword = [s for s in suggestions if s.kind == "keyword_match"]
        assert len(keyword) == 0

    def test_summary_without_keywords(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        _write_session_index(
            tmp_path,
            [_make_session_row(summary="Fixed a bug in the login flow", tools_created=0)],
        )
        suggestions = suggest_tools(tmp_path)
        keyword = [s for s in suggestions if s.kind == "keyword_match"]
        assert len(keyword) == 0

    def test_case_insensitive_matching(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        _write_session_index(
            tmp_path,
            [_make_session_row(summary="Created a PARSER for JSON", tools_created=0)],
        )
        suggestions = suggest_tools(tmp_path)
        keyword = [s for s in suggestions if s.kind == "keyword_match"]
        assert len(keyword) == 1

    def test_audit_sessions_skipped(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        _write_session_index(
            tmp_path,
            [_make_session_row(mode="audit", summary="Reviewed the helper code", tools_created=0)],
        )
        suggestions = suggest_tools(tmp_path)
        keyword = [s for s in suggestions if s.kind == "keyword_match"]
        assert len(keyword) == 0


# ===========================================================================
# TestSerialization
# ===========================================================================


class TestSerialization:
    def test_suggestion_to_dict(self) -> None:
        s = Suggestion(kind="underused", message="Tool X is underused", details={"tool_id": "X"})
        d = suggestion_to_dict(s)
        assert d["kind"] == "underused"
        assert d["message"] == "Tool X is underused"
        assert d["details"]["tool_id"] == "X"

    def test_suggestions_to_list(self) -> None:
        suggestions = [
            Suggestion(kind="underused", message="M1", details={}),
            Suggestion(kind="keyword_match", message="M2", details={}),
        ]
        result = suggestions_to_list(suggestions)
        assert len(result) == 2
        assert result[0]["kind"] == "underused"
        assert result[1]["kind"] == "keyword_match"


# ===========================================================================
# TestFormatHuman
# ===========================================================================


class TestFormatHuman:
    def test_empty_suggestions(self) -> None:
        text = format_suggestions_human([])
        assert "none" in text.lower()

    def test_with_suggestions(self) -> None:
        suggestions = [
            Suggestion(kind="underused", message="Tool X never used", details={}),
            Suggestion(kind="keyword_match", message="Session mentions parser", details={}),
        ]
        text = format_suggestions_human(suggestions)
        assert "2 found" in text
        assert "[UNDERUSED]" in text
        assert "[KEYWORD]" in text

    def test_unaware_label(self) -> None:
        suggestions = [Suggestion(kind="unaware_session", message="Session ignored tools", details={})]
        text = format_suggestions_human(suggestions)
        assert "[UNAWARE]" in text


# ===========================================================================
# TestCombinedSuggestions
# ===========================================================================


class TestCombinedSuggestions:
    def test_all_types_together(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        _write_tools_yaml(tmp_path, [_make_tool_entry(used_by=[])])
        _write_session_index(
            tmp_path,
            [
                _make_session_row(
                    session_id="SES-A",
                    summary="Built a helper utility",
                    tools_created=0,
                    tools_used=0,
                ),
                _make_session_row(
                    session_id="SES-B",
                    summary="Fixed a bug",
                    tools_created=0,
                    tools_used=0,
                ),
            ],
        )
        suggestions = suggest_tools(tmp_path)
        kinds = {s.kind for s in suggestions}
        assert "underused" in kinds
        assert "unaware_session" in kinds
        assert "keyword_match" in kinds

    def test_ordering_underused_first(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        _write_tools_yaml(tmp_path, [_make_tool_entry(used_by=[])])
        _write_session_index(
            tmp_path,
            [_make_session_row(summary="Built helper", tools_created=0, tools_used=0)],
        )
        suggestions = suggest_tools(tmp_path)
        assert suggestions[0].kind == "underused"

    def test_empty_repo(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        suggestions = suggest_tools(tmp_path)
        assert suggestions == []


# ===========================================================================
# TestUtilityKeywords
# ===========================================================================


class TestUtilityKeywords:
    def test_keywords_are_lowercase(self) -> None:
        for kw in UTILITY_KEYWORDS:
            assert kw == kw.lower(), f"keyword '{kw}' is not lowercase"

    def test_keywords_are_unique(self) -> None:
        assert len(UTILITY_KEYWORDS) == len(set(UTILITY_KEYWORDS))

    def test_common_patterns_covered(self) -> None:
        essential = {"helper", "utility", "parser", "validator", "handler", "wrapper"}
        assert essential.issubset(set(UTILITY_KEYWORDS))


# ===========================================================================
# TestCLI
# ===========================================================================


class TestCLISuggest:
    def _run_exo(self, repo: Path, *args: str) -> dict[str, Any]:
        import subprocess

        result = subprocess.run(
            ["exo", "--format", "json", "--repo", str(repo), *args],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"exo failed: {result.stderr}")
        return json.loads(result.stdout)

    def test_suggest_empty(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        result = self._run_exo(tmp_path, "tool-suggest")
        assert result["ok"] is True
        assert result["data"]["count"] == 0

    def test_suggest_finds_underused(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        _write_tools_yaml(tmp_path, [_make_tool_entry(used_by=[])])
        result = self._run_exo(tmp_path, "tool-suggest")
        assert result["ok"] is True
        assert result["data"]["count"] >= 1
        kinds = {s["kind"] for s in result["data"]["suggestions"]}
        assert "underused" in kinds
