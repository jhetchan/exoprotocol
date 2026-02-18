"""Tests for adapter generation (CLAUDE.md / .cursorrules / AGENTS.md from governance state)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from exo.cli import main as cli_main
from exo.kernel import governance as governance_mod
from exo.stdlib.adapters import (
    ADAPTER_TARGETS,
    AGENT_ADAPTER_TARGETS,
    EXO_MARKER_BEGIN,
    EXO_MARKER_END,
    TARGET_FILES,
    _count_user_lines,
    _derive_permission_deny,
    _extract_marker_sections,
    _format_deny_rules,
    _load_governance_lock,
    _wrap_with_markers,
    derive_sandbox_policy,
    format_sandbox_policy_human,
    generate_adapters,
)
from exo.stdlib.reconcile import reconcile_session


def _policy_block(rule: dict[str, Any]) -> str:
    return f"\n```yaml exo-policy\n{json.dumps(rule)}\n```\n"


def _bootstrap_repo(
    tmp_path: Path,
    *,
    max_files: int = 10,
    max_loc: int = 300,
    checks: list[str] | None = None,
    extra_deny_rules: list[dict[str, Any]] | None = None,
    ci_config: dict[str, Any] | None = None,
) -> Path:
    """Bootstrap repo with constitution + governance lock.

    Accepts custom config values so tests can verify manifest-driven output.
    """
    repo = tmp_path
    exo_dir = repo / ".exo"
    exo_dir.mkdir(parents=True, exist_ok=True)

    deny_rules = [
        {
            "id": "RULE-SEC-001",
            "type": "filesystem_deny",
            "patterns": ["**/.env*"],
            "actions": ["read", "write"],
            "message": "Secret deny",
        },
    ]
    if extra_deny_rules:
        deny_rules.extend(extra_deny_rules)

    constitution = "# Test Constitution\n\n"
    for rule in deny_rules:
        constitution += _policy_block(rule)
    constitution += _policy_block(
        {
            "id": "RULE-LOCK-001",
            "type": "require_lock",
            "message": "Lock required",
        }
    )

    (exo_dir / "CONSTITUTION.md").write_text(constitution)
    governance_mod.compile_constitution(repo)

    if checks is None:
        checks = ["pytest", "python3 -m compileall"]

    checks_yaml = "\n".join(f"  - {c}" for c in checks)
    config_text = f"""\
version: 1
defaults:
  ticket_budgets:
    max_files_changed: {max_files}
    max_loc_changed: {max_loc}
checks_allowlist:
{checks_yaml}
"""
    if ci_config:
        ci_lines = ["ci:"]
        for key, val in ci_config.items():
            ci_lines.append(f"  {key}: {json.dumps(val) if isinstance(val, str) else val}")
        config_text += "\n".join(ci_lines) + "\n"

    (exo_dir / "config.yaml").write_text(config_text)
    return repo


class TestAdapterGeneration:
    def test_generate_all_targets(self, tmp_path: Path) -> None:
        """Generating with no targets produces all adapter files."""
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo)
        assert result["dry_run"] is False
        assert sorted(result["targets"]) == sorted(ADAPTER_TARGETS)
        # written includes adapter files + LEARNINGS.md
        assert len(result["written"]) >= len(ADAPTER_TARGETS)
        for target in ADAPTER_TARGETS:
            filename = TARGET_FILES[target]
            assert (repo / filename).exists()

    def test_generate_single_target(self, tmp_path: Path) -> None:
        """Generating a single target writes only that file."""
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, targets=["claude"])
        assert "CLAUDE.md" in result["written"]
        assert (repo / "CLAUDE.md").exists()
        assert not (repo / ".cursorrules").exists()
        assert not (repo / "AGENTS.md").exists()

    def test_generate_dry_run(self, tmp_path: Path) -> None:
        """Dry run returns content without writing files."""
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, targets=["claude"], dry_run=True)
        assert result["dry_run"] is True
        assert result["written"] == []
        assert not (repo / "CLAUDE.md").exists()
        # Content should be in the result
        assert "content" in result["files"]["claude"]
        assert "ExoProtocol" in result["files"]["claude"]["content"]

    def test_invalid_target_rejected(self, tmp_path: Path) -> None:
        """Invalid target raises ExoError."""
        repo = _bootstrap_repo(tmp_path)
        try:
            generate_adapters(repo, targets=["invalid"])
            raise AssertionError("Should have raised ExoError")
        except Exception as e:
            assert "ADAPTER_TARGET_INVALID" in str(e)

    def test_missing_governance_lock_rejected(self, tmp_path: Path) -> None:
        """Missing governance lock raises ExoError."""
        repo = tmp_path
        (repo / ".exo").mkdir(parents=True, exist_ok=True)
        try:
            generate_adapters(repo, targets=["claude"])
            raise AssertionError("Should have raised ExoError")
        except Exception as e:
            assert "GOVERNANCE_LOCK_MISSING" in str(e)

    def test_claude_md_contains_governance_rules(self, tmp_path: Path) -> None:
        """Generated CLAUDE.md contains governance rules from lock."""
        repo = _bootstrap_repo(tmp_path)
        generate_adapters(repo, targets=["claude"])
        content = (repo / "CLAUDE.md").read_text()
        assert "ExoProtocol" in content
        assert "RULE-SEC-001" in content
        assert "**/.env*" in content
        assert "RULE-LOCK-001" in content
        assert "session-start" in content
        assert "session-finish" in content

    def test_claude_md_contains_budgets(self, tmp_path: Path) -> None:
        """Generated CLAUDE.md includes default budgets from config."""
        repo = _bootstrap_repo(tmp_path)
        generate_adapters(repo, targets=["claude"])
        content = (repo / "CLAUDE.md").read_text()
        assert "max files changed: 10" in content

    def test_claude_md_contains_checks_allowlist(self, tmp_path: Path) -> None:
        """Generated CLAUDE.md includes approved checks from config."""
        repo = _bootstrap_repo(tmp_path)
        generate_adapters(repo, targets=["claude"])
        content = (repo / "CLAUDE.md").read_text()
        assert "pytest" in content
        assert "python3 -m compileall" in content

    def test_cursorrules_contains_governance(self, tmp_path: Path) -> None:
        """Generated .cursorrules has governance rules."""
        repo = _bootstrap_repo(tmp_path)
        generate_adapters(repo, targets=["cursor"])
        content = (repo / ".cursorrules").read_text()
        assert "ExoProtocol" in content
        assert "RULE-SEC-001" in content
        assert "session-start" in content

    def test_agents_md_contains_governance(self, tmp_path: Path) -> None:
        """Generated AGENTS.md has governance rules."""
        repo = _bootstrap_repo(tmp_path)
        generate_adapters(repo, targets=["agents"])
        content = (repo / "AGENTS.md").read_text()
        assert "ExoProtocol" in content
        assert "RULE-SEC-001" in content
        assert "Drift detection" in content

    def test_result_includes_governance_hash(self, tmp_path: Path) -> None:
        """Return data includes governance source hash."""
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, targets=["claude"])
        assert result["governance_hash"]
        assert len(result["governance_hash"]) == 64  # SHA256 hex

    def test_idempotent_regeneration(self, tmp_path: Path) -> None:
        """Running generate twice overwrites cleanly."""
        repo = _bootstrap_repo(tmp_path)
        generate_adapters(repo, targets=["claude"])
        content1 = (repo / "CLAUDE.md").read_text()
        generate_adapters(repo, targets=["claude"])
        content2 = (repo / "CLAUDE.md").read_text()
        assert content1 == content2


class TestAdapterCLI:
    def test_adapter_generate_via_cli(self, tmp_path: Path) -> None:
        """CLI command generates adapters."""
        repo = _bootstrap_repo(tmp_path)
        rc = cli_main(
            [
                "--repo",
                str(repo),
                "--format",
                "json",
                "adapter-generate",
            ]
        )
        assert rc == 0
        assert (repo / "CLAUDE.md").exists()
        assert (repo / ".cursorrules").exists()
        assert (repo / "AGENTS.md").exists()
        assert (repo / ".github" / "workflows" / "exo-governance.yml").exists()

    def test_adapter_generate_single_target_cli(self, tmp_path: Path) -> None:
        """CLI can target a single adapter."""
        repo = _bootstrap_repo(tmp_path)
        rc = cli_main(
            [
                "--repo",
                str(repo),
                "--format",
                "json",
                "adapter-generate",
                "--target",
                "claude",
            ]
        )
        assert rc == 0
        assert (repo / "CLAUDE.md").exists()
        assert not (repo / ".cursorrules").exists()

    def test_adapter_generate_dry_run_cli(self, tmp_path: Path) -> None:
        """CLI dry run does not write files."""
        repo = _bootstrap_repo(tmp_path)
        rc = cli_main(
            [
                "--repo",
                str(repo),
                "--format",
                "json",
                "adapter-generate",
                "--dry-run",
            ]
        )
        assert rc == 0
        assert not (repo / "CLAUDE.md").exists()


class TestFormatHelpers:
    def test_format_deny_rules(self) -> None:
        """Deny rules are formatted as readable lines."""
        rules = [
            {"id": "R1", "type": "filesystem_deny", "patterns": ["*.env"], "actions": ["read"]},
            {"id": "R2", "type": "require_lock", "message": "lock"},
        ]
        lines = _format_deny_rules(rules)
        assert len(lines) == 1
        assert "R1" in lines[0]
        assert "*.env" in lines[0]


# ──────────────────────────────────────────────
# Manifest Conformance: values MUST come from config, not hardcoded
# ──────────────────────────────────────────────


class TestManifestConformance:
    """Verify adapter output loads values from .exo/ manifest.

    If an LLM hardcodes values (e.g. ``max_files_changed: 10``) instead of
    reading from config, these tests fail because we use *unusual* numbers
    that would never appear as hardcoded defaults.
    """

    def test_manifest_directive_present_in_all_targets(self, tmp_path: Path) -> None:
        """All agent adapters must contain the manifest-driven workflow directive."""
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, dry_run=True)
        for target in AGENT_ADAPTER_TARGETS:
            content = result["files"][target]["content"]
            assert "Source of Truth" in content, f"{target} adapter missing 'Source of Truth' directive"
            assert ".exo/config.yaml" in content, f"{target} adapter missing config.yaml manifest path"
            assert ".exo/governance.lock.json" in content, (
                f"{target} adapter missing governance.lock.json manifest path"
            )
            assert "hardcode" in content.lower(), f"{target} adapter missing anti-hardcode directive"

    def test_test_driven_workflow_directive_in_all_targets(self, tmp_path: Path) -> None:
        """All agent adapters must contain the test-driven manifest-first workflow section."""
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, dry_run=True)
        for target in AGENT_ADAPTER_TARGETS:
            content = result["files"][target]["content"]
            assert "Test-Driven, Manifest-First Workflow" in content, (
                f"{target} adapter missing test-driven workflow section"
            )
            # Must tell agent: config is source of truth (not just for governance)
            assert "all code you write" in content.lower(), (
                f"{target} adapter must apply manifest-first to all code, not just governance"
            )
            # Must explain the testing principle
            assert "vary" in content.lower(), (
                f"{target} adapter must instruct agent to write tests that vary config inputs"
            )

    def test_budget_values_come_from_config(self, tmp_path: Path) -> None:
        """Unusual budget values in config must appear verbatim in output."""
        repo = _bootstrap_repo(tmp_path, max_files=42, max_loc=777)
        result = generate_adapters(repo, targets=["claude"], dry_run=True)
        content = result["files"]["claude"]["content"]
        assert "max files changed: 42" in content
        # Must NOT contain the old defaults
        assert "max files changed: 10" not in content

    def test_changing_config_changes_output(self, tmp_path: Path) -> None:
        """Regenerating after config change produces different output."""
        repo = _bootstrap_repo(tmp_path, max_files=10, max_loc=300)
        result1 = generate_adapters(repo, targets=["claude"], dry_run=True)
        content1 = result1["files"]["claude"]["content"]

        # Update config with different values
        config_text = """\
version: 1
defaults:
  ticket_budgets:
    max_files_changed: 99
    max_loc_changed: 1500
checks_allowlist:
  - pytest
"""
        (repo / ".exo" / "config.yaml").write_text(config_text)

        result2 = generate_adapters(repo, targets=["claude"], dry_run=True)
        content2 = result2["files"]["claude"]["content"]

        assert content1 != content2
        assert "max files changed: 99" in content2
        assert "max files changed: 10" not in content2

    def test_all_targets_reflect_same_config_values(self, tmp_path: Path) -> None:
        """All agent adapter files must contain the same budget values from config."""
        repo = _bootstrap_repo(tmp_path, max_files=37, max_loc=891)
        result = generate_adapters(repo, dry_run=True)

        for target in AGENT_ADAPTER_TARGETS:
            content = result["files"][target]["content"]
            assert "max files changed: 37" in content, f"{target} adapter missing config budget max_files=37"

    def test_checks_allowlist_comes_from_config(self, tmp_path: Path) -> None:
        """Custom checks in config must appear; default checks must NOT."""
        repo = _bootstrap_repo(tmp_path, checks=["mypy --strict", "cargo test"])
        result = generate_adapters(repo, targets=["claude"], dry_run=True)
        content = result["files"]["claude"]["content"]
        assert "mypy --strict" in content
        assert "cargo test" in content
        # Default checks should NOT appear since we didn't include them
        assert "python3 -m compileall" not in content

    def test_deny_patterns_come_from_governance_lock(self, tmp_path: Path) -> None:
        """Custom deny patterns in constitution must appear in adapter output."""
        repo = _bootstrap_repo(
            tmp_path,
            extra_deny_rules=[
                {
                    "id": "RULE-CUSTOM-999",
                    "type": "filesystem_deny",
                    "patterns": ["**/secrets.toml", "deploy/*.key"],
                    "actions": ["read", "write"],
                    "message": "Custom secret deny",
                },
            ],
        )
        result = generate_adapters(repo, targets=["claude"], dry_run=True)
        content = result["files"]["claude"]["content"]
        assert "RULE-CUSTOM-999" in content
        assert "secrets.toml" in content
        assert "deploy/*.key" in content

    def test_governance_hash_changes_with_constitution(self, tmp_path: Path) -> None:
        """Different constitutions must produce different governance hashes."""
        repo1 = _bootstrap_repo(tmp_path / "a")
        result1 = generate_adapters(repo1, targets=["claude"])

        repo2 = _bootstrap_repo(
            tmp_path / "b",
            extra_deny_rules=[
                {
                    "id": "RULE-EXTRA-001",
                    "type": "filesystem_deny",
                    "patterns": ["*.bak"],
                    "actions": ["write"],
                    "message": "No backups",
                },
            ],
        )
        result2 = generate_adapters(repo2, targets=["claude"])

        assert result1["governance_hash"] != result2["governance_hash"]


class TestDefaultFallbacks:
    """Verify known defaults when config values are absent."""

    def test_missing_budgets_uses_known_defaults(self, tmp_path: Path) -> None:
        """When config has no ticket_budgets, output uses documented defaults."""
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
        (exo_dir / "CONSTITUTION.md").write_text(constitution)
        governance_mod.compile_constitution(repo)

        # Config with NO budgets section
        (exo_dir / "config.yaml").write_text("version: 1\n")

        result = generate_adapters(repo, targets=["claude"], dry_run=True)
        content = result["files"]["claude"]["content"]
        # Should NOT contain budget section at all (no budgets in config)
        assert "Default Budgets" not in content

    def test_missing_config_file_still_generates(self, tmp_path: Path) -> None:
        """Adapter generation works even without config.yaml (governance lock only)."""
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
        (exo_dir / "CONSTITUTION.md").write_text(constitution)
        governance_mod.compile_constitution(repo)
        # No config.yaml at all

        result = generate_adapters(repo, targets=["claude"], dry_run=True)
        content = result["files"]["claude"]["content"]
        assert "ExoProtocol" in content
        assert "RULE-SEC-001" in content
        # No budgets, no checks
        assert "Default Budgets" not in content
        assert "Approved Checks" not in content

    def test_empty_checks_allowlist_omitted(self, tmp_path: Path) -> None:
        """Empty checks_allowlist means no 'Approved Checks' section."""
        repo = _bootstrap_repo(tmp_path, checks=[])
        # Overwrite config without checks
        (repo / ".exo" / "config.yaml").write_text(
            "version: 1\ndefaults:\n  ticket_budgets:\n    max_files_changed: 5\n    max_loc_changed: 100\n"
        )
        result = generate_adapters(repo, targets=["claude"], dry_run=True)
        content = result["files"]["claude"]["content"]
        assert "max files changed: 5" in content
        assert "Approved Checks" not in content


# ──────────────────────────────────────────────
# Drift Budget Variance: budget values must affect drift score
# ──────────────────────────────────────────────


def _init_git_repo(repo: Path) -> None:
    """Initialize a git repo with an initial commit on a branch named 'main'."""
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True)
    (repo / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(repo), capture_output=True)


def _make_changes(repo: Path, n_files: int, loc_per_file: int) -> None:
    """Create n_files with loc_per_file lines each on a work branch."""
    subprocess.run(["git", "checkout", "-b", "work"], cwd=str(repo), capture_output=True)
    src = repo / "src"
    src.mkdir(exist_ok=True)
    for i in range(n_files):
        (src / f"file_{i}.py").write_text("\n".join(f"line_{j}" for j in range(loc_per_file)) + "\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "work"], cwd=str(repo), capture_output=True)


class TestDriftBudgetVariance:
    """Verify that ticket budget values drive drift score, not hardcoded defaults."""

    def test_tight_budget_higher_drift(self, tmp_path: Path) -> None:
        """Same changes with a tight budget produce higher drift than a loose budget."""
        repo = tmp_path
        _init_git_repo(repo)
        _make_changes(repo, n_files=5, loc_per_file=20)

        tight_ticket = {
            "id": "T-TIGHT",
            "kind": "task",
            "scope": {"allow": ["**"], "deny": []},
            "budgets": {"max_files_changed": 3, "max_loc_changed": 50},
            "boundary": "",
        }
        loose_ticket = {
            "id": "T-LOOSE",
            "kind": "task",
            "scope": {"allow": ["**"], "deny": []},
            "budgets": {"max_files_changed": 100, "max_loc_changed": 5000},
            "boundary": "",
        }

        report_tight = reconcile_session(repo, tight_ticket, git_base="main")
        report_loose = reconcile_session(repo, loose_ticket, git_base="main")

        assert report_tight.drift_score > report_loose.drift_score, (
            f"tight drift {report_tight.drift_score} should exceed loose drift {report_loose.drift_score}"
        )

    def test_budget_values_reflected_in_report(self, tmp_path: Path) -> None:
        """Budget max values in the report come from the ticket, not defaults."""
        repo = tmp_path
        _init_git_repo(repo)
        _make_changes(repo, n_files=2, loc_per_file=10)

        ticket = {
            "id": "T-CUSTOM",
            "kind": "task",
            "scope": {"allow": ["**"], "deny": []},
            "budgets": {"max_files_changed": 77, "max_loc_changed": 999},
            "boundary": "",
        }

        report = reconcile_session(repo, ticket, git_base="main")
        assert report.budget_files.max == 77
        assert report.budget_loc.max == 999

    def test_drift_varies_with_budget_not_hardcoded(self, tmp_path: Path) -> None:
        """Three different budgets must produce three different drift scores."""
        repo = tmp_path
        _init_git_repo(repo)
        _make_changes(repo, n_files=4, loc_per_file=15)

        scores = []
        for max_f, max_l in [(4, 60), (20, 500), (200, 5000)]:
            ticket = {
                "id": f"T-{max_f}",
                "kind": "task",
                "scope": {"allow": ["**"], "deny": []},
                "budgets": {"max_files_changed": max_f, "max_loc_changed": max_l},
                "boundary": "",
            }
            report = reconcile_session(repo, ticket, git_base="main")
            scores.append(report.drift_score)

        # Scores should be strictly decreasing as budgets get looser
        assert scores[0] > scores[1] > scores[2], f"drift scores should decrease as budget loosens: {scores}"

    def test_no_budgets_uses_reconcile_defaults(self, tmp_path: Path) -> None:
        """When ticket has no budgets, reconcile uses documented defaults (12/400)."""
        repo = tmp_path
        _init_git_repo(repo)
        _make_changes(repo, n_files=1, loc_per_file=5)

        ticket_with = {
            "id": "T-WITH",
            "kind": "task",
            "scope": {"allow": ["**"], "deny": []},
            "budgets": {"max_files_changed": 12, "max_loc_changed": 400},
            "boundary": "",
        }
        ticket_without = {
            "id": "T-WITHOUT",
            "kind": "task",
            "scope": {"allow": ["**"], "deny": []},
            "budgets": {},
            "boundary": "",
        }

        report_with = reconcile_session(repo, ticket_with, git_base="main")
        report_without = reconcile_session(repo, ticket_without, git_base="main")

        # Both should produce identical results (default = 12/400)
        assert report_with.budget_files.max == report_without.budget_files.max == 12
        assert report_with.budget_loc.max == report_without.budget_loc.max == 400
        assert report_with.drift_score == report_without.drift_score


# ──────────────────────────────────────────────
# CI Adapter: GitHub Action workflow generation
# ──────────────────────────────────────────────


class TestCIAdapterGeneration:
    """Verify ci target generates a valid GitHub Action workflow."""

    def test_ci_target_in_adapter_targets(self) -> None:
        assert "ci" in ADAPTER_TARGETS

    def test_ci_target_file_path(self) -> None:
        assert TARGET_FILES["ci"] == ".github/workflows/exo-governance.yml"

    def test_generate_ci_produces_valid_yaml(self, tmp_path: Path) -> None:
        """CI adapter output is valid YAML with expected GitHub Action structure."""
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, targets=["ci"], dry_run=True)
        content = result["files"]["ci"]["content"]
        assert "name: ExoProtocol Governance" in content
        assert "on:" in content
        assert "pull_request:" in content
        assert "branches: [main]" in content
        assert "jobs:" in content
        assert "governance-check:" in content

    def test_ci_workflow_runs_pr_check(self, tmp_path: Path) -> None:
        """Workflow must invoke exo pr-check."""
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, targets=["ci"], dry_run=True)
        content = result["files"]["ci"]["content"]
        assert "pr-check" in content
        assert "github.event.pull_request.base.sha" in content
        assert "github.event.pull_request.head.sha" in content

    def test_ci_workflow_installs_exo(self, tmp_path: Path) -> None:
        """Workflow must install ExoProtocol."""
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, targets=["ci"], dry_run=True)
        content = result["files"]["ci"]["content"]
        assert "Install ExoProtocol" in content
        assert "pip install" in content

    def test_ci_workflow_uploads_artifact(self, tmp_path: Path) -> None:
        """Workflow must upload governance report as artifact."""
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, targets=["ci"], dry_run=True)
        content = result["files"]["ci"]["content"]
        assert "upload-artifact" in content
        assert "exo-governance-report" in content

    def test_ci_workflow_includes_governance_hash(self, tmp_path: Path) -> None:
        """Workflow header must include the governance hash."""
        repo = _bootstrap_repo(tmp_path)
        lock = _load_governance_lock(repo)
        expected_hash_prefix = lock["source_hash"][:16]
        result = generate_adapters(repo, targets=["ci"], dry_run=True)
        content = result["files"]["ci"]["content"]
        assert expected_hash_prefix in content

    def test_ci_writes_to_github_workflows_dir(self, tmp_path: Path) -> None:
        """Non-dry-run creates .github/workflows/ directory."""
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, targets=["ci"])
        assert (repo / ".github" / "workflows" / "exo-governance.yml").exists()
        assert ".github/workflows/exo-governance.yml" in result["written"]

    def test_ci_workflow_includes_trace_reqs_step(self, tmp_path: Path) -> None:
        """Workflow must include trace-reqs --check-tests step."""
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, targets=["ci"], dry_run=True)
        content = result["files"]["ci"]["content"]
        assert "trace-reqs --check-tests" in content
        assert "acceptance criteria" in content.lower()

    def test_ci_idempotent_regeneration(self, tmp_path: Path) -> None:
        """Running ci generate twice produces identical output."""
        repo = _bootstrap_repo(tmp_path)
        generate_adapters(repo, targets=["ci"])
        content1 = (repo / ".github" / "workflows" / "exo-governance.yml").read_text()
        generate_adapters(repo, targets=["ci"])
        content2 = (repo / ".github" / "workflows" / "exo-governance.yml").read_text()
        assert content1 == content2

    def test_all_targets_includes_ci(self, tmp_path: Path) -> None:
        """Generating all targets includes ci."""
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo)
        assert "ci" in result["files"]
        assert (repo / ".github" / "workflows" / "exo-governance.yml").exists()


class TestCIManifestConformance:
    """Verify CI workflow values come from config, not hardcoded."""

    def test_drift_threshold_from_config(self, tmp_path: Path) -> None:
        """Custom drift_threshold in ci config must appear in workflow."""
        repo = _bootstrap_repo(tmp_path, ci_config={"drift_threshold": 0.42})
        result = generate_adapters(repo, targets=["ci"], dry_run=True)
        content = result["files"]["ci"]["content"]
        assert "0.42" in content
        # Default 0.7 must NOT appear
        assert "drift-threshold 0.7" not in content

    def test_python_version_from_config(self, tmp_path: Path) -> None:
        """Custom python_version in ci config must appear in workflow."""
        repo = _bootstrap_repo(tmp_path, ci_config={"python_version": "3.12"})
        result = generate_adapters(repo, targets=["ci"], dry_run=True)
        content = result["files"]["ci"]["content"]
        assert '"3.12"' in content

    def test_install_command_from_config(self, tmp_path: Path) -> None:
        """Custom install_command in ci config must appear in workflow."""
        repo = _bootstrap_repo(tmp_path, ci_config={"install_command": "pip install exo-protocol"})
        result = generate_adapters(repo, targets=["ci"], dry_run=True)
        content = result["files"]["ci"]["content"]
        assert "pip install exo-protocol" in content
        # Default install command must NOT appear
        assert "pip install -e ." not in content

    def test_default_values_when_no_ci_config(self, tmp_path: Path) -> None:
        """Without ci config, defaults are used."""
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, targets=["ci"], dry_run=True)
        content = result["files"]["ci"]["content"]
        assert "drift-threshold 0.7" in content
        assert '"3.11"' in content
        assert "pip install -e ." in content

    def test_checks_allowlist_appears_in_ci(self, tmp_path: Path) -> None:
        """Governed checks from config appear as a workflow step."""
        repo = _bootstrap_repo(tmp_path, checks=["pytest tests/", "mypy src/"])
        result = generate_adapters(repo, targets=["ci"], dry_run=True)
        content = result["files"]["ci"]["content"]
        assert "pytest tests/" in content
        assert "mypy src/" in content
        assert "governed checks" in content.lower()

    def test_no_checks_no_checks_step(self, tmp_path: Path) -> None:
        """Without checks, no governed checks step appears."""
        repo = _bootstrap_repo(tmp_path, checks=[])
        # Overwrite config without checks
        (repo / ".exo" / "config.yaml").write_text(
            "version: 1\ndefaults:\n  ticket_budgets:\n    max_files_changed: 5\n    max_loc_changed: 100\n"
        )
        result = generate_adapters(repo, targets=["ci"], dry_run=True)
        content = result["files"]["ci"]["content"]
        assert "governed checks" not in content.lower()

    def test_changing_ci_config_changes_output(self, tmp_path: Path) -> None:
        """Regenerating after ci config change produces different output."""
        repo = _bootstrap_repo(tmp_path, ci_config={"drift_threshold": 0.5})
        result1 = generate_adapters(repo, targets=["ci"], dry_run=True)

        # Update config
        config_text = """\
version: 1
defaults:
  ticket_budgets:
    max_files_changed: 10
    max_loc_changed: 300
checks_allowlist:
  - pytest
ci:
  drift_threshold: 0.9
  python_version: "3.13"
"""
        (repo / ".exo" / "config.yaml").write_text(config_text)

        result2 = generate_adapters(repo, targets=["ci"], dry_run=True)
        content1 = result1["files"]["ci"]["content"]
        content2 = result2["files"]["ci"]["content"]
        assert content1 != content2
        assert "0.9" in content2
        assert '"3.13"' in content2


# ──────────────────────────────────────────────
# Marker Helpers: unit tests for merge primitives
# ──────────────────────────────────────────────


class TestMarkerHelpers:
    """Verify low-level marker wrap/extract/count helpers."""

    def test_wrap_with_markers_contains_begin_end(self) -> None:
        result = _wrap_with_markers("hello", "abc123")
        assert EXO_MARKER_BEGIN in result
        assert EXO_MARKER_END in result

    def test_wrap_with_markers_embeds_governance_hash(self) -> None:
        result = _wrap_with_markers("hello", "abc123def456789012345678")
        assert "<!-- Governance hash: abc123def4567890 -->" in result

    def test_extract_marker_sections_with_markers(self) -> None:
        content = f"user stuff\n{EXO_MARKER_BEGIN}\ngoverned\n{EXO_MARKER_END}\nmore user"
        sections = _extract_marker_sections(content)
        assert sections is not None
        before, governed, after = sections
        assert "user stuff" in before
        assert EXO_MARKER_BEGIN in governed
        assert "more user" in after

    def test_extract_marker_sections_without_markers(self) -> None:
        content = "just user content\nno markers here"
        assert _extract_marker_sections(content) is None

    def test_count_user_lines_with_markers(self) -> None:
        governed = _wrap_with_markers("line1\nline2\nline3", "hash123")
        content = f"user line A\nuser line B\n\n{governed}\nuser line C\n"
        count = _count_user_lines(content)
        assert count == 3  # A, B, C (blank lines excluded)

    def test_count_user_lines_without_markers(self) -> None:
        content = "line1\nline2\n\nline3\n"
        count = _count_user_lines(content)
        assert count == 3


# ──────────────────────────────────────────────
# Brownfield Merge: adapter generation with existing files
# ──────────────────────────────────────────────


class TestBrownfieldMerge:
    """Verify adapter generation preserves user content in existing files."""

    def test_greenfield_wraps_in_markers(self, tmp_path: Path) -> None:
        """New file (no existing) gets content wrapped in markers."""
        repo = _bootstrap_repo(tmp_path)
        generate_adapters(repo, targets=["claude"])
        content = (repo / "CLAUDE.md").read_text()
        assert EXO_MARKER_BEGIN in content
        assert EXO_MARKER_END in content
        assert "ExoProtocol" in content

    def test_brownfield_no_markers_appends(self, tmp_path: Path) -> None:
        """Existing file without markers gets governance appended."""
        repo = _bootstrap_repo(tmp_path)
        user_content = "# My Project\n\nCustom instructions for Claude.\n"
        (repo / "CLAUDE.md").write_text(user_content, encoding="utf-8")

        result = generate_adapters(repo, targets=["claude"])
        content = (repo / "CLAUDE.md").read_text()

        # User content preserved
        assert "# My Project" in content
        assert "Custom instructions for Claude." in content
        # Governance appended with markers
        assert EXO_MARKER_BEGIN in content
        assert EXO_MARKER_END in content
        assert "ExoProtocol" in content
        # Result reports merge
        assert result["files"]["claude"]["merged"] is True
        assert result["files"]["claude"]["had_markers"] is False

    def test_brownfield_no_markers_creates_backup(self, tmp_path: Path) -> None:
        """First merge of unmarked file creates .pre-exo backup."""
        repo = _bootstrap_repo(tmp_path)
        user_content = "# My Project\n\nOriginal content.\n"
        (repo / "CLAUDE.md").write_text(user_content, encoding="utf-8")

        result = generate_adapters(repo, targets=["claude"])
        backup_path = repo / "CLAUDE.md.pre-exo"

        assert backup_path.exists()
        assert backup_path.read_text() == user_content
        assert result["files"]["claude"]["backed_up"] is True

    def test_brownfield_with_markers_replaces_governed(self, tmp_path: Path) -> None:
        """File with existing markers gets governed section replaced only."""
        repo = _bootstrap_repo(tmp_path)
        # First generate to create markers
        generate_adapters(repo, targets=["claude"])
        # Add user content before and after markers
        content = (repo / "CLAUDE.md").read_text()
        content = "# User Header\n\n" + content + "\n# User Footer\n"
        (repo / "CLAUDE.md").write_text(content, encoding="utf-8")

        # Regenerate
        result = generate_adapters(repo, targets=["claude"])
        new_content = (repo / "CLAUDE.md").read_text()

        assert "# User Header" in new_content
        assert "# User Footer" in new_content
        assert EXO_MARKER_BEGIN in new_content
        assert result["files"]["claude"]["merged"] is True
        assert result["files"]["claude"]["had_markers"] is True

    def test_brownfield_with_markers_no_backup(self, tmp_path: Path) -> None:
        """File already with markers does NOT create a backup."""
        repo = _bootstrap_repo(tmp_path)
        generate_adapters(repo, targets=["claude"])
        # Regenerate — should not create backup
        generate_adapters(repo, targets=["claude"])
        assert not (repo / "CLAUDE.md.pre-exo").exists()

    def test_user_content_preserved_across_regeneration(self, tmp_path: Path) -> None:
        """Two consecutive regenerations preserve user content each time."""
        repo = _bootstrap_repo(tmp_path)
        user_content = "# My Custom Rules\n\nDo not touch production.\n"
        (repo / "CLAUDE.md").write_text(user_content, encoding="utf-8")

        generate_adapters(repo, targets=["claude"])
        generate_adapters(repo, targets=["claude"])

        content = (repo / "CLAUDE.md").read_text()
        assert "# My Custom Rules" in content
        assert "Do not touch production." in content
        assert content.count(EXO_MARKER_BEGIN) == 1

    def test_merge_reports_user_lines(self, tmp_path: Path) -> None:
        """Result dict includes user_lines count."""
        repo = _bootstrap_repo(tmp_path)
        user_content = "line one\nline two\nline three\n"
        (repo / "CLAUDE.md").write_text(user_content, encoding="utf-8")

        result = generate_adapters(repo, targets=["claude"])
        assert result["files"]["claude"]["user_lines"] == 3

    def test_ci_target_not_merged(self, tmp_path: Path) -> None:
        """CI target is unconditionally overwritten (no markers)."""
        repo = _bootstrap_repo(tmp_path)
        ci_dir = repo / ".github" / "workflows"
        ci_dir.mkdir(parents=True, exist_ok=True)
        (ci_dir / "exo-governance.yml").write_text("# old ci stuff\n", encoding="utf-8")

        result = generate_adapters(repo, targets=["ci"])
        content = (repo / ".github" / "workflows" / "exo-governance.yml").read_text()

        assert "# old ci stuff" not in content
        assert "ExoProtocol Governance" in content
        assert "merged" not in result["files"]["ci"]

    def test_all_agent_targets_merge_independently(self, tmp_path: Path) -> None:
        """Each agent target merges with its own existing file independently."""
        repo = _bootstrap_repo(tmp_path)
        (repo / "CLAUDE.md").write_text("claude user content\n", encoding="utf-8")
        (repo / ".cursorrules").write_text("cursor user content\n", encoding="utf-8")
        (repo / "AGENTS.md").write_text("agents user content\n", encoding="utf-8")

        result = generate_adapters(repo, targets=["claude", "cursor", "agents"])

        for target, user_text in [
            ("claude", "claude user content"),
            ("cursor", "cursor user content"),
            ("agents", "agents user content"),
        ]:
            content = (repo / TARGET_FILES[target]).read_text()
            assert user_text in content, f"{target} should preserve user content"
            assert EXO_MARKER_BEGIN in content, f"{target} should have markers"
            assert result["files"][target]["merged"] is True


# ──────────────────────────────────────────────
# Drift detection with markers (governance hash in merged files)
# ──────────────────────────────────────────────


class TestDriftWithMarkers:
    """Verify drift detection finds governance hash inside merged files."""

    def test_drift_finds_hash_in_merged_file(self, tmp_path: Path) -> None:
        """Drift check passes when governance hash is inside markers."""
        repo = _bootstrap_repo(tmp_path)
        (repo / "CLAUDE.md").write_text("# Existing content\n", encoding="utf-8")
        generate_adapters(repo)

        from exo.stdlib.drift import _check_adapters

        section = _check_adapters(repo)
        # All adapter files should be fresh (including merged CLAUDE.md)
        assert section.status == "pass", f"Expected pass, got {section.status}: {section.summary}"

    def test_drift_detects_stale_merged_file(self, tmp_path: Path) -> None:
        """Drift check fails when constitution changes after merge."""
        repo = _bootstrap_repo(tmp_path)
        (repo / "CLAUDE.md").write_text("# Existing content\n", encoding="utf-8")
        generate_adapters(repo)

        # Modify constitution to change governance hash
        old_const = (repo / ".exo" / "CONSTITUTION.md").read_text()
        new_const = old_const + _policy_block(
            {
                "id": "RULE-EXTRA-999",
                "type": "filesystem_deny",
                "patterns": ["*.bak"],
                "actions": ["write"],
                "message": "No backups",
            }
        )
        (repo / ".exo" / "CONSTITUTION.md").write_text(new_const)
        governance_mod.compile_constitution(repo)

        from exo.stdlib.drift import _check_adapters

        section = _check_adapters(repo)
        assert section.status == "fail"

    def test_drift_finds_hash_beyond_500_chars(self, tmp_path: Path) -> None:
        """Drift hash detection works even if hash is past byte 500."""
        repo = _bootstrap_repo(tmp_path)
        # Create a large user content block that pushes markers past 500 chars
        big_content = "# Big Header\n\n" + ("x" * 600) + "\n\n"
        (repo / "CLAUDE.md").write_text(big_content, encoding="utf-8")
        generate_adapters(repo)

        from exo.stdlib.drift import _check_adapters

        section = _check_adapters(repo)
        assert section.status == "pass", f"Expected pass, got {section.status}: {section.summary}"


# ── Tool Reuse Protocol in Adapters ────────────────────────────────


class TestToolReuseProtocolInAdapters:
    def test_claude_includes_tool_protocol_with_tools(self, tmp_path: Path) -> None:
        """CLAUDE.md includes Tool Reuse Protocol when tools.yaml exists."""
        from exo.kernel.utils import dump_yaml

        repo = _bootstrap_repo(tmp_path)
        tools_path = repo / ".exo" / "tools.yaml"
        dump_yaml(
            tools_path,
            {
                "tools": [
                    {
                        "id": "lib.csv:parse",
                        "module": "lib/csv.py",
                        "function": "parse",
                        "description": "Parse CSV files",
                        "tags": ["csv", "parsing"],
                    }
                ]
            },
        )
        generate_adapters(repo, targets=["claude"])
        content = (repo / "CLAUDE.md").read_text()
        assert "Tool Reuse Protocol" in content
        assert "exo tool-search" in content
        assert "exo tool-register" in content
        assert "lib.csv:parse" in content
        assert "Parse CSV files" in content

    def test_cursor_includes_tool_protocol(self, tmp_path: Path) -> None:
        """Cursor adapter also gets the tool protocol."""
        from exo.kernel.utils import dump_yaml

        repo = _bootstrap_repo(tmp_path)
        tools_path = repo / ".exo" / "tools.yaml"
        dump_yaml(tools_path, {"tools": [{"id": "t1:fn", "module": "t1.py", "function": "fn", "description": "Test"}]})
        generate_adapters(repo, targets=["cursor"])
        content = (repo / ".cursorrules").read_text()
        assert "Tool Reuse Protocol" in content
        assert "t1:fn" in content

    def test_agents_includes_tool_protocol(self, tmp_path: Path) -> None:
        """AGENTS.md also gets the tool protocol."""
        from exo.kernel.utils import dump_yaml

        repo = _bootstrap_repo(tmp_path)
        tools_path = repo / ".exo" / "tools.yaml"
        dump_yaml(tools_path, {"tools": [{"id": "t1:fn", "module": "t1.py", "function": "fn", "description": "Test"}]})
        generate_adapters(repo, targets=["agents"])
        content = (repo / "AGENTS.md").read_text()
        assert "Tool Reuse Protocol" in content

    def test_no_tools_yaml_still_has_protocol(self, tmp_path: Path) -> None:
        """When no tools.yaml exists, protocol section is still injected (empty)."""
        repo = _bootstrap_repo(tmp_path)
        generate_adapters(repo, targets=["claude"])
        content = (repo / "CLAUDE.md").read_text()
        assert "Tool Reuse Protocol" in content
        assert "exo tool-search" in content
        # No registered tools section
        assert "Registered Tools" not in content

    def test_ci_does_not_include_tool_protocol(self, tmp_path: Path) -> None:
        """CI adapter should NOT include tool protocol (it's a YAML workflow)."""
        from exo.kernel.utils import dump_yaml

        repo = _bootstrap_repo(tmp_path)
        tools_path = repo / ".exo" / "tools.yaml"
        dump_yaml(tools_path, {"tools": [{"id": "t1:fn", "module": "t1.py", "function": "fn", "description": "Test"}]})
        generate_adapters(repo, targets=["ci"])
        content = (repo / ".github" / "workflows" / "exo-governance.yml").read_text()
        assert "Tool Reuse Protocol" not in content

    def test_tool_tags_in_adapter(self, tmp_path: Path) -> None:
        """Tool tags appear in adapter output."""
        from exo.kernel.utils import dump_yaml

        repo = _bootstrap_repo(tmp_path)
        tools_path = repo / ".exo" / "tools.yaml"
        dump_yaml(
            tools_path,
            {"tools": [{"id": "t:fn", "module": "t.py", "function": "fn", "description": "D", "tags": ["csv", "io"]}]},
        )
        generate_adapters(repo, targets=["claude"])
        content = (repo / "CLAUDE.md").read_text()
        assert "[csv, io]" in content

    def test_multiple_tools_listed(self, tmp_path: Path) -> None:
        """Multiple tools are all listed in adapter output."""
        from exo.kernel.utils import dump_yaml

        repo = _bootstrap_repo(tmp_path)
        tools_path = repo / ".exo" / "tools.yaml"
        dump_yaml(
            tools_path,
            {
                "tools": [
                    {"id": "a:fn", "module": "a.py", "function": "fn", "description": "Tool A"},
                    {"id": "b:fn", "module": "b.py", "function": "fn", "description": "Tool B"},
                    {"id": "c:fn", "module": "c.py", "function": "fn", "description": "Tool C"},
                ]
            },
        )
        generate_adapters(repo, targets=["claude"])
        content = (repo / "CLAUDE.md").read_text()
        assert "Registered Tools (3)" in content
        assert "a:fn" in content
        assert "b:fn" in content
        assert "c:fn" in content


# ── Sandbox Adapter Tests ─────────────────────────────────────────


class TestDerivePermissionDeny:
    """Tests for _derive_permission_deny() helper."""

    def test_filesystem_deny_to_permissions(self) -> None:
        rules = [
            {
                "type": "filesystem_deny",
                "patterns": ["**/.env*"],
                "actions": ["read", "write"],
            }
        ]
        result = _derive_permission_deny(rules)
        assert "Read(**/.env*)" in result
        assert "Edit(**/.env*)" in result

    def test_read_only_action(self) -> None:
        rules = [
            {
                "type": "filesystem_deny",
                "patterns": ["secrets/**"],
                "actions": ["read"],
            }
        ]
        result = _derive_permission_deny(rules)
        assert "Read(secrets/**)" in result
        assert not any(e.startswith("Edit(") for e in result)

    def test_write_only_action(self) -> None:
        rules = [
            {
                "type": "filesystem_deny",
                "patterns": ["dist/**"],
                "actions": ["write"],
            }
        ]
        result = _derive_permission_deny(rules)
        assert "Edit(dist/**)" in result
        assert not any(e.startswith("Read(") for e in result)

    def test_empty_rules(self) -> None:
        result = _derive_permission_deny([])
        assert result == []

    def test_skips_non_filesystem_rules(self) -> None:
        rules = [
            {"type": "require_lock", "message": "Lock required"},
        ]
        result = _derive_permission_deny(rules)
        assert result == []

    def test_multiple_patterns(self) -> None:
        rules = [
            {
                "type": "filesystem_deny",
                "patterns": ["**/.env*", "**/credentials.json"],
                "actions": ["read", "write"],
            }
        ]
        result = _derive_permission_deny(rules)
        assert len(result) == 4
        assert "Read(**/.env*)" in result
        assert "Edit(**/.env*)" in result
        assert "Read(**/credentials.json)" in result
        assert "Edit(**/credentials.json)" in result

    def test_multiple_rules_combined(self) -> None:
        rules = [
            {"type": "filesystem_deny", "patterns": ["**/.env*"], "actions": ["read", "write"]},
            {"type": "filesystem_deny", "patterns": ["dist/**"], "actions": ["write"]},
        ]
        result = _derive_permission_deny(rules)
        assert "Read(**/.env*)" in result
        assert "Edit(**/.env*)" in result
        assert "Edit(dist/**)" in result

    def test_feature_locked_deny(self, tmp_path: Path) -> None:
        from exo.kernel.utils import dump_yaml

        repo = _bootstrap_repo(tmp_path)
        features_path = repo / ".exo" / "features.yaml"
        dump_yaml(
            features_path,
            {
                "features": [
                    {
                        "id": "locked-feat",
                        "status": "active",
                        "allow_agent_edit": False,
                        "files": ["src/core/**"],
                    }
                ]
            },
        )
        rules = [
            {"type": "filesystem_deny", "patterns": ["**/.env*"], "actions": ["read", "write"]},
        ]
        result = _derive_permission_deny(rules, repo=repo)
        # Should have constitution deny + feature locked deny
        assert "Read(**/.env*)" in result
        assert "Edit(**/.env*)" in result
        # Feature locked paths should appear as Edit deny
        feature_edits = [e for e in result if e.startswith("Edit(") and ".env" not in e]
        assert len(feature_edits) > 0


class TestSandboxAdapter:
    """Tests for the sandbox adapter target (.claude/settings.json)."""

    def test_sandbox_in_targets(self) -> None:
        assert "sandbox" in ADAPTER_TARGETS
        assert "sandbox" not in AGENT_ADAPTER_TARGETS

    def test_sandbox_target_file(self) -> None:
        assert TARGET_FILES["sandbox"] == ".claude/settings.json"

    def test_sandbox_generates_settings_json(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        generate_adapters(repo, targets=["sandbox"])
        settings_path = repo / ".claude" / "settings.json"
        assert settings_path.exists()
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert "permissions" in settings
        assert "deny" in settings["permissions"]

    def test_deny_rules_mapped(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        generate_adapters(repo, targets=["sandbox"])
        settings = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
        deny = settings["permissions"]["deny"]
        assert "Read(**/.env*)" in deny
        assert "Edit(**/.env*)" in deny

    def test_governance_hash_embedded(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        generate_adapters(repo, targets=["sandbox"])
        settings = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert "_exo_governance_hash" in settings
        assert len(settings["_exo_governance_hash"]) == 16

    def test_merges_with_existing_hooks(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        claude_dir = repo / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        existing = {
            "hooks": {"SessionStart": [{"matcher": "startup", "hooks": []}]},
            "customKey": "preserved",
        }
        (claude_dir / "settings.json").write_text(json.dumps(existing), encoding="utf-8")
        generate_adapters(repo, targets=["sandbox"])
        settings = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
        # Hooks and custom keys preserved
        assert settings["hooks"] == existing["hooks"]
        assert settings["customKey"] == "preserved"
        # Permissions added
        assert "permissions" in settings
        assert "Read(**/.env*)" in settings["permissions"]["deny"]

    def test_dry_run_no_write(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, targets=["sandbox"], dry_run=True)
        settings_path = repo / ".claude" / "settings.json"
        assert not settings_path.exists()
        assert "content" in result["files"]["sandbox"]
        content = json.loads(result["files"]["sandbox"]["content"])
        assert "permissions" in content

    def test_multiple_deny_rules(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(
            tmp_path,
            extra_deny_rules=[
                {
                    "id": "RULE-SEC-002",
                    "type": "filesystem_deny",
                    "patterns": ["dist/**"],
                    "actions": ["write"],
                    "message": "No dist writes",
                }
            ],
        )
        generate_adapters(repo, targets=["sandbox"])
        settings = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
        deny = settings["permissions"]["deny"]
        assert "Read(**/.env*)" in deny
        assert "Edit(**/.env*)" in deny
        assert "Edit(dist/**)" in deny

    def test_idempotent(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        generate_adapters(repo, targets=["sandbox"])
        first = (repo / ".claude" / "settings.json").read_text(encoding="utf-8")
        generate_adapters(repo, targets=["sandbox"])
        second = (repo / ".claude" / "settings.json").read_text(encoding="utf-8")
        assert first == second

    def test_merged_flag_with_existing(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        claude_dir = repo / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "settings.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        result = generate_adapters(repo, targets=["sandbox"])
        assert result["files"]["sandbox"]["merged"] is True

    def test_merged_flag_no_existing(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, targets=["sandbox"])
        assert result["files"]["sandbox"]["merged"] is False

    def test_read_only_deny_rule(self, tmp_path: Path) -> None:
        """Constitution rule with only read action produces only Read() entries."""
        repo = _bootstrap_repo(
            tmp_path,
            extra_deny_rules=[
                {
                    "id": "RULE-SEC-READONLY",
                    "type": "filesystem_deny",
                    "patterns": ["audit/**"],
                    "actions": ["read"],
                    "message": "Audit logs read-deny",
                }
            ],
        )
        generate_adapters(repo, targets=["sandbox"])
        settings = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
        deny = settings["permissions"]["deny"]
        assert "Read(audit/**)" in deny
        assert "Edit(audit/**)" not in deny


# ── Delete Action + Bash Deny Tests ─────────────────────────────


class TestDeleteActionDeny:
    """Tests for delete action mapping in _derive_permission_deny()."""

    def test_delete_action_produces_edit_and_bash(self) -> None:
        rules = [
            {
                "type": "filesystem_deny",
                "patterns": ["backups/**"],
                "actions": ["delete"],
            }
        ]
        result = _derive_permission_deny(rules)
        assert "Edit(backups/**)" in result
        assert "Bash(rm backups/**)" in result

    def test_delete_with_write_no_duplicate_edit(self) -> None:
        """When both write and delete are present, Edit appears only once."""
        rules = [
            {
                "type": "filesystem_deny",
                "patterns": ["data/**"],
                "actions": ["write", "delete"],
            }
        ]
        result = _derive_permission_deny(rules)
        edit_count = sum(1 for e in result if e == "Edit(data/**)")
        assert edit_count == 1  # write produces Edit, delete skips duplicate
        assert "Bash(rm data/**)" in result

    def test_delete_only_no_read(self) -> None:
        """Delete-only rule should NOT produce Read entries."""
        rules = [
            {
                "type": "filesystem_deny",
                "patterns": ["tmp/**"],
                "actions": ["delete"],
            }
        ]
        result = _derive_permission_deny(rules)
        assert not any(e.startswith("Read(") for e in result)

    def test_all_three_actions(self) -> None:
        """read + write + delete produces Read, Edit (once), and Bash(rm)."""
        rules = [
            {
                "type": "filesystem_deny",
                "patterns": ["**/.secrets"],
                "actions": ["read", "write", "delete"],
            }
        ]
        result = _derive_permission_deny(rules)
        assert "Read(**/.secrets)" in result
        assert "Edit(**/.secrets)" in result
        assert "Bash(rm **/.secrets)" in result
        # Edit should appear exactly once
        edit_count = sum(1 for e in result if e == "Edit(**/.secrets)")
        assert edit_count == 1

    def test_delete_multiple_patterns(self) -> None:
        rules = [
            {
                "type": "filesystem_deny",
                "patterns": ["logs/**", "*.bak"],
                "actions": ["delete"],
            }
        ]
        result = _derive_permission_deny(rules)
        assert "Bash(rm logs/**)" in result
        assert "Bash(rm *.bak)" in result
        assert "Edit(logs/**)" in result
        assert "Edit(*.bak)" in result

    def test_delete_in_sandbox_adapter(self, tmp_path: Path) -> None:
        """Delete action flows through to .claude/settings.json deny list."""
        repo = _bootstrap_repo(
            tmp_path,
            extra_deny_rules=[
                {
                    "id": "RULE-NO-DELETE",
                    "type": "filesystem_deny",
                    "patterns": ["archive/**"],
                    "actions": ["delete"],
                    "message": "No archive deletion",
                }
            ],
        )
        generate_adapters(repo, targets=["sandbox"])
        settings = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
        deny = settings["permissions"]["deny"]
        assert "Edit(archive/**)" in deny
        assert "Bash(rm archive/**)" in deny


# ── derive_sandbox_policy() Public API Tests ─────────────────────


class TestDeriveSandboxPolicy:
    """Tests for the derive_sandbox_policy() public API."""

    def test_returns_deny_entries(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        policy = derive_sandbox_policy(repo)
        assert "deny_entries" in policy
        assert "Read(**/.env*)" in policy["deny_entries"]
        assert "Edit(**/.env*)" in policy["deny_entries"]

    def test_returns_source_rules(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        policy = derive_sandbox_policy(repo)
        assert "source_rules" in policy
        assert len(policy["source_rules"]) >= 1
        assert policy["source_rules"][0]["id"] == "RULE-SEC-001"

    def test_returns_governance_hash(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        policy = derive_sandbox_policy(repo)
        assert "governance_hash" in policy
        assert len(policy["governance_hash"]) == 16

    def test_deny_count_matches(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        policy = derive_sandbox_policy(repo)
        assert policy["deny_count"] == len(policy["deny_entries"])

    def test_source_rule_count_matches(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        policy = derive_sandbox_policy(repo)
        assert policy["source_rule_count"] == len(policy["source_rules"])

    def test_delete_action_in_policy(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(
            tmp_path,
            extra_deny_rules=[
                {
                    "id": "RULE-NO-DEL",
                    "type": "filesystem_deny",
                    "patterns": ["vault/**"],
                    "actions": ["delete"],
                    "message": "No vault deletion",
                }
            ],
        )
        policy = derive_sandbox_policy(repo)
        assert "Bash(rm vault/**)" in policy["deny_entries"]
        assert "Edit(vault/**)" in policy["deny_entries"]

    def test_no_governance_lock_raises(self, tmp_path: Path) -> None:
        """derive_sandbox_policy raises when no governance lock exists."""
        import pytest

        from exo.kernel.errors import ExoError

        with pytest.raises(ExoError):
            derive_sandbox_policy(tmp_path)


# ── format_sandbox_policy_human() Tests ──────────────────────────


class TestFormatSandboxPolicyHuman:
    """Tests for human-readable sandbox policy formatting."""

    def test_includes_governance_hash(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        policy = derive_sandbox_policy(repo)
        human = format_sandbox_policy_human(policy)
        assert "Governance hash:" in human

    def test_includes_deny_entries(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        policy = derive_sandbox_policy(repo)
        human = format_sandbox_policy_human(policy)
        assert "Read(**/.env*)" in human
        assert "Edit(**/.env*)" in human

    def test_includes_source_rules(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        policy = derive_sandbox_policy(repo)
        human = format_sandbox_policy_human(policy)
        assert "RULE-SEC-001" in human

    def test_shows_entry_count(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        policy = derive_sandbox_policy(repo)
        human = format_sandbox_policy_human(policy)
        assert f"({policy['deny_count']} entries)" in human


# ── Codex Adapter Target ─────────────────────────────────────────


class TestCodexAdapter:
    def test_codex_in_adapter_targets(self) -> None:
        assert "codex" in ADAPTER_TARGETS

    def test_codex_in_agent_adapter_targets(self) -> None:
        assert "codex" in AGENT_ADAPTER_TARGETS

    def test_codex_target_file_mapping(self) -> None:
        assert TARGET_FILES["codex"] == "codex.md"

    def test_generate_codex_file(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, targets=["codex"])
        assert "codex.md" in result["written"]
        assert (repo / "codex.md").exists()

    def test_codex_includes_governance_preamble(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, targets=["codex"], dry_run=True)
        content = result["files"]["codex"]["content"]
        assert "ExoProtocol Governance" in content
        assert "Filesystem Deny Rules" in content

    def test_codex_has_session_lifecycle(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, targets=["codex"], dry_run=True)
        content = result["files"]["codex"]["content"]
        assert "session-start" in content
        assert "session-finish" in content
        assert "openai" in content  # vendor should be openai

    def test_codex_has_approval_mode(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, targets=["codex"], dry_run=True)
        content = result["files"]["codex"]["content"]
        assert "Approval Mode" in content
        assert "suggest" in content or "auto-edit" in content

    def test_codex_has_sandbox_policy(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, targets=["codex"], dry_run=True)
        content = result["files"]["codex"]["content"]
        assert "Sandbox Policy" in content
        assert ".env" in content  # from deny rules

    def test_codex_budgets_from_config(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path, max_files=42, max_loc=777)
        result = generate_adapters(repo, targets=["codex"], dry_run=True)
        content = result["files"]["codex"]["content"]
        assert "42" in content

    def test_codex_checks_from_config(self, tmp_path: Path) -> None:
        repo = _bootstrap_repo(tmp_path, checks=["ruff check", "pytest"])
        result = generate_adapters(repo, targets=["codex"], dry_run=True)
        content = result["files"]["codex"]["content"]
        assert "ruff check" in content
        assert "pytest" in content

    def test_codex_brownfield_merge(self, tmp_path: Path) -> None:
        """Codex adapter should use brownfield merge like other agent targets."""
        repo = _bootstrap_repo(tmp_path)
        # Write existing user content
        (repo / "codex.md").write_text("# My Codex Config\n\nCustom instructions here.\n")
        result = generate_adapters(repo, targets=["codex"])
        content = (repo / "codex.md").read_text(encoding="utf-8")
        # User content preserved, governance section added
        assert "My Codex Config" in content
        assert "ExoProtocol" in content
        assert result["files"]["codex"]["merged"] is True


# ---------------------------------------------------------------------------
# Provenance: intent/ticket context in adapter output (TKT-20260217-220659-KSCJ)
# ---------------------------------------------------------------------------


class TestProvenance:
    def test_active_intent_appears_in_adapter(self, tmp_path: Path) -> None:
        """Active intents with boundaries appear in generated adapters."""
        repo = _bootstrap_repo(tmp_path)

        from exo.kernel.tickets import save_ticket

        intent = {
            "id": "INTENT-001",
            "title": "Auth system redesign",
            "kind": "intent",
            "status": "active",
            "boundary": "No kernel changes",
            "scope": {"allow": ["**"], "deny": []},
            "budgets": {"max_files_changed": 10},
            "children": [],
        }
        save_ticket(repo, intent)

        result = generate_adapters(repo, targets=["claude"], dry_run=True)
        content = result["files"]["claude"]["content"]
        assert "Active Intents" in content
        assert "INTENT-001" in content
        assert "Auth system redesign" in content
        assert "No kernel changes" in content

    def test_child_tickets_with_scope_shown(self, tmp_path: Path) -> None:
        """Child tickets show scope constraints under their parent intent."""
        repo = _bootstrap_repo(tmp_path)

        from exo.kernel.tickets import save_ticket

        intent = {
            "id": "INTENT-002",
            "title": "Scoped intent",
            "kind": "intent",
            "status": "active",
            "boundary": "",
            "scope": {"allow": ["**"], "deny": []},
            "budgets": {"max_files_changed": 10},
            "children": ["TICKET-001"],
        }
        save_ticket(repo, intent)

        child = {
            "id": "TICKET-001",
            "title": "Implement auth endpoint",
            "kind": "task",
            "status": "active",
            "parent_id": "INTENT-002",
            "scope": {"allow": ["src/auth/**"], "deny": ["src/auth/secrets/**"]},
            "budgets": {"max_files_changed": 3},
        }
        save_ticket(repo, child)

        result = generate_adapters(repo, targets=["claude"], dry_run=True)
        content = result["files"]["claude"]["content"]
        assert "TICKET-001" in content
        assert "Implement auth endpoint" in content
        assert "src/auth/**" in content
        assert "src/auth/secrets/**" in content

    def test_done_intents_excluded(self, tmp_path: Path) -> None:
        """Intents with status=done are not shown."""
        repo = _bootstrap_repo(tmp_path)

        from exo.kernel.tickets import save_ticket

        intent = {
            "id": "INTENT-003",
            "title": "Finished work",
            "kind": "intent",
            "status": "done",
            "scope": {"allow": ["**"], "deny": []},
            "budgets": {"max_files_changed": 10},
            "children": [],
        }
        save_ticket(repo, intent)

        result = generate_adapters(repo, targets=["claude"], dry_run=True)
        content = result["files"]["claude"]["content"]
        assert "INTENT-003" not in content

    def test_provenance_in_all_agent_targets(self, tmp_path: Path) -> None:
        """Provenance appears in all agent adapter targets."""
        repo = _bootstrap_repo(tmp_path)

        from exo.kernel.tickets import save_ticket

        intent = {
            "id": "INTENT-004",
            "title": "Universal intent",
            "kind": "intent",
            "status": "active",
            "boundary": "Stay in stdlib",
            "scope": {"allow": ["**"], "deny": []},
            "budgets": {"max_files_changed": 5},
            "children": [],
        }
        save_ticket(repo, intent)

        result = generate_adapters(repo, dry_run=True)
        for target in AGENT_ADAPTER_TARGETS:
            content = result["files"][target]["content"]
            assert "INTENT-004" in content, f"{target} adapter missing provenance"

    def test_no_tickets_no_provenance_section(self, tmp_path: Path) -> None:
        """Without tickets, no Active Intents section appears."""
        repo = _bootstrap_repo(tmp_path)
        result = generate_adapters(repo, targets=["claude"], dry_run=True)
        content = result["files"]["claude"]["content"]
        assert "Active Intents" not in content

    def test_done_child_tickets_excluded(self, tmp_path: Path) -> None:
        """Child tickets with status=done are not shown under their intent."""
        repo = _bootstrap_repo(tmp_path)

        from exo.kernel.tickets import save_ticket

        intent = {
            "id": "INTENT-005",
            "title": "Mixed children",
            "kind": "intent",
            "status": "active",
            "scope": {"allow": ["**"], "deny": []},
            "budgets": {"max_files_changed": 10},
            "children": ["TICKET-010", "TICKET-011"],
        }
        save_ticket(repo, intent)
        save_ticket(
            repo,
            {
                "id": "TICKET-010",
                "title": "Still working",
                "kind": "task",
                "status": "active",
                "parent_id": "INTENT-005",
                "scope": {"allow": ["**"], "deny": []},
                "budgets": {"max_files_changed": 5},
            },
        )
        save_ticket(
            repo,
            {
                "id": "TICKET-011",
                "title": "Already finished",
                "kind": "task",
                "status": "done",
                "parent_id": "INTENT-005",
                "scope": {"allow": ["**"], "deny": []},
                "budgets": {"max_files_changed": 5},
            },
        )

        result = generate_adapters(repo, targets=["claude"], dry_run=True)
        content = result["files"]["claude"]["content"]
        assert "TICKET-010" in content
        assert "TICKET-011" not in content
