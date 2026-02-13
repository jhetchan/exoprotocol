"""Tests for privacy config, portable topic IDs, and gitignore management.

Covers:
- DEFAULT_CONFIG includes privacy section with correct defaults
- Config schema validates privacy keys
- default_topic_id() returns portable ID (no absolute paths)
- Topic IDs in ledger/audit don't contain absolute paths
- exo init manages .gitignore based on privacy.commit_logs
- Upgrade backfills privacy section
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from exo.kernel import governance as governance_mod
from exo.kernel.utils import default_topic_id, dump_yaml, load_yaml
from exo.stdlib.defaults import DEFAULT_CONFIG, DEFAULT_CONSTITUTION


def _bootstrap_repo(tmp_path: Path) -> Path:
    repo = tmp_path
    exo_dir = repo / ".exo"
    exo_dir.mkdir(parents=True, exist_ok=True)
    constitution = DEFAULT_CONSTITUTION
    (exo_dir / "CONSTITUTION.md").write_text(constitution, encoding="utf-8")
    dump_yaml(exo_dir / "config.yaml", DEFAULT_CONFIG)
    for d in ["tickets", "locks", "logs", "memory", "memory/reflections",
              "memory/sessions", "cache", "cache/sessions"]:
        (exo_dir / d).mkdir(parents=True, exist_ok=True)
    governance_mod.compile_constitution(repo)
    return repo


# ── Privacy Config Defaults ──────────────────────────────────────


class TestPrivacyDefaults:

    def test_default_config_has_privacy_section(self) -> None:
        assert "privacy" in DEFAULT_CONFIG

    def test_commit_logs_false_by_default(self) -> None:
        assert DEFAULT_CONFIG["privacy"]["commit_logs"] is False

    def test_redact_local_paths_true_by_default(self) -> None:
        assert DEFAULT_CONFIG["privacy"]["redact_local_paths"] is True


# ── Config Schema Validation ─────────────────────────────────────


class TestPrivacyConfigValidation:

    def test_valid_privacy_section_passes(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        from exo.stdlib.config_schema import validate_config
        result = validate_config(repo)
        privacy_issues = [i for i in result.issues if "privacy" in i.path]
        assert not privacy_issues

    def test_missing_privacy_warns(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        config = load_yaml(repo / ".exo" / "config.yaml")
        del config["privacy"]
        dump_yaml(repo / ".exo" / "config.yaml", config)
        from exo.stdlib.config_schema import validate_config
        result = validate_config(repo)
        privacy_issues = [i for i in result.issues if i.path == "privacy"]
        assert len(privacy_issues) == 1
        assert privacy_issues[0].severity == "warning"

    def test_wrong_type_privacy_key_errors(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        config = load_yaml(repo / ".exo" / "config.yaml")
        config["privacy"]["commit_logs"] = "yes"  # should be bool
        dump_yaml(repo / ".exo" / "config.yaml", config)
        from exo.stdlib.config_schema import validate_config
        result = validate_config(repo)
        privacy_issues = [i for i in result.issues if "commit_logs" in i.path]
        assert len(privacy_issues) == 1
        assert privacy_issues[0].severity == "error"


# ── Portable Topic ID ────────────────────────────────────────────


class TestDefaultTopicId:

    def test_returns_portable_string(self, tmp_path: Path) -> None:
        tid = default_topic_id(tmp_path)
        assert tid == "repo:default"

    def test_no_absolute_path(self, tmp_path: Path) -> None:
        tid = default_topic_id(tmp_path)
        assert "/" not in tid
        assert str(tmp_path) not in tid

    def test_same_for_different_repos(self, tmp_path: Path) -> None:
        """All local repos get the same default topic — portable."""
        repo_a = tmp_path / "a"
        repo_b = tmp_path / "b"
        repo_a.mkdir()
        repo_b.mkdir()
        assert default_topic_id(repo_a) == default_topic_id(repo_b)


# ── Topic IDs in Ledger ──────────────────────────────────────────


class TestLedgerTopicIdsPortable:

    def test_mint_ticket_uses_portable_topic(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        from exo.kernel import ledger
        from exo.kernel.engine import open_session
        from exo.kernel.tickets import mint_ticket

        session = open_session(repo, "human:test")
        ticket = mint_ticket(session, "Write README", {"allow": ["README.md"], "deny": []}, 1)

        # Read ledger and check topic_id
        log_path = repo / ".exo" / "logs" / "ledger.log.jsonl"
        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        for line in lines:
            record = json.loads(line)
            if "topic_id" in record:
                assert record["topic_id"] == "repo:default", \
                    f"Expected portable topic, got: {record['topic_id']}"
                assert str(tmp_path) not in record["topic_id"]

    def test_audit_log_no_absolute_paths(self, tmp_path: Path) -> None:
        """Audit log path fields should be repo-relative, not absolute."""
        repo = _bootstrap_repo(tmp_path)
        log_path = repo / ".exo" / "logs" / "audit.log.jsonl"
        if log_path.exists():
            content = log_path.read_text(encoding="utf-8")
            assert str(tmp_path) not in content


# ── Gitignore Management ─────────────────────────────────────────


class TestGitignoreManagement:

    def test_init_creates_gitignore_entry(self, tmp_path: Path) -> None:
        """exo init should add .exo/logs/ to .gitignore when commit_logs=false."""
        from exo.stdlib.engine import KernelEngine
        engine = KernelEngine(repo=tmp_path, actor="test-agent")
        engine.init(scan=False)
        gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert ".exo/logs/" in gitignore

    def test_init_with_commit_logs_true_no_gitignore_entry(self, tmp_path: Path) -> None:
        """When commit_logs=true, .exo/logs/ should NOT be in .gitignore."""
        from exo.stdlib.engine import KernelEngine
        engine = KernelEngine(repo=tmp_path, actor="test-agent")
        engine.init(scan=False)
        # Modify config to enable commit_logs
        config = load_yaml(tmp_path / ".exo" / "config.yaml")
        config["privacy"]["commit_logs"] = True
        dump_yaml(tmp_path / ".exo" / "config.yaml", config)
        # Re-run init (idempotent)
        from exo.stdlib.engine import _manage_gitignore
        _manage_gitignore(tmp_path, tmp_path / ".exo" / "config.yaml")
        gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert ".exo/logs/" not in gitignore

    def test_idempotent_gitignore(self, tmp_path: Path) -> None:
        """Running init twice should not duplicate the .gitignore entry."""
        from exo.stdlib.engine import KernelEngine
        engine = KernelEngine(repo=tmp_path, actor="test-agent")
        engine.init(scan=False)
        # Run gitignore management again
        from exo.stdlib.engine import _manage_gitignore
        _manage_gitignore(tmp_path, tmp_path / ".exo" / "config.yaml")
        gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert gitignore.count(".exo/logs/") == 1

    def test_preserves_existing_gitignore_content(self, tmp_path: Path) -> None:
        """Existing .gitignore entries should be preserved."""
        (tmp_path / ".gitignore").write_text("node_modules/\n*.pyc\n", encoding="utf-8")
        from exo.stdlib.engine import KernelEngine
        engine = KernelEngine(repo=tmp_path, actor="test-agent")
        engine.init(scan=False)
        gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert "node_modules/" in gitignore
        assert "*.pyc" in gitignore
        assert ".exo/logs/" in gitignore

    def test_gitignore_has_comment(self, tmp_path: Path) -> None:
        """The gitignore entry should include an explanatory comment."""
        from exo.stdlib.engine import KernelEngine
        engine = KernelEngine(repo=tmp_path, actor="test-agent")
        engine.init(scan=False)
        gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert "privacy.commit_logs" in gitignore

    def test_no_gitignore_when_no_config(self, tmp_path: Path) -> None:
        """If config doesn't exist, still default to excluding logs."""
        from exo.stdlib.engine import _manage_gitignore
        result = _manage_gitignore(tmp_path, tmp_path / ".exo" / "config.yaml")
        assert result is True
        gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert ".exo/logs/" in gitignore


# ── Upgrade Backfills Privacy ─────────────────────────────────────


class TestUpgradeBackfillsPrivacy:

    def test_upgrade_adds_privacy_section(self, tmp_path: Path) -> None:
        """Upgrade should backfill privacy section if missing."""
        from exo.stdlib.engine import KernelEngine
        engine = KernelEngine(repo=tmp_path, actor="test-agent")
        engine.init(scan=False)
        # Remove privacy from config
        config = load_yaml(tmp_path / ".exo" / "config.yaml")
        del config["privacy"]
        dump_yaml(tmp_path / ".exo" / "config.yaml", config)

        from exo.stdlib.upgrade import upgrade
        result = upgrade(tmp_path)
        config_after = load_yaml(tmp_path / ".exo" / "config.yaml")
        assert "privacy" in config_after
        assert config_after["privacy"]["commit_logs"] is False
        assert config_after["privacy"]["redact_local_paths"] is True

    def test_upgrade_preserves_custom_privacy(self, tmp_path: Path) -> None:
        """Upgrade should not overwrite existing privacy values."""
        from exo.stdlib.engine import KernelEngine
        engine = KernelEngine(repo=tmp_path, actor="test-agent")
        engine.init(scan=False)
        config = load_yaml(tmp_path / ".exo" / "config.yaml")
        config["privacy"]["commit_logs"] = True
        dump_yaml(tmp_path / ".exo" / "config.yaml", config)

        from exo.stdlib.upgrade import upgrade
        upgrade(tmp_path)
        config_after = load_yaml(tmp_path / ".exo" / "config.yaml")
        assert config_after["privacy"]["commit_logs"] is True


# ── Init Return Value ─────────────────────────────────────────────


class TestInitPrivacyReturn:

    def test_init_returns_gitignore_updated(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine
        engine = KernelEngine(repo=tmp_path, actor="test-agent")
        result = engine.init(scan=False)
        data = result.get("data", result)
        assert data.get("gitignore_updated") is True

    def test_second_init_no_gitignore_update(self, tmp_path: Path) -> None:
        """Re-init should not report gitignore as updated."""
        from exo.stdlib.engine import KernelEngine
        engine = KernelEngine(repo=tmp_path, actor="test-agent")
        engine.init(scan=False)
        # Init again
        from exo.stdlib.engine import _manage_gitignore
        result = _manage_gitignore(tmp_path, tmp_path / ".exo" / "config.yaml")
        assert result is False  # already present
