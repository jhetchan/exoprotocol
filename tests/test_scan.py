"""Tests for Smart Init — Brownfield Repo Scanner.

Covers:
- Language detection (Python, Node, Go, Rust, Java, Ruby, multi, none)
- Sensitive file detection
- Build directory detection
- Existing governance detection
- CI system detection
- Source directory detection
- Constitution generation (rules, compilation, sentinel)
- Config generation (checks, budgets, ignore_paths)
- Smart Init integration (engine.init with scan)
- Scan idempotency
- CLI human/JSON output
- format_scan_human output
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from exo.kernel import governance as governance_mod
from exo.stdlib.defaults import DEFAULT_CONFIG
from exo.stdlib.scan import (
    LANGUAGE_BUDGETS,
    LANGUAGE_CHECKS,
    BuildDir,
    CIDetection,
    ExistingGovernance,
    LanguageDetection,
    ScanReport,
    SensitiveFile,
    _base_rules,
    _constitution_rules,
    _detect_build_dirs,
    _detect_ci,
    _detect_existing_governance,
    _detect_languages,
    _detect_sensitive_files,
    _detect_source_dirs,
    format_scan_human,
    generate_config,
    generate_constitution,
    scan_repo,
    scan_to_dict,
)

# ── Language Detection ─────────────────────────────────────────────


class TestScanDetection:
    def test_detect_python(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[build-system]", encoding="utf-8")
        (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
        langs = _detect_languages(tmp_path)
        assert len(langs) == 1
        assert langs[0].language == "python"
        assert "pyproject.toml" in langs[0].markers
        assert "requirements.txt" in langs[0].markers
        assert langs[0].confidence > 0

    def test_detect_node(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        langs = _detect_languages(tmp_path)
        assert len(langs) == 1
        assert langs[0].language == "node"
        assert "package.json" in langs[0].markers

    def test_detect_go(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module test\n", encoding="utf-8")
        (tmp_path / "go.sum").write_text("", encoding="utf-8")
        langs = _detect_languages(tmp_path)
        assert len(langs) == 1
        assert langs[0].language == "go"

    def test_detect_rust(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text("[package]", encoding="utf-8")
        langs = _detect_languages(tmp_path)
        assert len(langs) == 1
        assert langs[0].language == "rust"

    def test_detect_java_gradle(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle").write_text("", encoding="utf-8")
        langs = _detect_languages(tmp_path)
        assert len(langs) == 1
        assert langs[0].language == "java"

    def test_detect_java_maven(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
        langs = _detect_languages(tmp_path)
        assert len(langs) == 1
        assert langs[0].language == "java"

    def test_detect_ruby(self, tmp_path: Path) -> None:
        (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'", encoding="utf-8")
        langs = _detect_languages(tmp_path)
        assert len(langs) == 1
        assert langs[0].language == "ruby"

    def test_detect_multi_language(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[build-system]", encoding="utf-8")
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        langs = _detect_languages(tmp_path)
        names = {lang.language for lang in langs}
        assert "python" in names
        assert "node" in names

    def test_detect_no_language(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("Hello", encoding="utf-8")
        langs = _detect_languages(tmp_path)
        assert len(langs) == 0


# ── Sensitive File Detection ──────────────────────────────────────


class TestScanSensitiveFiles:
    def test_detect_pem_files(self, tmp_path: Path) -> None:
        certs = tmp_path / "certs"
        certs.mkdir()
        (certs / "server.pem").write_text("CERT", encoding="utf-8")
        results = _detect_sensitive_files(tmp_path)
        patterns = [sf.pattern for sf in results]
        assert "**/*.pem" in patterns

    def test_detect_credentials(self, tmp_path: Path) -> None:
        (tmp_path / "credentials.json").write_text("{}", encoding="utf-8")
        results = _detect_sensitive_files(tmp_path)
        matches_flat = [m for sf in results for m in sf.matches]
        assert any("credentials" in m for m in matches_flat)

    def test_detect_npmrc(self, tmp_path: Path) -> None:
        (tmp_path / ".npmrc").write_text("//registry.npmjs.org/:_authToken=xxx", encoding="utf-8")
        results = _detect_sensitive_files(tmp_path)
        matches_flat = [m for sf in results for m in sf.matches]
        assert any(".npmrc" in m for m in matches_flat)

    def test_empty_repo(self, tmp_path: Path) -> None:
        results = _detect_sensitive_files(tmp_path)
        assert len(results) == 0

    def test_skip_node_modules(self, tmp_path: Path) -> None:
        nm = tmp_path / "node_modules" / "some-pkg"
        nm.mkdir(parents=True)
        (nm / "server.pem").write_text("CERT", encoding="utf-8")
        results = _detect_sensitive_files(tmp_path)
        # Should be empty — node_modules is skipped
        matches_flat = [m for sf in results for m in sf.matches]
        assert not any("node_modules" in m for m in matches_flat)


# ── Build Directory Detection ────────────────────────────────────


class TestScanBuildDirs:
    def test_detect_node_modules(self, tmp_path: Path) -> None:
        (tmp_path / "node_modules").mkdir()
        langs = [LanguageDetection(language="node", markers=["package.json"], confidence=0.5)]
        dirs = _detect_build_dirs(tmp_path, langs)
        paths = [d.path for d in dirs]
        assert "node_modules" in paths

    def test_detect_pycache(self, tmp_path: Path) -> None:
        (tmp_path / "__pycache__").mkdir()
        langs = [LanguageDetection(language="python", markers=["pyproject.toml"], confidence=0.5)]
        dirs = _detect_build_dirs(tmp_path, langs)
        paths = [d.path for d in dirs]
        assert "__pycache__" in paths

    def test_detect_rust_target(self, tmp_path: Path) -> None:
        (tmp_path / "target").mkdir()
        langs = [LanguageDetection(language="rust", markers=["Cargo.toml"], confidence=0.5)]
        dirs = _detect_build_dirs(tmp_path, langs)
        paths = [d.path for d in dirs]
        assert "target" in paths

    def test_detect_common_dist(self, tmp_path: Path) -> None:
        (tmp_path / "dist").mkdir()
        dirs = _detect_build_dirs(tmp_path, [])
        paths = [d.path for d in dirs]
        assert "dist" in paths
        assert any(d.language == "common" for d in dirs if d.path == "dist")


# ── Existing Governance Detection ────────────────────────────────


class TestScanExistingGovernance:
    def test_detect_claude_md(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("# Rules", encoding="utf-8")
        found = _detect_existing_governance(tmp_path)
        kinds = [eg.kind for eg in found]
        assert "claude_md" in kinds

    def test_detect_cursorrules(self, tmp_path: Path) -> None:
        (tmp_path / ".cursorrules").write_text("rules", encoding="utf-8")
        found = _detect_existing_governance(tmp_path)
        kinds = [eg.kind for eg in found]
        assert "cursorrules" in kinds

    def test_detect_exo_dir(self, tmp_path: Path) -> None:
        (tmp_path / ".exo").mkdir()
        found = _detect_existing_governance(tmp_path)
        kinds = [eg.kind for eg in found]
        assert "exo_dir" in kinds

    def test_empty_repo(self, tmp_path: Path) -> None:
        found = _detect_existing_governance(tmp_path)
        assert len(found) == 0

    def test_detect_exo_managed_file(self, tmp_path: Path) -> None:
        """File with exo governance markers is detected as exo_managed."""
        from exo.stdlib.adapters import EXO_MARKER_BEGIN, EXO_MARKER_END

        content = f"# My Rules\n\n{EXO_MARKER_BEGIN}\ngoverned\n{EXO_MARKER_END}\n"
        (tmp_path / "CLAUDE.md").write_text(content, encoding="utf-8")
        found = _detect_existing_governance(tmp_path)
        claude = [eg for eg in found if eg.kind == "claude_md"][0]
        assert claude.exo_managed is True
        assert claude.total_lines > 0

    def test_detect_user_only_file(self, tmp_path: Path) -> None:
        """File without markers is detected as user-only."""
        (tmp_path / "CLAUDE.md").write_text("# My Rules\nline2\nline3\n", encoding="utf-8")
        found = _detect_existing_governance(tmp_path)
        claude = [eg for eg in found if eg.kind == "claude_md"][0]
        assert claude.exo_managed is False
        assert claude.user_lines == 3
        assert claude.total_lines == 3

    def test_scan_to_dict_includes_content_fields(self, tmp_path: Path) -> None:
        """scan_to_dict includes exo_managed, user_lines, total_lines."""
        (tmp_path / "CLAUDE.md").write_text("# Rules\n", encoding="utf-8")
        report = scan_repo(tmp_path)
        data = scan_to_dict(report)
        eg_list = data["existing_governance"]
        claude = [eg for eg in eg_list if eg["kind"] == "claude_md"][0]
        assert "exo_managed" in claude
        assert "user_lines" in claude
        assert "total_lines" in claude

    def test_format_scan_human_shows_content_summary(self, tmp_path: Path) -> None:
        """format_scan_human shows user-only or exo-managed per file."""
        (tmp_path / "CLAUDE.md").write_text("# My Rules\nline 2\n", encoding="utf-8")
        report = scan_repo(tmp_path)
        human = format_scan_human(report)
        assert "user-only" in human
        assert "2 lines" in human


# ── CI Detection ─────────────────────────────────────────────────


class TestScanCI:
    def test_detect_github_actions(self, tmp_path: Path) -> None:
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("name: CI", encoding="utf-8")
        found = _detect_ci(tmp_path)
        systems = [ci.system for ci in found]
        assert "github_actions" in systems

    def test_detect_gitlab_ci(self, tmp_path: Path) -> None:
        (tmp_path / ".gitlab-ci.yml").write_text("stages:", encoding="utf-8")
        found = _detect_ci(tmp_path)
        systems = [ci.system for ci in found]
        assert "gitlab_ci" in systems

    def test_detect_jenkins(self, tmp_path: Path) -> None:
        (tmp_path / "Jenkinsfile").write_text("pipeline {}", encoding="utf-8")
        found = _detect_ci(tmp_path)
        systems = [ci.system for ci in found]
        assert "jenkins" in systems


# ── Source Directory Detection ───────────────────────────────────


class TestScanSourceDirs:
    def test_detect_src(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        found = _detect_source_dirs(tmp_path, [])
        assert "src" in found

    def test_detect_lib_and_app(self, tmp_path: Path) -> None:
        (tmp_path / "lib").mkdir()
        (tmp_path / "app").mkdir()
        found = _detect_source_dirs(tmp_path, [])
        assert "lib" in found
        assert "app" in found

    def test_empty_repo(self, tmp_path: Path) -> None:
        found = _detect_source_dirs(tmp_path, [])
        assert len(found) == 0


# ── Constitution Generation ──────────────────────────────────────


class TestConstitutionGeneration:
    def test_base_rules_preserved(self) -> None:
        """Default constitution has 6 rules — base extraction should find them all."""
        rules = _base_rules()
        assert len(rules) == 6
        ids = [r["id"] for r in rules]
        assert "RULE-SEC-001" in ids
        assert "RULE-GIT-001" in ids

    def test_source_dirs_customize_delete_rule(self, tmp_path: Path) -> None:
        report = ScanReport(source_dirs=["src", "lib"])
        rules = _constitution_rules(report)
        del_rule = [r for r in rules if r.get("id") == "RULE-DEL-001"][0]
        assert "src/**" in del_rule["patterns"]
        assert "lib/**" in del_rule["patterns"]

    def test_sensitive_files_add_sec002(self, tmp_path: Path) -> None:
        report = ScanReport(
            sensitive_files=[
                SensitiveFile(pattern="**/*.pem", matches=["certs/server.pem"]),
            ]
        )
        rules = _constitution_rules(report)
        sec002 = [r for r in rules if r.get("id") == "RULE-SEC-002"]
        assert len(sec002) == 1
        assert "**/*.pem" in sec002[0]["patterns"]

    def test_generated_constitution_compiles(self, tmp_path: Path) -> None:
        """Generated constitution should be parseable by governance compiler."""
        report = ScanReport(
            source_dirs=["src"],
            sensitive_files=[
                SensitiveFile(pattern="**/*.key", matches=["keys/test.key"]),
            ],
        )
        constitution_text = generate_constitution(report)
        # Write and compile
        exo_dir = tmp_path / ".exo"
        exo_dir.mkdir()
        (exo_dir / "CONSTITUTION.md").write_text(constitution_text, encoding="utf-8")
        result = governance_mod.compile_constitution(tmp_path)
        assert "source_hash" in result
        # Should have 8 rules (6 base + RULE-DEL-001 dynamic + RULE-SEC-002)
        lock_path = exo_dir / "governance.lock.json"
        lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
        assert len(lock_data["rules"]) == 8

    def test_sentinel_no_source_dirs(self, tmp_path: Path) -> None:
        """Without source dirs, RULE-DEL-001 should NOT be present."""
        report = ScanReport()
        rules = _constitution_rules(report)
        del_rules = [r for r in rules if r.get("id") == "RULE-DEL-001"]
        assert len(del_rules) == 0


# ── Config Generation ────────────────────────────────────────────


class TestConfigGeneration:
    def test_python_checks(self) -> None:
        report = ScanReport(
            languages=[LanguageDetection(language="python", markers=["pyproject.toml"], confidence=0.5)]
        )
        config = generate_config(report)
        for check in LANGUAGE_CHECKS["python"]:
            assert check in config["checks_allowlist"]

    def test_node_checks(self) -> None:
        report = ScanReport(languages=[LanguageDetection(language="node", markers=["package.json"], confidence=0.5)])
        config = generate_config(report)
        for check in LANGUAGE_CHECKS["node"]:
            assert check in config["checks_allowlist"]

    def test_budgets_match_language(self) -> None:
        report = ScanReport(languages=[LanguageDetection(language="node", markers=["package.json"], confidence=0.5)])
        config = generate_config(report)
        expected = LANGUAGE_BUDGETS["node"]
        assert config["defaults"]["ticket_budgets"]["max_files_changed"] == expected["max_files_changed"]
        assert config["defaults"]["ticket_budgets"]["max_loc_changed"] == expected["max_loc_changed"]

    def test_ignore_paths_include_build_dirs(self) -> None:
        report = ScanReport(
            build_dirs=[
                BuildDir(path="node_modules", language="node"),
                BuildDir(path="dist", language="common"),
            ]
        )
        config = generate_config(report)
        ignore_paths = config["git_controls"]["ignore_paths"]
        assert "node_modules/**" in ignore_paths
        assert "dist/**" in ignore_paths

    def test_config_structure_matches_default(self) -> None:
        """Generated config should have all keys from DEFAULT_CONFIG."""
        report = ScanReport()
        config = generate_config(report)
        for key in DEFAULT_CONFIG:
            assert key in config


# ── Smart Init Integration ────────────────────────────────────────


def _policy_block(rule: dict[str, Any]) -> str:
    return f"\n```yaml exo-policy\n{json.dumps(rule)}\n```\n"


class TestSmartInit:
    def test_init_scan_python(self, tmp_path: Path) -> None:
        """Init with scan on a Python project should detect Python."""
        (tmp_path / "pyproject.toml").write_text("[build-system]", encoding="utf-8")
        (tmp_path / "src").mkdir()
        from exo.stdlib.engine import KernelEngine

        engine = KernelEngine(repo=tmp_path, actor="test-agent")
        result = engine.init(scan=True)
        data = result.get("data", result)
        assert "scan" in data
        scan_data = data["scan"]
        assert scan_data["primary_language"] == "python"

    def test_init_scan_node(self, tmp_path: Path) -> None:
        """Init with scan on a Node project should detect Node."""
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        from exo.stdlib.engine import KernelEngine

        engine = KernelEngine(repo=tmp_path, actor="test-agent")
        result = engine.init(scan=True)
        data = result.get("data", result)
        assert "scan" in data
        scan_data = data["scan"]
        assert scan_data["primary_language"] == "node"

    def test_init_no_scan_uses_defaults(self, tmp_path: Path) -> None:
        """Init with scan=False should NOT include scan data."""
        (tmp_path / "pyproject.toml").write_text("[build-system]", encoding="utf-8")
        from exo.stdlib.engine import KernelEngine

        engine = KernelEngine(repo=tmp_path, actor="test-agent")
        result = engine.init(scan=False)
        data = result.get("data", result)
        assert data.get("scan") is None

    def test_return_includes_scan_data(self, tmp_path: Path) -> None:
        """Return dict should include scan report."""
        from exo.stdlib.engine import KernelEngine

        engine = KernelEngine(repo=tmp_path, actor="test-agent")
        result = engine.init(scan=True)
        data = result.get("data", result)
        assert "scan" in data
        assert "languages" in data["scan"]
        assert "scanned_at" in data["scan"]

    def test_return_includes_adapters_generated(self, tmp_path: Path) -> None:
        """Return dict should include adapters_generated paths."""
        from exo.stdlib.engine import KernelEngine

        engine = KernelEngine(repo=tmp_path, actor="test-agent")
        result = engine.init(scan=True)
        data = result.get("data", result)
        assert "adapters_generated" in data
        # Should have generated at least CLAUDE.md
        assert len(data["adapters_generated"]) > 0


# ── Scan Idempotency ────────────────────────────────────────────


class TestScanIdempotent:
    def test_reinit_preserves_existing_constitution(self, tmp_path: Path) -> None:
        """Re-running init should NOT overwrite existing constitution."""
        from exo.stdlib.engine import KernelEngine

        engine = KernelEngine(repo=tmp_path, actor="test-agent")
        engine.init(scan=True)
        # Read constitution
        constitution_path = tmp_path / ".exo" / "CONSTITUTION.md"
        original = constitution_path.read_text(encoding="utf-8")
        # Re-init
        engine2 = KernelEngine(repo=tmp_path, actor="test-agent")
        engine2.init(scan=True)
        # Should be unchanged
        assert constitution_path.read_text(encoding="utf-8") == original

    def test_scan_is_read_only(self, tmp_path: Path) -> None:
        """scan_repo() should not create any files."""
        (tmp_path / "pyproject.toml").write_text("[build-system]", encoding="utf-8")
        files_before = set(tmp_path.rglob("*"))
        scan_repo(tmp_path)
        files_after = set(tmp_path.rglob("*"))
        assert files_before == files_after


# ── CLI Output ──────────────────────────────────────────────────


class TestScanCLI:
    def test_cli_human_output(self, tmp_path: Path) -> None:
        """CLI scan should produce human-readable output."""
        from exo.cli import main

        (tmp_path / "pyproject.toml").write_text("[build-system]", encoding="utf-8")
        # Need .exo scaffold for scan command
        from exo.stdlib.engine import KernelEngine

        engine = KernelEngine(repo=tmp_path, actor="test-agent")
        engine.init(scan=False)
        # Run scan CLI
        exit_code = main(["--repo", str(tmp_path), "--format", "human", "scan"])
        assert exit_code == 0

    def test_cli_json_output(self, tmp_path: Path) -> None:
        """CLI scan --format json should produce valid JSON."""
        from exo.cli import main

        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        from exo.stdlib.engine import KernelEngine

        engine = KernelEngine(repo=tmp_path, actor="test-agent")
        engine.init(scan=False)
        exit_code = main(["--repo", str(tmp_path), "--format", "json", "scan"])
        assert exit_code == 0

    def test_cli_empty_repo(self, tmp_path: Path) -> None:
        """CLI scan on empty repo should still succeed."""
        from exo.cli import main
        from exo.stdlib.engine import KernelEngine

        engine = KernelEngine(repo=tmp_path, actor="test-agent")
        engine.init(scan=False)
        exit_code = main(["--repo", str(tmp_path), "--format", "human", "scan"])
        assert exit_code == 0


# ── Format Human ─────────────────────────────────────────────────


class TestFormatScanHuman:
    def test_with_findings(self) -> None:
        report = ScanReport(
            languages=[
                LanguageDetection(language="python", markers=["pyproject.toml"], confidence=0.5),
            ],
            sensitive_files=[
                SensitiveFile(pattern="**/*.pem", matches=["certs/server.pem"]),
            ],
            build_dirs=[BuildDir(path="dist", language="common")],
            existing_governance=[ExistingGovernance(kind="claude_md", path="CLAUDE.md")],
            ci_systems=[CIDetection(system="github_actions", path=".github/workflows")],
            source_dirs=["src"],
        )
        text = format_scan_human(report)
        assert "python" in text
        assert "*.pem" in text
        assert "dist" in text
        assert "claude_md" in text
        assert "github_actions" in text
        assert "src" in text

    def test_empty_report(self) -> None:
        report = ScanReport()
        text = format_scan_human(report)
        assert "(none)" in text
        assert "Scan Report" in text


# ── ScanReport Properties ───────────────────────────────────────


class TestScanReportProperties:
    def test_primary_language_picks_highest_confidence(self) -> None:
        report = ScanReport(
            languages=[
                LanguageDetection(language="python", markers=["pyproject.toml"], confidence=0.3),
                LanguageDetection(language="node", markers=["package.json", "yarn.lock"], confidence=0.5),
            ]
        )
        assert report.primary_language == "node"

    def test_primary_language_none_when_empty(self) -> None:
        report = ScanReport()
        assert report.primary_language is None

    def test_has_existing_exo_true(self) -> None:
        report = ScanReport(existing_governance=[ExistingGovernance(kind="exo_dir", path=".exo")])
        assert report.has_existing_exo is True

    def test_has_existing_exo_false(self) -> None:
        report = ScanReport(existing_governance=[ExistingGovernance(kind="claude_md", path="CLAUDE.md")])
        assert report.has_existing_exo is False


# ── scan_to_dict Serialization ───────────────────────────────────


class TestScanToDict:
    def test_all_fields_present(self) -> None:
        report = ScanReport(
            languages=[LanguageDetection(language="python", markers=["pyproject.toml"], confidence=0.5)],
            scanned_at="2025-01-01T00:00:00+00:00",
        )
        d = scan_to_dict(report)
        assert "languages" in d
        assert "sensitive_files" in d
        assert "build_dirs" in d
        assert "existing_governance" in d
        assert "ci_systems" in d
        assert "source_dirs" in d
        assert "primary_language" in d
        assert "has_existing_exo" in d
        assert "scanned_at" in d

    def test_json_serializable(self) -> None:
        report = ScanReport(
            languages=[LanguageDetection(language="go", markers=["go.mod"], confidence=0.5)],
        )
        d = scan_to_dict(report)
        # Should not raise
        json.dumps(d, ensure_ascii=True)
