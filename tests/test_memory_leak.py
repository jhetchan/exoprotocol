"""Tests for private memory leak detection.

Covers:
- MemoryLeakWarning dataclass
- detect_memory_leaks (config loading, path expansion, mtime comparison, reflection check)
- format_memory_leak_warnings
- warnings_to_dicts
- Session-finish integration (memento, result dict, banner)
- Adapter directive (Operational Learnings section)
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from exo.kernel import governance as governance_mod
from exo.kernel import tickets as tickets_mod
from exo.stdlib.memory_leak import (
    MemoryLeakWarning,
    detect_memory_leaks,
    format_memory_leak_warnings,
    warnings_to_dicts,
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


def _create_reflection(repo: Path, ref_id: str, session_id: str) -> None:
    """Create a minimal reflection YAML file."""
    ref_dir = repo / ".exo" / "memory" / "reflections"
    ref_dir.mkdir(parents=True, exist_ok=True)
    ref_data = {
        "id": ref_id,
        "pattern": "test pattern",
        "insight": "test insight",
        "severity": "medium",
        "scope": "global",
        "actor": "agent:test",
        "session_id": session_id,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "status": "active",
        "tags": [],
        "hit_count": 0,
    }
    (ref_dir / f"{ref_id}.yaml").write_text(yaml.dump(ref_data), encoding="utf-8")


# ---------------------------------------------------------------------------
# MemoryLeakWarning dataclass
# ---------------------------------------------------------------------------
class TestMemoryLeakWarning:
    def test_fields_correct(self) -> None:
        w = MemoryLeakWarning(
            path="/home/user/.claude/MEMORY.md",
            mtime="2025-01-01T12:00:00+00:00",
            session_started="2025-01-01T11:00:00+00:00",
            has_reflection=False,
            message="Private memory written without reflection",
        )
        assert w.path == "/home/user/.claude/MEMORY.md"
        assert w.mtime == "2025-01-01T12:00:00+00:00"
        assert w.session_started == "2025-01-01T11:00:00+00:00"
        assert w.has_reflection is False
        assert w.message == "Private memory written without reflection"

    def test_message_non_empty(self) -> None:
        w = MemoryLeakWarning(
            path="/tmp/mem",
            mtime="2025-01-01T12:00:00+00:00",
            session_started="2025-01-01T11:00:00+00:00",
            has_reflection=False,
            message="test",
        )
        assert len(w.message) > 0


# ---------------------------------------------------------------------------
# detect_memory_leaks
# ---------------------------------------------------------------------------
class TestDetectMemoryLeaks:
    def test_no_watch_paths_no_warnings(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        config = {"private_memory": {"watch_paths": [], "enabled": True}}
        result = detect_memory_leaks(repo, session_id="S-001", started_at="2025-01-01T00:00:00+00:00", config=config)
        assert result == []

    def test_watch_path_not_exists_no_warnings(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        config = {"private_memory": {"watch_paths": [str(tmp_path / "nonexistent.md")], "enabled": True}}
        result = detect_memory_leaks(repo, session_id="S-001", started_at="2025-01-01T00:00:00+00:00", config=config)
        assert result == []

    def test_watch_path_not_modified_since_start_no_warnings(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        mem_file = tmp_path / "agent_memory.md"
        mem_file.write_text("old content", encoding="utf-8")
        # Set mtime to the past
        old_time = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(mem_file, (old_time, old_time))

        config = {"private_memory": {"watch_paths": [str(mem_file)], "enabled": True}}
        result = detect_memory_leaks(repo, session_id="S-001", started_at="2025-06-01T00:00:00+00:00", config=config)
        assert result == []

    def test_modified_after_start_no_reflections_warning(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        mem_file = tmp_path / "agent_memory.md"
        mem_file.write_text("new learning saved", encoding="utf-8")
        # mtime is "now" which is after started_at
        config = {"private_memory": {"watch_paths": [str(mem_file)], "enabled": True}}
        result = detect_memory_leaks(repo, session_id="S-001", started_at="2020-01-01T00:00:00+00:00", config=config)
        assert len(result) == 1
        assert result[0].has_reflection is False
        assert "exo reflect" in result[0].message

    def test_modified_after_start_with_reflection_no_warning(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        mem_file = tmp_path / "agent_memory.md"
        mem_file.write_text("saved learning", encoding="utf-8")
        # Create a reflection for this session
        _create_reflection(repo, "REF-001", session_id="S-001")

        config = {"private_memory": {"watch_paths": [str(mem_file)], "enabled": True}}
        result = detect_memory_leaks(repo, session_id="S-001", started_at="2020-01-01T00:00:00+00:00", config=config)
        assert result == []

    def test_modified_reflection_wrong_session_warning(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        mem_file = tmp_path / "agent_memory.md"
        mem_file.write_text("saved learning", encoding="utf-8")
        # Reflection exists but for a DIFFERENT session
        _create_reflection(repo, "REF-001", session_id="S-OTHER")

        config = {"private_memory": {"watch_paths": [str(mem_file)], "enabled": True}}
        result = detect_memory_leaks(repo, session_id="S-001", started_at="2020-01-01T00:00:00+00:00", config=config)
        assert len(result) == 1
        assert result[0].has_reflection is False

    def test_multiple_paths_one_modified(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        mem_new = tmp_path / "new_memory.md"
        mem_new.write_text("new", encoding="utf-8")
        mem_old = tmp_path / "old_memory.md"
        mem_old.write_text("old", encoding="utf-8")
        old_time = datetime(2019, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(mem_old, (old_time, old_time))

        config = {"private_memory": {"watch_paths": [str(mem_new), str(mem_old)], "enabled": True}}
        result = detect_memory_leaks(repo, session_id="S-001", started_at="2020-01-01T00:00:00+00:00", config=config)
        assert len(result) == 1
        assert str(mem_new) in result[0].path

    def test_tilde_expansion(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        # We can't truly test ~ expansion without writing to home dir,
        # but we can verify the function doesn't crash on tilde paths
        # that don't exist after expansion
        config = {"private_memory": {"watch_paths": ["~/nonexistent_exo_test_path.md"], "enabled": True}}
        result = detect_memory_leaks(repo, session_id="S-001", started_at="2020-01-01T00:00:00+00:00", config=config)
        assert result == []

    def test_disabled_no_warnings(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        mem_file = tmp_path / "agent_memory.md"
        mem_file.write_text("new learning", encoding="utf-8")
        config = {"private_memory": {"watch_paths": [str(mem_file)], "enabled": False}}
        result = detect_memory_leaks(repo, session_id="S-001", started_at="2020-01-01T00:00:00+00:00", config=config)
        assert result == []

    def test_malformed_started_at_graceful(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        mem_file = tmp_path / "agent_memory.md"
        mem_file.write_text("content", encoding="utf-8")
        config = {"private_memory": {"watch_paths": [str(mem_file)], "enabled": True}}
        result = detect_memory_leaks(repo, session_id="S-001", started_at="not-a-date", config=config)
        assert result == []


# ---------------------------------------------------------------------------
# format_memory_leak_warnings
# ---------------------------------------------------------------------------
class TestFormatMemoryLeakWarnings:
    def test_empty_returns_empty_string(self) -> None:
        assert format_memory_leak_warnings([]) == ""

    def test_single_warning_markdown_section(self) -> None:
        w = MemoryLeakWarning(
            path="/tmp/mem.md",
            mtime="2025-01-01T12:00:00+00:00",
            session_started="2025-01-01T11:00:00+00:00",
            has_reflection=False,
            message="Private memory written without reflection",
        )
        result = format_memory_leak_warnings([w])
        assert "## Private Memory Warnings" in result
        assert "Private memory written without reflection" in result

    def test_multiple_warnings_all_listed(self) -> None:
        warnings = [
            MemoryLeakWarning(
                path=f"/tmp/mem{i}.md",
                mtime="2025-01-01T12:00:00+00:00",
                session_started="2025-01-01T11:00:00+00:00",
                has_reflection=False,
                message=f"Warning {i}",
            )
            for i in range(3)
        ]
        result = format_memory_leak_warnings(warnings)
        for i in range(3):
            assert f"Warning {i}" in result


# ---------------------------------------------------------------------------
# warnings_to_dicts
# ---------------------------------------------------------------------------
class TestWarningsToDicts:
    def test_round_trips_correctly(self) -> None:
        w = MemoryLeakWarning(
            path="/tmp/mem.md",
            mtime="2025-01-01T12:00:00+00:00",
            session_started="2025-01-01T11:00:00+00:00",
            has_reflection=False,
            message="test message",
        )
        dicts = warnings_to_dicts([w])
        assert len(dicts) == 1
        d = dicts[0]
        assert d["path"] == "/tmp/mem.md"
        assert d["mtime"] == "2025-01-01T12:00:00+00:00"
        assert d["session_started"] == "2025-01-01T11:00:00+00:00"
        assert d["has_reflection"] is False
        assert d["message"] == "test message"

    def test_empty_returns_empty_list(self) -> None:
        assert warnings_to_dicts([]) == []


# ---------------------------------------------------------------------------
# Session-finish integration
# ---------------------------------------------------------------------------
class TestSessionFinishIntegration:
    def _start_session(self, repo: Path, *, mode: str = "work") -> dict[str, Any]:
        from exo.orchestrator.session import AgentSessionManager

        tickets_mod.save_ticket(
            repo,
            {
                "id": "TICKET-001",
                "title": "Test ticket",
                "intent": "Do testing",
                "status": "todo",
            },
        )
        tickets_mod.acquire_lock(repo, "TICKET-001", owner="agent:test", role="developer")
        mgr = AgentSessionManager(repo, actor="agent:test")
        return mgr.start(
            ticket_id="TICKET-001",
            vendor="test",
            model="test-model",
            context_window_tokens=100000,
            mode=mode,
        )

    def test_leak_detected_in_memento(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        # Write config with watch_paths
        mem_file = tmp_path / "private_mem.md"
        config_path = repo / ".exo" / "config.yaml"
        config_data = {"private_memory": {"watch_paths": [str(mem_file)], "enabled": True}}
        config_path.write_text(yaml.dump(config_data), encoding="utf-8")

        self._start_session(repo)

        # Simulate agent writing to private memory during session
        time.sleep(0.05)
        mem_file.write_text("learned something privately", encoding="utf-8")

        from exo.orchestrator.session import AgentSessionManager

        mgr = AgentSessionManager(repo, actor="agent:test")
        result = mgr.finish(summary="did some work", skip_check=True, break_glass_reason="test")

        # Read the memento and check for warning
        memento_path = repo / result["memento_path"]
        memento_text = memento_path.read_text(encoding="utf-8")
        assert "Private Memory Warnings" in memento_text

    def test_leak_detected_in_result_dict(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        mem_file = tmp_path / "private_mem.md"
        config_path = repo / ".exo" / "config.yaml"
        config_data = {"private_memory": {"watch_paths": [str(mem_file)], "enabled": True}}
        config_path.write_text(yaml.dump(config_data), encoding="utf-8")

        self._start_session(repo)
        time.sleep(0.05)
        mem_file.write_text("learned something privately", encoding="utf-8")

        from exo.orchestrator.session import AgentSessionManager

        mgr = AgentSessionManager(repo, actor="agent:test")
        result = mgr.finish(summary="did work", skip_check=True, break_glass_reason="test")
        assert "memory_leak_warnings" in result
        assert len(result["memory_leak_warnings"]) == 1
        assert "exo reflect" in result["memory_leak_warnings"][0]["message"]

    def test_leak_detected_in_banner(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        mem_file = tmp_path / "private_mem.md"
        config_path = repo / ".exo" / "config.yaml"
        config_data = {"private_memory": {"watch_paths": [str(mem_file)], "enabled": True}}
        config_path.write_text(yaml.dump(config_data), encoding="utf-8")

        self._start_session(repo)
        time.sleep(0.05)
        mem_file.write_text("learned something privately", encoding="utf-8")

        from exo.orchestrator.session import AgentSessionManager

        mgr = AgentSessionManager(repo, actor="agent:test")
        result = mgr.finish(summary="did work", skip_check=True, break_glass_reason="test")
        assert "!" in result["exo_banner"]

    def test_audit_mode_no_check(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        mem_file = tmp_path / "private_mem.md"
        config_path = repo / ".exo" / "config.yaml"
        config_data = {"private_memory": {"watch_paths": [str(mem_file)], "enabled": True}}
        config_path.write_text(yaml.dump(config_data), encoding="utf-8")

        self._start_session(repo, mode="audit")
        time.sleep(0.05)
        mem_file.write_text("learned something privately", encoding="utf-8")

        from exo.orchestrator.session import AgentSessionManager

        mgr = AgentSessionManager(repo, actor="agent:test")
        result = mgr.finish(summary="audited", skip_check=True, break_glass_reason="test")
        assert "memory_leak_warnings" not in result

    def test_no_watch_paths_no_warning_fields(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        self._start_session(repo)

        from exo.orchestrator.session import AgentSessionManager

        mgr = AgentSessionManager(repo, actor="agent:test")
        result = mgr.finish(summary="clean session", skip_check=True, break_glass_reason="test")
        assert "memory_leak_warnings" not in result


# ---------------------------------------------------------------------------
# Adapter directive
# ---------------------------------------------------------------------------
class TestAdapterDirective:
    def _generate_adapter(self, tmp_path: Path, target: str) -> str:
        from exo.stdlib.adapters import generate_agents, generate_claude, generate_cursor

        repo = _bootstrap_repo(tmp_path)
        lock_path = repo / ".exo" / "governance.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        config: dict[str, Any] = {}

        if target == "claude":
            return generate_claude(repo, lock, config)
        elif target == "cursor":
            return generate_cursor(repo, lock, config)
        elif target == "agents":
            return generate_agents(repo, lock, config)
        raise ValueError(f"Unknown target: {target}")

    def test_claude_contains_operational_learnings(self, tmp_path: Path) -> None:
        content = self._generate_adapter(tmp_path, "claude")
        assert "### Operational Learnings" in content
        assert "exo reflect" in content

    def test_cursor_contains_exo_reflect(self, tmp_path: Path) -> None:
        content = self._generate_adapter(tmp_path, "cursor")
        assert "exo reflect" in content

    def test_directive_includes_not_private_memory(self, tmp_path: Path) -> None:
        content = self._generate_adapter(tmp_path, "agents")
        assert "NOT your private memory" in content
