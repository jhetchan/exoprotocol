"""Tests for the Tool Awareness Registry.

Covers: schema, load/save, register, search, remove, mark-used, serialization,
        human formatting, CLI integration, bootstrap injection, and session-finish integration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from exo.kernel import governance as governance_mod
from exo.kernel import tickets
from exo.kernel.errors import ExoError
from exo.kernel.utils import dump_yaml
from exo.stdlib.tools import (
    TOOLS_PATH,
    ToolDef,
    _derive_tool_id,
    format_bootstrap_tools,
    format_tools_human,
    format_tools_memento,
    load_tools,
    mark_tool_used,
    register_tool,
    remove_tool,
    search_tools,
    tool_to_dict,
    tools_session_summary,
    tools_to_list,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_tools_yaml(repo: Path, tools: list[dict[str, Any]]) -> Path:
    """Write a tools.yaml file to the repo."""
    tools_path = repo / TOOLS_PATH
    tools_path.parent.mkdir(parents=True, exist_ok=True)
    dump_yaml(tools_path, {"tools": tools})
    return tools_path


def _make_tool_entry(**overrides: Any) -> dict[str, Any]:
    """Create a tool entry dict with sensible defaults."""
    defaults: dict[str, Any] = {
        "id": "lib.utils:helper",
        "module": "lib/utils.py",
        "function": "helper",
        "description": "A helper function",
        "signature": "(x: int) -> str",
        "tags": ["utility"],
        "created_by": "session-001",
        "used_by": [],
        "last_used": "",
        "created_at": "2026-01-01T00:00:00Z",
    }
    defaults.update(overrides)
    return defaults


# ===========================================================================
# TestDeriveToolId
# ===========================================================================


class TestDeriveToolId:
    def test_basic_derivation(self) -> None:
        assert _derive_tool_id("lib/parsers/csv_utils.py", "parse_csv") == "lib.parsers.csv_utils:parse_csv"

    def test_no_py_extension(self) -> None:
        assert _derive_tool_id("lib/parsers/csv_utils", "parse_csv") == "lib.parsers.csv_utils:parse_csv"

    def test_single_file(self) -> None:
        assert _derive_tool_id("utils.py", "helper") == "utils:helper"

    def test_nested_path(self) -> None:
        assert _derive_tool_id("src/a/b/c.py", "fn") == "src.a.b.c:fn"

    def test_backslash_path(self) -> None:
        assert _derive_tool_id("lib\\utils.py", "fn") == "lib.utils:fn"


# ===========================================================================
# TestLoadTools
# ===========================================================================


class TestLoadTools:
    def test_load_basic(self, tmp_path: Path) -> None:
        _write_tools_yaml(tmp_path, [_make_tool_entry()])
        tools = load_tools(tmp_path)
        assert len(tools) == 1
        assert tools[0].id == "lib.utils:helper"
        assert tools[0].module == "lib/utils.py"
        assert tools[0].function == "helper"
        assert tools[0].description == "A helper function"
        assert tools[0].signature == "(x: int) -> str"
        assert tools[0].tags == ("utility",)
        assert tools[0].created_by == "session-001"

    def test_load_empty_file(self, tmp_path: Path) -> None:
        _write_tools_yaml(tmp_path, [])
        tools = load_tools(tmp_path)
        assert tools == []

    def test_load_missing_file_returns_empty(self, tmp_path: Path) -> None:
        tools = load_tools(tmp_path)
        assert tools == []

    def test_load_auto_derives_id(self, tmp_path: Path) -> None:
        entry = _make_tool_entry()
        del entry["id"]
        _write_tools_yaml(tmp_path, [entry])
        tools = load_tools(tmp_path)
        assert tools[0].id == "lib.utils:helper"

    def test_load_with_tags(self, tmp_path: Path) -> None:
        _write_tools_yaml(tmp_path, [_make_tool_entry(tags=["csv", "parsing", "data"])])
        tools = load_tools(tmp_path)
        assert tools[0].tags == ("csv", "parsing", "data")

    def test_load_with_used_by(self, tmp_path: Path) -> None:
        _write_tools_yaml(tmp_path, [_make_tool_entry(used_by=["sess-1", "sess-2"])])
        tools = load_tools(tmp_path)
        assert tools[0].used_by == ("sess-1", "sess-2")

    def test_invalid_manifest_not_list_raises(self, tmp_path: Path) -> None:
        tools_path = tmp_path / TOOLS_PATH
        tools_path.parent.mkdir(parents=True, exist_ok=True)
        dump_yaml(tools_path, {"tools": "not_a_list"})
        with pytest.raises(ExoError, match="TOOLS_MANIFEST_INVALID"):
            load_tools(tmp_path)

    def test_duplicate_id_raises(self, tmp_path: Path) -> None:
        _write_tools_yaml(tmp_path, [_make_tool_entry(), _make_tool_entry()])
        with pytest.raises(ExoError, match="TOOLS_DUPLICATE_ID"):
            load_tools(tmp_path)

    def test_non_dict_entry_raises(self, tmp_path: Path) -> None:
        _write_tools_yaml(tmp_path, ["not_a_dict"])
        with pytest.raises(ExoError, match="TOOLS_ENTRY_INVALID"):
            load_tools(tmp_path)


# ===========================================================================
# TestRegisterTool
# ===========================================================================


class TestRegisterTool:
    def test_register_basic(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        tool = register_tool(
            tmp_path,
            module="lib/utils.py",
            function="helper",
            description="A helper function",
        )
        assert tool.id == "lib.utils:helper"
        assert tool.module == "lib/utils.py"
        assert tool.function == "helper"
        assert tool.description == "A helper function"
        assert tool.created_at != ""
        # Verify file was written
        tools = load_tools(tmp_path)
        assert len(tools) == 1
        assert tools[0].id == tool.id

    def test_register_creates_file_if_missing(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        assert not (tmp_path / TOOLS_PATH).exists()
        register_tool(
            tmp_path,
            module="lib/utils.py",
            function="helper",
            description="A helper function",
        )
        assert (tmp_path / TOOLS_PATH).exists()

    def test_register_with_tags(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        tool = register_tool(
            tmp_path,
            module="lib/csv.py",
            function="parse",
            description="Parse CSV files",
            tags=["csv", "parsing"],
        )
        assert tool.tags == ("csv", "parsing")

    def test_register_with_signature(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        tool = register_tool(
            tmp_path,
            module="lib/csv.py",
            function="parse",
            description="Parse CSV files",
            signature="(path: Path) -> list[dict]",
        )
        assert tool.signature == "(path: Path) -> list[dict]"

    def test_register_with_session_id(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        tool = register_tool(
            tmp_path,
            module="lib/csv.py",
            function="parse",
            description="Parse CSV",
            session_id="session-abc",
        )
        assert tool.created_by == "session-abc"

    def test_register_duplicate_raises(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        register_tool(tmp_path, module="lib/utils.py", function="helper", description="First")
        with pytest.raises(ExoError, match="TOOLS_DUPLICATE_ID"):
            register_tool(tmp_path, module="lib/utils.py", function="helper", description="Second")

    def test_register_missing_module_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ExoError, match="TOOLS_MODULE_REQUIRED"):
            register_tool(tmp_path, module="", function="helper", description="desc")

    def test_register_missing_function_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ExoError, match="TOOLS_FUNCTION_REQUIRED"):
            register_tool(tmp_path, module="lib/utils.py", function="", description="desc")

    def test_register_missing_description_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ExoError, match="TOOLS_DESCRIPTION_REQUIRED"):
            register_tool(tmp_path, module="lib/utils.py", function="helper", description="")


# ===========================================================================
# TestSearchTools
# ===========================================================================


class TestSearchTools:
    def _setup_tools(self, tmp_path: Path) -> None:
        _write_tools_yaml(
            tmp_path,
            [
                _make_tool_entry(
                    id="lib.parsers.csv:parse_csv",
                    module="lib/parsers/csv.py",
                    function="parse_csv",
                    description="Parse CSV files with column selection",
                    tags=["csv", "parsing", "data"],
                ),
                _make_tool_entry(
                    id="lib.parsers.json:parse_json",
                    module="lib/parsers/json.py",
                    function="parse_json",
                    description="Parse JSON files with schema validation",
                    tags=["json", "parsing", "data"],
                ),
                _make_tool_entry(
                    id="lib.notify.slack:send_message",
                    module="lib/notify/slack.py",
                    function="send_message",
                    description="Send Slack notification to a channel",
                    tags=["slack", "notification"],
                ),
            ],
        )

    def test_search_by_description(self, tmp_path: Path) -> None:
        self._setup_tools(tmp_path)
        results = search_tools(tmp_path, query="column selection")
        assert len(results) == 1
        assert results[0].id == "lib.parsers.csv:parse_csv"

    def test_search_by_tag(self, tmp_path: Path) -> None:
        self._setup_tools(tmp_path)
        results = search_tools(tmp_path, query="slack")
        assert len(results) == 1
        assert results[0].id == "lib.notify.slack:send_message"

    def test_search_by_module(self, tmp_path: Path) -> None:
        self._setup_tools(tmp_path)
        results = search_tools(tmp_path, query="notify")
        assert len(results) == 1
        assert results[0].id == "lib.notify.slack:send_message"

    def test_search_by_function(self, tmp_path: Path) -> None:
        self._setup_tools(tmp_path)
        results = search_tools(tmp_path, query="parse_json")
        assert len(results) == 1
        assert results[0].id == "lib.parsers.json:parse_json"

    def test_search_multi_word_scores_higher(self, tmp_path: Path) -> None:
        self._setup_tools(tmp_path)
        results = search_tools(tmp_path, query="csv parsing")
        # CSV parser matches both "csv" and "parsing", JSON matches only "parsing"
        assert len(results) == 2
        assert results[0].id == "lib.parsers.csv:parse_csv"

    def test_search_no_match(self, tmp_path: Path) -> None:
        self._setup_tools(tmp_path)
        results = search_tools(tmp_path, query="kubernetes deploy")
        assert results == []

    def test_search_empty_query(self, tmp_path: Path) -> None:
        self._setup_tools(tmp_path)
        results = search_tools(tmp_path, query="")
        assert len(results) == 3

    def test_search_case_insensitive(self, tmp_path: Path) -> None:
        self._setup_tools(tmp_path)
        results = search_tools(tmp_path, query="CSV")
        assert len(results) == 1
        assert results[0].id == "lib.parsers.csv:parse_csv"


# ===========================================================================
# TestRemoveTool
# ===========================================================================


class TestRemoveTool:
    def test_remove_existing(self, tmp_path: Path) -> None:
        _write_tools_yaml(
            tmp_path,
            [
                _make_tool_entry(id="a:fn1", module="a.py", function="fn1"),
                _make_tool_entry(id="b:fn2", module="b.py", function="fn2"),
            ],
        )
        remove_tool(tmp_path, tool_id="a:fn1")
        tools = load_tools(tmp_path)
        assert len(tools) == 1
        assert tools[0].id == "b:fn2"

    def test_remove_not_found_raises(self, tmp_path: Path) -> None:
        _write_tools_yaml(tmp_path, [_make_tool_entry()])
        with pytest.raises(ExoError, match="TOOLS_NOT_FOUND"):
            remove_tool(tmp_path, tool_id="nonexistent:fn")

    def test_remove_from_empty(self, tmp_path: Path) -> None:
        _write_tools_yaml(tmp_path, [])
        with pytest.raises(ExoError, match="TOOLS_NOT_FOUND"):
            remove_tool(tmp_path, tool_id="any:fn")


# ===========================================================================
# TestMarkToolUsed
# ===========================================================================


class TestMarkToolUsed:
    def test_mark_used_basic(self, tmp_path: Path) -> None:
        _write_tools_yaml(tmp_path, [_make_tool_entry(used_by=[])])
        tool = mark_tool_used(tmp_path, tool_id="lib.utils:helper", session_id="session-new")
        assert "session-new" in tool.used_by
        assert tool.last_used != ""

    def test_mark_used_dedup(self, tmp_path: Path) -> None:
        _write_tools_yaml(tmp_path, [_make_tool_entry(used_by=["session-1"])])
        tool = mark_tool_used(tmp_path, tool_id="lib.utils:helper", session_id="session-1")
        assert tool.used_by.count("session-1") == 1

    def test_mark_used_not_found_raises(self, tmp_path: Path) -> None:
        _write_tools_yaml(tmp_path, [_make_tool_entry()])
        with pytest.raises(ExoError, match="TOOLS_NOT_FOUND"):
            mark_tool_used(tmp_path, tool_id="nonexistent:fn", session_id="session-1")

    def test_mark_used_no_session(self, tmp_path: Path) -> None:
        _write_tools_yaml(tmp_path, [_make_tool_entry(used_by=[])])
        tool = mark_tool_used(tmp_path, tool_id="lib.utils:helper", session_id="")
        assert tool.last_used != ""
        assert tool.used_by == ()


# ===========================================================================
# TestToolsToList
# ===========================================================================


class TestToolsToList:
    def test_round_trip(self, tmp_path: Path) -> None:
        _write_tools_yaml(tmp_path, [_make_tool_entry(tags=["a", "b"], used_by=["s1"])])
        tools = load_tools(tmp_path)
        result = tools_to_list(tools)
        assert len(result) == 1
        assert result[0]["id"] == "lib.utils:helper"
        assert result[0]["tags"] == ["a", "b"]
        assert result[0]["used_by"] == ["s1"]

    def test_empty(self) -> None:
        assert tools_to_list([]) == []


class TestToolToDict:
    def test_basic(self) -> None:
        tool = ToolDef(
            id="lib.utils:fn",
            module="lib/utils.py",
            function="fn",
            description="desc",
        )
        d = tool_to_dict(tool)
        assert d["id"] == "lib.utils:fn"
        assert d["tags"] == []
        assert d["used_by"] == []


# ===========================================================================
# TestFormatToolsHuman
# ===========================================================================


class TestFormatToolsHuman:
    def test_with_tools(self) -> None:
        tools = [
            ToolDef(
                id="lib.utils:fn",
                module="lib/utils.py",
                function="fn",
                description="A utility function",
                used_by=("s1", "s2"),
            ),
        ]
        text = format_tools_human(tools)
        assert "1 registered" in text
        assert "lib.utils:fn" in text
        assert "A utility function" in text
        assert "2 session(s)" in text

    def test_empty(self) -> None:
        text = format_tools_human([])
        assert "(none registered)" in text

    def test_with_tags_and_signature(self) -> None:
        tools = [
            ToolDef(
                id="lib.csv:parse",
                module="lib/csv.py",
                function="parse",
                description="Parse CSV",
                signature="(path: Path) -> list",
                tags=("csv", "parsing"),
            ),
        ]
        text = format_tools_human(tools)
        assert "[csv, parsing]" in text
        assert "sig: (path: Path) -> list" in text


# ===========================================================================
# TestCLITools
# ===========================================================================


class TestCLITools:
    """CLI integration tests using subprocess."""

    def _run_exo(self, repo: Path, *args: str) -> dict[str, Any]:
        import json
        import subprocess

        result = subprocess.run(
            ["exo", "--format", "json", "--repo", str(repo), *args],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"exo failed: {result.stderr}")
        return json.loads(result.stdout)

    def test_tools_empty(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        result = self._run_exo(tmp_path, "tools")
        assert result["ok"] is True
        assert result["data"]["count"] == 0

    def test_tool_register_and_list(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        result = self._run_exo(
            tmp_path,
            "tool-register",
            "lib/utils.py",
            "helper",
            "--description",
            "A helper function",
            "--tag",
            "utility",
        )
        assert result["ok"] is True
        assert result["data"]["tool"]["id"] == "lib.utils:helper"

        result = self._run_exo(tmp_path, "tools")
        assert result["data"]["count"] == 1

    def test_tools_tag_filter(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        self._run_exo(tmp_path, "tool-register", "lib/csv.py", "parse", "--description", "CSV parser", "--tag", "csv")
        self._run_exo(
            tmp_path, "tool-register", "lib/json.py", "parse", "--description", "JSON parser", "--tag", "json"
        )
        result = self._run_exo(tmp_path, "tools", "--tag", "csv")
        assert result["data"]["count"] == 1
        assert result["data"]["tools"][0]["id"] == "lib.csv:parse"

    def test_tool_search(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        self._run_exo(tmp_path, "tool-register", "lib/csv.py", "parse", "--description", "Parse CSV files")
        result = self._run_exo(tmp_path, "tool-search", "csv")
        assert result["data"]["count"] == 1

    def test_tool_remove(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        self._run_exo(tmp_path, "tool-register", "lib/utils.py", "helper", "--description", "Helper")
        result = self._run_exo(tmp_path, "tool-remove", "lib.utils:helper")
        assert result["ok"] is True
        assert result["data"]["removed"] == "lib.utils:helper"
        result = self._run_exo(tmp_path, "tools")
        assert result["data"]["count"] == 0

    def test_tool_register_missing_description_fails(self, tmp_path: Path) -> None:
        import subprocess

        (tmp_path / ".exo").mkdir()
        result = subprocess.run(
            ["exo", "--format", "json", "--repo", str(tmp_path), "tool-register", "lib/utils.py", "helper"],
            capture_output=True,
            text=True,
        )
        # argparse should reject missing --description
        assert result.returncode != 0


# ===========================================================================
# Bootstrap Injection (format_bootstrap_tools)
# ===========================================================================


class TestFormatBootstrapTools:
    def test_with_tools(self) -> None:
        tools = [
            ToolDef(
                id="lib.csv:parse",
                module="lib/csv.py",
                function="parse",
                description="Parse CSV files",
                tags=("csv", "parsing"),
            ),
            ToolDef(
                id="lib.json:load",
                module="lib/json.py",
                function="load",
                description="Load JSON data",
            ),
        ]
        lines = format_bootstrap_tools(tools)
        text = "\n".join(lines)
        assert "## Tool Reuse Protocol" in text
        assert "exo tool-search" in text
        assert "exo tool-register" in text
        assert "Registered Tools (2)" in text
        assert "`lib.csv:parse`" in text
        assert "[csv, parsing]" in text
        assert "Parse CSV files" in text
        assert "`lib.json:load`" in text

    def test_empty_tools(self) -> None:
        lines = format_bootstrap_tools([])
        text = "\n".join(lines)
        assert "## Tool Reuse Protocol" in text
        assert "exo tool-search" in text
        assert "No tools registered yet" in text
        assert "Registered Tools" not in text

    def test_signature_included(self) -> None:
        tools = [
            ToolDef(
                id="lib.utils:helper",
                module="lib/utils.py",
                function="helper",
                description="A helper",
                signature="(x: int) -> str",
            ),
        ]
        lines = format_bootstrap_tools(tools)
        text = "\n".join(lines)
        assert "`(x: int) -> str`" in text

    def test_no_signature_no_dash(self) -> None:
        tools = [
            ToolDef(
                id="lib.utils:helper",
                module="lib/utils.py",
                function="helper",
                description="A helper",
            ),
        ]
        lines = format_bootstrap_tools(tools)
        text = "\n".join(lines)
        # Should have description but no signature separator
        assert "A helper" in text
        # No " — `" since signature is empty
        for line in lines:
            if "lib.utils:helper" in line:
                assert "— `" not in line


# ===========================================================================
# Session-finish tool tracking (tools_session_summary + format_tools_memento)
# ===========================================================================


class TestToolsSessionSummary:
    def test_empty_registry(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        _write_tools_yaml(tmp_path, [])
        result = tools_session_summary(tmp_path, session_id="sess-001")
        assert result["total_tools"] == 0
        assert result["tools_created"] == 0
        assert result["tools_used"] == 0
        assert result["created_ids"] == []
        assert result["used_ids"] == []

    def test_session_created_tools(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        _write_tools_yaml(
            tmp_path,
            [
                _make_tool_entry(id="lib.a:fn", module="lib/a.py", function="fn", created_by="sess-001"),
                _make_tool_entry(id="lib.b:fn", module="lib/b.py", function="fn", created_by="sess-002"),
            ],
        )
        result = tools_session_summary(tmp_path, session_id="sess-001")
        assert result["total_tools"] == 2
        assert result["tools_created"] == 1
        assert result["created_ids"] == ["lib.a:fn"]

    def test_session_used_tools(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        _write_tools_yaml(
            tmp_path,
            [
                _make_tool_entry(id="lib.a:fn", module="lib/a.py", function="fn", used_by=["sess-001", "sess-002"]),
                _make_tool_entry(id="lib.b:fn", module="lib/b.py", function="fn", used_by=["sess-003"]),
            ],
        )
        result = tools_session_summary(tmp_path, session_id="sess-001")
        assert result["tools_used"] == 1
        assert result["used_ids"] == ["lib.a:fn"]

    def test_no_tools_file_raises(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        # No tools.yaml — load_tools returns []
        result = tools_session_summary(tmp_path, session_id="sess-001")
        assert result["total_tools"] == 0


class TestFormatToolsMemento:
    def test_basic(self) -> None:
        summary = {
            "total_tools": 5,
            "tools_created": 2,
            "tools_used": 1,
            "created_ids": ["lib.a:fn", "lib.b:fn"],
            "used_ids": ["lib.c:fn"],
        }
        text = format_tools_memento(summary)
        assert "## Tool Registry" in text
        assert "total: 5" in text
        assert "created this session: 2" in text
        assert "used this session: 1" in text
        assert "lib.a:fn" in text
        assert "lib.b:fn" in text
        assert "lib.c:fn" in text

    def test_no_created_or_used(self) -> None:
        summary = {
            "total_tools": 3,
            "tools_created": 0,
            "tools_used": 0,
            "created_ids": [],
            "used_ids": [],
        }
        text = format_tools_memento(summary)
        assert "created this session: 0" in text
        assert "used this session: 0" in text
        assert "created:" not in text
        assert "used:" not in text


# ===========================================================================
# Session Integration Tests (bootstrap + finish)
# ===========================================================================


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


def _create_ticket(repo: Path, ticket_id: str) -> dict[str, Any]:
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
    tickets.acquire_lock(repo, ticket_id, owner="test-agent", role="developer")
    return ticket_data


class TestBootstrapToolInjection:
    def test_start_injects_tool_reuse_protocol_with_tools(self, tmp_path: Path) -> None:
        from exo.orchestrator.session import AgentSessionManager

        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        _write_tools_yaml(
            repo,
            [
                _make_tool_entry(id="lib.csv:parse", module="lib/csv.py", function="parse", description="Parse CSV"),
            ],
        )
        manager = AgentSessionManager(repo, actor="test-agent")
        result = manager.start(ticket_id="TICKET-001")
        bootstrap = result.get("bootstrap_prompt", "")
        assert "## Tool Reuse Protocol" in bootstrap
        assert "exo tool-search" in bootstrap
        assert "Registered Tools (1)" in bootstrap
        assert "`lib.csv:parse`" in bootstrap

    def test_start_injects_empty_protocol_without_yaml(self, tmp_path: Path) -> None:
        from exo.orchestrator.session import AgentSessionManager

        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        # No tools.yaml at all
        manager = AgentSessionManager(repo, actor="test-agent")
        result = manager.start(ticket_id="TICKET-001")
        bootstrap = result.get("bootstrap_prompt", "")
        assert "## Tool Reuse Protocol" in bootstrap
        assert "No tools registered yet" in bootstrap

    def test_no_injection_in_audit_mode(self, tmp_path: Path) -> None:
        from exo.orchestrator.session import AgentSessionManager

        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        _write_tools_yaml(
            repo,
            [_make_tool_entry(id="lib.csv:parse", module="lib/csv.py", function="parse", description="Parse CSV")],
        )
        manager = AgentSessionManager(repo, actor="test-agent")
        result = manager.start(ticket_id="TICKET-001", mode="audit")
        bootstrap = result.get("bootstrap_prompt", "")
        assert "## Tool Reuse Protocol" not in bootstrap

    def test_tool_protocol_appears_after_reflections(self, tmp_path: Path) -> None:
        from exo.orchestrator.session import AgentSessionManager

        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        _write_tools_yaml(repo, [_make_tool_entry()])
        manager = AgentSessionManager(repo, actor="test-agent")
        result = manager.start(ticket_id="TICKET-001")
        bootstrap = result.get("bootstrap_prompt", "")
        tool_pos = bootstrap.index("## Tool Reuse Protocol")
        task_pos = bootstrap.index("## Current Task")
        assert tool_pos < task_pos


class TestFinishToolTracking:
    def test_finish_includes_tools_in_return(self, tmp_path: Path) -> None:
        from exo.orchestrator.session import AgentSessionManager

        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        register_tool(
            repo,
            module="lib/utils.py",
            function="helper",
            description="Helper fn",
            session_id="will-be-replaced",
        )
        manager = AgentSessionManager(repo, actor="test-agent")
        start_result = manager.start(ticket_id="TICKET-001")
        session_id = start_result["session"]["session_id"]

        # Update the tool's created_by to match this session
        from exo.stdlib.tools import _save_tools

        tools = load_tools(repo)
        updated = [
            ToolDef(
                id=t.id,
                module=t.module,
                function=t.function,
                description=t.description,
                signature=t.signature,
                tags=t.tags,
                created_by=session_id,
                used_by=t.used_by,
                last_used=t.last_used,
                created_at=t.created_at,
            )
            for t in tools
        ]
        _save_tools(repo, updated)

        result = manager.finish(
            summary="Finished work",
            ticket_id="TICKET-001",
            skip_check=True,
            break_glass_reason="test",
        )
        assert result["tools"] is not None
        assert result["tools"]["total_tools"] == 1
        assert result["tools"]["tools_created"] == 1
        assert result["tools"]["created_ids"] == ["lib.utils:helper"]

    def test_finish_memento_has_tool_section(self, tmp_path: Path) -> None:
        from exo.orchestrator.session import AgentSessionManager

        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        _write_tools_yaml(
            repo,
            [_make_tool_entry(id="lib.csv:parse", module="lib/csv.py", function="parse")],
        )
        manager = AgentSessionManager(repo, actor="test-agent")
        manager.start(ticket_id="TICKET-001")
        result = manager.finish(
            summary="Done",
            ticket_id="TICKET-001",
            skip_check=True,
            break_glass_reason="test",
        )
        memento_path = repo / result["memento_path"]
        memento = memento_path.read_text(encoding="utf-8")
        assert "## Tool Registry" in memento
        assert "total: 1" in memento

    def test_finish_index_has_tool_fields(self, tmp_path: Path) -> None:
        from exo.orchestrator.session import AgentSessionManager

        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        _write_tools_yaml(repo, [_make_tool_entry()])
        manager = AgentSessionManager(repo, actor="test-agent")
        manager.start(ticket_id="TICKET-001")
        result = manager.finish(
            summary="Done",
            ticket_id="TICKET-001",
            skip_check=True,
            break_glass_reason="test",
        )
        index_path = repo / result["session_index_path"]
        row = json.loads(index_path.read_text(encoding="utf-8").strip().split("\n")[-1])
        assert "tools_created" in row
        assert "tools_used" in row

    def test_finish_no_tools_file_returns_none(self, tmp_path: Path) -> None:
        from exo.orchestrator.session import AgentSessionManager

        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        # No tools.yaml
        manager = AgentSessionManager(repo, actor="test-agent")
        manager.start(ticket_id="TICKET-001")
        result = manager.finish(
            summary="Done",
            ticket_id="TICKET-001",
            skip_check=True,
            break_glass_reason="test",
        )
        assert result["tools"] is None

    def test_finish_audit_mode_skips_tools(self, tmp_path: Path) -> None:
        from exo.orchestrator.session import AgentSessionManager

        repo = _bootstrap_repo(tmp_path)
        _create_ticket(repo, "TICKET-001")
        _write_tools_yaml(repo, [_make_tool_entry()])
        manager = AgentSessionManager(repo, actor="test-agent")
        manager.start(ticket_id="TICKET-001", mode="audit")
        result = manager.finish(
            summary="Done",
            ticket_id="TICKET-001",
            skip_check=True,
            break_glass_reason="test",
        )
        assert result["tools"] is None

    def test_tools_human_output(self, tmp_path: Path) -> None:
        import subprocess

        (tmp_path / ".exo").mkdir()
        subprocess.run(
            [
                "exo",
                "--repo",
                str(tmp_path),
                "tool-register",
                "lib/utils.py",
                "helper",
                "--description",
                "A helper function",
            ],
            capture_output=True,
            text=True,
        )
        result = subprocess.run(
            ["exo", "--repo", str(tmp_path), "tools"],
            capture_output=True,
            text=True,
        )
        assert "lib.utils:helper" in result.stdout
        assert "A helper function" in result.stdout
