"""Tests for policy compiler (exo compose) and self-healing hooks.

Covers:
- compose() — sealed policy compilation
- verify_sealed_policy() — integrity verification
- load_sealed_policy() — loading from disk
- CLI: exo compose
- Hook integrity verification
- Scope-gated Write/Edit blocking
- Auto-reinstall on tamper detection
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from exo.kernel import governance as governance_mod
from exo.kernel.tickets import save_ticket
from exo.stdlib.compose import (
    SEALED_POLICY_PATH,
    compose,
    load_sealed_policy,
    verify_sealed_policy,
)


def _policy_block(rule: dict[str, Any]) -> str:
    return f"\n```yaml exo-policy\n{json.dumps(rule)}\n```\n"


def _bootstrap_repo(tmp_path: Path) -> Path:
    repo = tmp_path
    exo_dir = repo / ".exo"
    exo_dir.mkdir(parents=True, exist_ok=True)

    deny_rule = {
        "id": "RULE-SEC-001",
        "type": "filesystem_deny",
        "patterns": ["**/.env*", "~/.aws/**"],
        "actions": ["read", "write"],
        "message": "Secret deny",
    }
    lock_rule = {
        "id": "RULE-LOCK-001",
        "type": "require_lock",
        "message": "Lock required",
    }
    constitution = "# Constitution\n" + _policy_block(deny_rule) + _policy_block(lock_rule)
    (exo_dir / "CONSTITUTION.md").write_text(constitution, encoding="utf-8")
    governance_mod.compile_constitution(repo)
    return repo


# ── Compose Engine ───────────────────────────────────────────────


class TestCompose:
    def test_compose_creates_sealed_file(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        compose(repo)
        assert (repo / SEALED_POLICY_PATH).exists()

    def test_compose_has_integrity_hash(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = compose(repo)
        policy = result["policy"]
        assert "integrity_hash" in policy
        assert len(policy["integrity_hash"]) == 64  # SHA-256 hex

    def test_compose_integrity_verifies(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        compose(repo)
        check = verify_sealed_policy(repo)
        assert check["valid"] is True
        assert check["reason"] == "ok"

    def test_compose_tampered_fails_verify(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        compose(repo)
        # Tamper with the sealed file
        sealed_path = repo / SEALED_POLICY_PATH
        data = json.loads(sealed_path.read_text(encoding="utf-8"))
        data["deny_patterns"].append("INJECTED_PATTERN")
        sealed_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        check = verify_sealed_policy(repo)
        assert check["valid"] is False
        assert check["reason"] == "tampered"

    def test_compose_sources_hashed(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = compose(repo)
        sources = result["policy"]["sources"]
        assert "constitution" in sources
        assert "config" in sources
        assert "features" in sources
        assert "requirements" in sources
        # Constitution exists, so hash is non-empty
        assert len(sources["constitution"]) == 64
        # Config doesn't exist, so hash is empty
        assert sources["config"] == ""

    def test_compose_deny_from_constitution(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = compose(repo)
        deny = result["policy"]["deny_patterns"]
        assert "**/.env*" in deny
        assert "~/.aws/**" in deny

    def test_compose_feature_deny(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        # Create a features.yaml with a locked feature (dict format with 'features' key)
        import yaml

        features_data = {
            "features": [
                {
                    "id": "core-kernel",
                    "status": "active",
                    "files": ["exo/kernel/**"],
                    "allow_agent_edit": False,
                    "description": "Frozen kernel",
                }
            ]
        }
        (repo / ".exo" / "features.yaml").write_text(yaml.dump(features_data), encoding="utf-8")
        result = compose(repo)
        feature_deny = result["policy"]["scope_deny_from_features"]
        assert "exo/kernel/**" in feature_deny

    def test_compose_no_features_ok(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = compose(repo)
        assert result["policy"]["scope_deny_from_features"] == []

    def test_compose_dry_run(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = compose(repo, dry_run=True)
        assert result["dry_run"] is True
        assert not (repo / SEALED_POLICY_PATH).exists()

    def test_compose_idempotent(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        r1 = compose(repo)
        r2 = compose(repo)
        # Integrity hashes differ (composed_at changes), but structure is same
        assert r1["policy"]["deny_patterns"] == r2["policy"]["deny_patterns"]
        assert r1["policy"]["governance"] == r2["policy"]["governance"]

    def test_compose_hooks_hash(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        # Create a .claude/settings.json with hooks
        claude_dir = repo / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        hooks_config = {"hooks": {"SessionStart": [{"matcher": "startup"}]}}
        (claude_dir / "settings.json").write_text(json.dumps(hooks_config), encoding="utf-8")
        result = compose(repo)
        assert result["policy"]["hooks_hash"] != ""
        assert len(result["policy"]["hooks_hash"]) == 64

    def test_compose_no_hooks_empty_hash(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = compose(repo)
        assert result["policy"]["hooks_hash"] == ""


# ── Load and Verify ──────────────────────────────────────────────


class TestLoadSealedPolicy:
    def test_load_returns_policy(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        compose(repo)
        policy = load_sealed_policy(repo)
        assert policy is not None
        assert policy["version"] == "1"

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        assert load_sealed_policy(repo) is None


class TestVerifySealedPolicy:
    def test_verify_missing(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        check = verify_sealed_policy(repo)
        assert check["valid"] is False
        assert check["reason"] == "missing"

    def test_verify_stale_constitution(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        compose(repo)
        # Modify constitution after composing
        const_path = repo / ".exo" / "CONSTITUTION.md"
        const_path.write_text(
            const_path.read_text(encoding="utf-8") + "\n# Appendix\n",
            encoding="utf-8",
        )
        check = verify_sealed_policy(repo)
        assert check["valid"] is False
        assert check["reason"] == "stale"
        assert "constitution" in check.get("stale_sources", [])


# ── CLI ──────────────────────────────────────────────────────────


class TestCLICompose:
    def test_cli_compose(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = subprocess.run(
            [
                "python3",
                "-m",
                "exo.cli",
                "--format",
                "json",
                "--repo",
                str(repo),
                "compose",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"]
        assert "policy" in data["data"]

    def test_cli_compose_dry_run(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = subprocess.run(
            [
                "python3",
                "-m",
                "exo.cli",
                "--format",
                "json",
                "--repo",
                str(repo),
                "compose",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"]
        assert data["data"]["dry_run"] is True
        assert not (repo / SEALED_POLICY_PATH).exists()


# ── Config Integration ───────────────────────────────────────────


class TestComposeConfig:
    def test_compose_with_config(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        config = {
            "defaults": {"ticket_budgets": {"max_files_changed": 42, "max_loc_changed": 777}},
            "checks_allowlist": ["pytest tests/", "npm test"],
            "coherence": {
                "co_update_rules": [
                    {
                        "trigger": ["exo/cli.py"],
                        "requires": ["tests/**"],
                        "label": "CLI needs tests",
                    }
                ]
            },
        }
        import yaml

        (repo / ".exo" / "config.yaml").write_text(yaml.dump(config), encoding="utf-8")
        result = compose(repo)
        policy = result["policy"]
        assert policy["budgets"]["max_files_changed"] == 42
        assert policy["budgets"]["max_loc_changed"] == 777
        assert "pytest tests/" in policy["checks_allowlist"]
        assert len(policy["coherence_rules"]) == 1

    def test_compose_no_config_defaults(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = compose(repo)
        policy = result["policy"]
        assert policy["budgets"] == {}
        assert policy["checks_allowlist"] == []
        assert policy["coherence_rules"] == []


# ── Hook Integrity Verification ──────────────────────────────────


class TestHookIntegrity:
    def test_verify_hooks_match(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import verify_hook_integrity

        repo = _bootstrap_repo(tmp_path)
        # Install hooks and compose
        claude_dir = repo / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        hooks = {"hooks": {"SessionStart": [{"matcher": "startup"}]}}
        (claude_dir / "settings.json").write_text(json.dumps(hooks), encoding="utf-8")
        compose(repo)
        result = verify_hook_integrity(repo)
        assert result["verified"] is True

    def test_verify_hooks_tampered(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import verify_hook_integrity

        repo = _bootstrap_repo(tmp_path)
        claude_dir = repo / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        hooks = {"hooks": {"SessionStart": [{"matcher": "startup"}]}}
        (claude_dir / "settings.json").write_text(json.dumps(hooks), encoding="utf-8")
        compose(repo)
        # Tamper with hooks
        tampered = {"hooks": {"SessionStart": [{"matcher": "INJECTED"}]}}
        (claude_dir / "settings.json").write_text(json.dumps(tampered), encoding="utf-8")
        result = verify_hook_integrity(repo)
        assert result["verified"] is False
        assert result["reason"] == "tamper_detected"

    def test_verify_no_policy(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import verify_hook_integrity

        repo = _bootstrap_repo(tmp_path)
        result = verify_hook_integrity(repo)
        assert result["verified"] is False
        assert result["reason"] == "no_sealed_policy"

    def test_verify_no_hooks_in_policy(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import verify_hook_integrity

        repo = _bootstrap_repo(tmp_path)
        # Compose without hooks installed
        compose(repo)
        result = verify_hook_integrity(repo)
        assert result["verified"] is True
        assert result["reason"] == "no_hooks_hash_in_policy"


# ── Scope-Gated Blocking ────────────────────────────────────────


class TestScopeCheck:
    def _setup_session(self, repo: Path) -> None:
        """Create a ticket with scope and start a session."""
        ticket = {
            "id": "TKT-SCOPE-1",
            "title": "Scope test",
            "intent": "Test scope",
            "priority": 2,
            "labels": [],
            "type": "feature",
            "status": "todo",
            "scope": {
                "allow": ["src/**", "tests/**"],
                "deny": ["src/secret/**"],
            },
        }
        save_ticket(repo, ticket)
        from exo.kernel.tickets import acquire_lock

        acquire_lock(repo, "TKT-SCOPE-1", owner="agent:test", role="developer")

        from exo.orchestrator.session import AgentSessionManager

        mgr = AgentSessionManager(repo, actor="agent:test")
        mgr.start(vendor="test", model="test")

    def test_scope_allows_in_scope_file(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import check_scope_for_tool

        repo = _bootstrap_repo(tmp_path)
        compose(repo)
        self._setup_session(repo)
        result = check_scope_for_tool(repo, "Write", "src/main.py")
        assert result["allowed"] is True

    def test_scope_blocks_out_of_scope(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import check_scope_for_tool

        repo = _bootstrap_repo(tmp_path)
        compose(repo)
        self._setup_session(repo)
        result = check_scope_for_tool(repo, "Write", "docs/readme.md")
        assert result["allowed"] is False

    def test_scope_blocks_deny_pattern(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import check_scope_for_tool

        repo = _bootstrap_repo(tmp_path)
        compose(repo)
        self._setup_session(repo)
        result = check_scope_for_tool(repo, "Edit", "src/secret/key.py")
        assert result["allowed"] is False

    def test_scope_no_session_allows(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import check_scope_for_tool

        repo = _bootstrap_repo(tmp_path)
        compose(repo)
        # No session active — fail-open
        result = check_scope_for_tool(repo, "Write", "anything.py")
        assert result["allowed"] is True

    def test_scope_blocks_global_deny(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import check_scope_for_tool

        repo = _bootstrap_repo(tmp_path)
        compose(repo)
        self._setup_session(repo)
        # .env files denied by constitution
        result = check_scope_for_tool(repo, "Write", ".env.local")
        assert result["allowed"] is False


# ── Auto-Heal ────────────────────────────────────────────────────


class TestAutoHeal:
    def test_auto_heal_reinstalls(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import auto_heal_hooks

        repo = _bootstrap_repo(tmp_path)
        claude_dir = repo / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        hooks = {"hooks": {"SessionStart": [{"matcher": "startup"}]}}
        (claude_dir / "settings.json").write_text(json.dumps(hooks), encoding="utf-8")
        compose(repo)
        # Tamper
        (claude_dir / "settings.json").write_text('{"hooks": {}}', encoding="utf-8")
        result = auto_heal_hooks(repo)
        assert result["healed"] is True
        # Verify hooks restored
        restored = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
        assert "SessionStart" in restored.get("hooks", {})

    def test_auto_heal_logs_tamper(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import auto_heal_hooks

        repo = _bootstrap_repo(tmp_path)
        claude_dir = repo / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        hooks = {"hooks": {"SessionStart": [{"matcher": "startup"}]}}
        (claude_dir / "settings.json").write_text(json.dumps(hooks), encoding="utf-8")
        compose(repo)
        # Tamper
        (claude_dir / "settings.json").write_text('{"hooks": {}}', encoding="utf-8")
        auto_heal_hooks(repo)
        # Check tamper log
        tamper_log = repo / ".exo" / "audit" / "tamper.jsonl"
        assert tamper_log.exists()
        line = tamper_log.read_text(encoding="utf-8").strip().split("\n")[0]
        event = json.loads(line)
        assert event["event"] == "hook_tamper_detected"

    def test_auto_heal_noop_when_valid(self, tmp_path: Path) -> None:
        from exo.stdlib.hooks import auto_heal_hooks

        repo = _bootstrap_repo(tmp_path)
        claude_dir = repo / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        hooks = {"hooks": {"SessionStart": [{"matcher": "startup"}]}}
        (claude_dir / "settings.json").write_text(json.dumps(hooks), encoding="utf-8")
        compose(repo)
        result = auto_heal_hooks(repo)
        assert result["healed"] is False


# ── Composed Check (exo check runs ALL subsystems) ──────────────


def _full_bootstrap(tmp_path: Path) -> Path:
    """Bootstrap with DEFAULT_CONFIG for engine tests."""
    from exo.kernel.utils import dump_yaml
    from exo.stdlib.defaults import DEFAULT_CONFIG, DEFAULT_CONSTITUTION

    repo = tmp_path
    exo_dir = repo / ".exo"
    exo_dir.mkdir(parents=True, exist_ok=True)
    (exo_dir / "CONSTITUTION.md").write_text(DEFAULT_CONSTITUTION, encoding="utf-8")
    dump_yaml(exo_dir / "config.yaml", DEFAULT_CONFIG)
    for d in [
        "tickets",
        "locks",
        "logs",
        "memory",
        "memory/sessions",
        "cache",
        "cache/sessions",
    ]:
        (exo_dir / d).mkdir(parents=True, exist_ok=True)
    governance_mod.compile_constitution(repo)
    return repo


def _init_git(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)


def _create_ticket_and_lock(repo: Path, ticket_id: str = "TKT-COMP-1") -> None:
    save_ticket(
        repo,
        {
            "id": ticket_id,
            "title": "composed check test",
            "intent": "test",
            "priority": 1,
            "type": "feature",
            "status": "todo",
            "labels": [],
            "checks": [],
        },
    )
    from exo.kernel.tickets import acquire_lock

    acquire_lock(repo, ticket_id, owner="test")


class TestComposedCheck:
    """exo check should run all governance subsystems by default."""

    def test_check_returns_governance_field(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine

        repo = _full_bootstrap(tmp_path)
        _init_git(repo)
        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.check()
        data = result.get("data", {})
        assert "governance" in data

    def test_check_returns_sealed_policy_field(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine

        repo = _full_bootstrap(tmp_path)
        _init_git(repo)
        compose(repo)
        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.check()
        data = result.get("data", {})
        assert "sealed_policy" in data

    def test_check_governance_passes_when_valid(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine

        repo = _full_bootstrap(tmp_path)
        _init_git(repo)
        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.check()
        data = result.get("data", {})
        assert data["governance"]["status"] == "pass"

    def test_check_sealed_policy_valid_after_compose(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine

        repo = _full_bootstrap(tmp_path)
        _init_git(repo)
        compose(repo)
        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.check()
        data = result.get("data", {})
        assert data["sealed_policy"]["valid"] is True

    def test_check_sealed_policy_missing_reports_invalid(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine

        repo = _full_bootstrap(tmp_path)
        _init_git(repo)
        # No compose — sealed policy missing
        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.check()
        data = result.get("data", {})
        assert data["sealed_policy"]["valid"] is False
        assert data["sealed_policy"]["reason"] == "missing"
        # Governance checks are advisory — missing sealed policy doesn't block
        assert data["passed"] is True

    def test_check_features_none_when_no_manifest(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine

        repo = _full_bootstrap(tmp_path)
        _init_git(repo)
        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.check()
        data = result.get("data", {})
        # No features.yaml → skip → None
        assert data["features"] is None

    def test_check_requirements_none_when_no_manifest(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine

        repo = _full_bootstrap(tmp_path)
        _init_git(repo)
        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.check()
        data = result.get("data", {})
        assert data["requirements"] is None

    def test_check_features_reports_when_manifest_exists(self, tmp_path: Path) -> None:
        import yaml

        from exo.stdlib.engine import KernelEngine

        repo = _full_bootstrap(tmp_path)
        features_data = {
            "features": [
                {
                    "id": "core",
                    "status": "active",
                    "files": ["src/**"],
                    "description": "Core",
                }
            ]
        }
        (repo / ".exo" / "features.yaml").write_text(yaml.dump(features_data), encoding="utf-8")
        _init_git(repo)
        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.check()
        data = result.get("data", {})
        assert data["features"] is not None
        assert data["features"]["name"] == "features"

    def test_check_requirements_reports_when_manifest_exists(self, tmp_path: Path) -> None:
        import yaml

        from exo.stdlib.engine import KernelEngine

        repo = _full_bootstrap(tmp_path)
        reqs_data = {
            "requirements": [
                {
                    "id": "REQ-001",
                    "title": "Must work",
                    "status": "active",
                    "priority": "high",
                }
            ]
        }
        (repo / ".exo" / "requirements.yaml").write_text(yaml.dump(reqs_data), encoding="utf-8")
        _init_git(repo)
        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.check()
        data = result.get("data", {})
        assert data["requirements"] is not None
        assert data["requirements"]["name"] == "requirements"

    def test_check_fails_on_untested_acceptance_criteria(self, tmp_path: Path) -> None:
        """exo check must fail when acceptance criteria have no @acc: test annotations."""
        import yaml

        from exo.stdlib.engine import KernelEngine

        repo = _full_bootstrap(tmp_path)
        reqs_data = {
            "requirements": [
                {
                    "id": "REQ-001",
                    "title": "Auth",
                    "status": "active",
                    "priority": "high",
                    "acceptance": ["ACC-LOGIN", "ACC-LOCKOUT"],
                }
            ]
        }
        (repo / ".exo" / "requirements.yaml").write_text(yaml.dump(reqs_data), encoding="utf-8")
        # Add @req: ref but no @acc: annotations
        src_dir = repo / "src"
        src_dir.mkdir(exist_ok=True)
        (src_dir / "auth.py").write_text("# @req: REQ-001\ndef login(): pass\n", encoding="utf-8")
        _init_git(repo)
        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.check()
        data = result.get("data", {})
        # Requirements section must report failure
        assert data["requirements"] is not None
        assert data["requirements"]["status"] == "fail"
        assert data["requirements"]["errors"] >= 1
        # Overall check must fail
        assert data["passed"] is False

    def test_check_passed_includes_all_subsystems(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine

        repo = _full_bootstrap(tmp_path)
        _init_git(repo)
        compose(repo)
        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.check()
        data = result.get("data", {})
        # All subsystems present
        assert data["governance"] is not None
        assert data["sealed_policy"] is not None
        assert data["coherence"] is not None
        # passed is True when everything is clean
        assert data["passed"] is True


# ── Human Summary for Check ────────────────────────────────────


class TestCheckHumanSummary:
    def test_human_summary_shows_pass(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine, format_check_human

        repo = _full_bootstrap(tmp_path)
        _init_git(repo)
        compose(repo)
        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.check()
        data = result.get("data", {})
        text = format_check_human(data)
        assert "Governance Check: PASS" in text

    def test_human_summary_shows_governance(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine, format_check_human

        repo = _full_bootstrap(tmp_path)
        _init_git(repo)
        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.check()
        data = result.get("data", {})
        text = format_check_human(data)
        assert "governance:" in text
        assert "PASS" in text

    def test_human_summary_shows_sealed_policy(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine, format_check_human

        repo = _full_bootstrap(tmp_path)
        _init_git(repo)
        compose(repo)
        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.check()
        data = result.get("data", {})
        text = format_check_human(data)
        assert "sealed policy: OK" in text

    def test_human_summary_missing_sealed_policy(self, tmp_path: Path) -> None:
        from exo.stdlib.engine import KernelEngine, format_check_human

        repo = _full_bootstrap(tmp_path)
        _init_git(repo)
        _create_ticket_and_lock(repo)
        engine = KernelEngine(str(repo))
        result = engine.check()
        data = result.get("data", {})
        text = format_check_human(data)
        assert "sealed policy: MISSING" in text

    def test_cli_check_human_format(self, tmp_path: Path) -> None:
        repo = _full_bootstrap(tmp_path)
        _init_git(repo)
        _create_ticket_and_lock(repo)
        result = subprocess.run(
            [
                "python3",
                "-m",
                "exo.cli",
                "--format",
                "human",
                "--repo",
                str(repo),
                "check",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "Governance Check:" in result.stdout


# ── Pre-commit Hook Script ─────────────────────────────────────


class TestPreCommitHookScript:
    def test_hook_script_has_python_fallback(self) -> None:
        from exo.stdlib.hooks import GIT_HOOK_SCRIPT

        assert "python3 -m exo.cli" in GIT_HOOK_SCRIPT

    def test_hook_script_uses_human_format(self) -> None:
        from exo.stdlib.hooks import GIT_HOOK_SCRIPT

        assert "--format human" in GIT_HOOK_SCRIPT

    def test_hook_script_checks_exo_dir(self) -> None:
        from exo.stdlib.hooks import GIT_HOOK_SCRIPT

        assert '[ -d ".exo" ]' in GIT_HOOK_SCRIPT


# ── Session-Finish Composed Policy ─────────────────────────────


def _create_ticket_only(repo: Path, ticket_id: str = "TKT-FIN-1") -> None:
    save_ticket(
        repo,
        {
            "id": ticket_id,
            "title": "session finish test",
            "intent": "test",
            "priority": 1,
            "type": "feature",
            "status": "todo",
            "labels": [],
            "checks": [],
        },
    )


class TestSessionFinishComposedPolicy:
    def _start_and_finish(self, repo: Path, ticket_id: str = "TKT-FIN-1") -> dict:
        from exo.kernel.tickets import acquire_lock
        from exo.orchestrator.session import AgentSessionManager

        acquire_lock(repo, ticket_id, owner="agent:test", role="developer")
        mgr = AgentSessionManager(repo, actor="agent:test")
        mgr.start(vendor="test", model="test")
        return mgr.finish(
            summary="test finish",
            ticket_id=ticket_id,
            set_status="keep",
            skip_check=True,
            break_glass_reason="test",
        )

    def test_finish_includes_composed_policy_valid(self, tmp_path: Path) -> None:
        repo = _full_bootstrap(tmp_path)
        _init_git(repo)
        compose(repo)
        _create_ticket_only(repo)
        result = self._start_and_finish(repo)
        assert "composed_policy_valid" in result

    def test_finish_composed_policy_valid_after_compose(self, tmp_path: Path) -> None:
        repo = _full_bootstrap(tmp_path)
        _init_git(repo)
        compose(repo)
        _create_ticket_only(repo)
        result = self._start_and_finish(repo)
        assert result["composed_policy_valid"] is True

    def test_finish_composed_policy_in_memento(self, tmp_path: Path) -> None:
        repo = _full_bootstrap(tmp_path)
        _init_git(repo)
        compose(repo)
        _create_ticket_only(repo)
        result = self._start_and_finish(repo)
        memento = (repo / result["memento_path"]).read_text(encoding="utf-8")
        assert "Composed Policy:" in memento

    def test_finish_composed_policy_in_session_index(self, tmp_path: Path) -> None:
        repo = _full_bootstrap(tmp_path)
        _init_git(repo)
        compose(repo)
        _create_ticket_only(repo)
        result = self._start_and_finish(repo)
        index_path = repo / result["session_index_path"]
        lines = index_path.read_text(encoding="utf-8").strip().splitlines()
        row = json.loads(lines[-1])
        assert "composed_policy_valid" in row
        assert row["composed_policy_valid"] is True

    def test_finish_auto_recomposes(self, tmp_path: Path) -> None:
        repo = _full_bootstrap(tmp_path)
        _init_git(repo)
        _create_ticket_only(repo)
        self._start_and_finish(repo)
        # After finish, sealed policy should exist (auto-recomposed)
        sealed_path = repo / ".exo" / "policy.sealed.json"
        assert sealed_path.exists()


# ════════════════════════════════════════════════════════════════════
# exo brief (closes feedback #1)
# ════════════════════════════════════════════════════════════════════


class TestComposeBrief:
    """compose_brief is read-only: never acquires lock, never writes mementos."""

    def test_brief_returns_rules_from_governance_lock(self, tmp_path: Path) -> None:
        from exo.stdlib.compose import compose_brief

        repo = _bootstrap_repo(tmp_path)
        brief = compose_brief(repo)
        assert brief["governance_loaded"] is True
        rule_ids = [r["id"] for r in brief["rules"]]
        assert "RULE-SEC-001" in rule_ids
        assert "RULE-LOCK-001" in rule_ids

    def test_brief_handles_missing_governance(self, tmp_path: Path) -> None:
        from exo.stdlib.compose import compose_brief

        # No governance.lock.json
        brief = compose_brief(tmp_path)
        assert brief["governance_loaded"] is False
        assert brief["rules"] == []

    def test_brief_lists_active_intents(self, tmp_path: Path) -> None:
        from exo.kernel import tickets as tickets_mod
        from exo.stdlib.compose import compose_brief

        repo = _bootstrap_repo(tmp_path)
        tickets_mod.save_ticket(
            repo,
            {
                "id": "INTENT-001",
                "kind": "intent",
                "title": "Active intent",
                "intent": "Active intent",
                "brain_dump": "x",
                "boundary": "do not touch tests/",
                "status": "todo",
            },
        )
        tickets_mod.save_ticket(
            repo,
            {
                "id": "INTENT-002",
                "kind": "intent",
                "title": "Done intent",
                "intent": "Done intent",
                "brain_dump": "y",
                "status": "done",
            },
        )
        brief = compose_brief(repo)
        intent_ids = [i["id"] for i in brief["active_intents"]]
        assert "INTENT-001" in intent_ids
        assert "INTENT-002" not in intent_ids

    def test_brief_max_intents_caps_list(self, tmp_path: Path) -> None:
        from exo.kernel import tickets as tickets_mod
        from exo.stdlib.compose import compose_brief

        repo = _bootstrap_repo(tmp_path)
        for n in range(5):
            tickets_mod.save_ticket(
                repo,
                {
                    "id": f"INTENT-{n:03d}",
                    "kind": "intent",
                    "title": f"intent {n}",
                    "intent": f"intent {n}",
                    "brain_dump": "x",
                    "status": "todo",
                },
            )
        brief = compose_brief(repo, max_intents=2)
        assert len(brief["active_intents"]) == 2

    def test_brief_does_not_acquire_lock(self, tmp_path: Path) -> None:
        """Critical contract: exo brief is read-only — no ticket lock written."""
        from exo.stdlib.compose import compose_brief

        repo = _bootstrap_repo(tmp_path)
        compose_brief(repo)
        lock_file = repo / ".exo" / "locks" / "ticket.lock.json"
        assert not lock_file.exists(), "compose_brief must not acquire a ticket lock"

    def test_brief_does_not_create_memento(self, tmp_path: Path) -> None:
        """Critical contract: exo brief writes nothing to memory/sessions."""
        from exo.stdlib.compose import compose_brief

        repo = _bootstrap_repo(tmp_path)
        compose_brief(repo)
        mementos_dir = repo / ".exo" / "memory" / "sessions"
        # Either dir doesn't exist OR is empty after a brief call
        if mementos_dir.exists():
            assert not list(mementos_dir.iterdir()), "compose_brief must not write mementos"

    def test_format_brief_human_includes_rules_and_intents(self, tmp_path: Path) -> None:
        from exo.kernel import tickets as tickets_mod
        from exo.stdlib.compose import compose_brief, format_brief_human

        repo = _bootstrap_repo(tmp_path)
        tickets_mod.save_ticket(
            repo,
            {
                "id": "INTENT-001",
                "kind": "intent",
                "title": "Build the thing",
                "intent": "Build the thing",
                "brain_dump": "x",
                "status": "todo",
            },
        )
        text = format_brief_human(compose_brief(repo))
        assert "ExoProtocol Governance Brief" in text
        assert "RULE-SEC-001" in text
        assert "INTENT-001" in text
        assert "Build the thing" in text

    def test_brief_via_cli_no_session_artifacts(self, tmp_path: Path, monkeypatch: Any) -> None:
        from exo.cli import main as cli_main_local

        repo = _bootstrap_repo(tmp_path)
        monkeypatch.chdir(repo)
        rc = cli_main_local(["brief"])
        assert rc == 0
        # No lock, no memento — same contract enforced through CLI
        assert not (repo / ".exo" / "locks" / "ticket.lock.json").exists()
