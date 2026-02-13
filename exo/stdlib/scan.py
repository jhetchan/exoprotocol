"""Smart Init — Brownfield Repo Scanner.

Deterministic (no LLM) repo scanner that detects project language,
sensitive files, build directories, existing governance artifacts,
CI systems, and source directories.  Generates a project-aware
constitution and config so ``exo init`` adds value from day one.

Storage: scan results are ephemeral (returned, never persisted).
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from exo.kernel.utils import now_iso
from exo.stdlib.defaults import DEFAULT_CONFIG, DEFAULT_CONSTITUTION


# ── Dataclasses ────────────────────────────────────────────────────

@dataclass
class LanguageDetection:
    language: str
    markers: list[str]  # which marker files were found
    confidence: float   # 0.0 – 1.0


@dataclass
class SensitiveFile:
    pattern: str
    matches: list[str]  # relative paths found


@dataclass
class BuildDir:
    path: str
    language: str  # which language it belongs to, or "common"


@dataclass
class ExistingGovernance:
    kind: str   # claude_md | cursorrules | agents_md | exo_dir
    path: str
    exo_managed: bool = False   # True if exo governance markers found
    user_lines: int = 0         # non-blank lines outside markers
    total_lines: int = 0        # total lines in file


@dataclass
class CIDetection:
    system: str  # github_actions | gitlab_ci | jenkins
    path: str


@dataclass
class ScanReport:
    languages: list[LanguageDetection] = field(default_factory=list)
    sensitive_files: list[SensitiveFile] = field(default_factory=list)
    build_dirs: list[BuildDir] = field(default_factory=list)
    existing_governance: list[ExistingGovernance] = field(default_factory=list)
    ci_systems: list[CIDetection] = field(default_factory=list)
    source_dirs: list[str] = field(default_factory=list)
    scanned_at: str = ""

    @property
    def primary_language(self) -> str | None:
        if not self.languages:
            return None
        best = max(self.languages, key=lambda l: l.confidence)
        return best.language

    @property
    def has_existing_exo(self) -> bool:
        return any(g.kind == "exo_dir" for g in self.existing_governance)


# ── Detection Registries (data-driven) ────────────────────────────

# (language, [marker_files], priority)
LANGUAGE_MARKERS: list[tuple[str, list[str], int]] = [
    ("python", ["pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "requirements.txt"], 1),
    ("node", ["package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"], 2),
    ("go", ["go.mod", "go.sum"], 3),
    ("rust", ["Cargo.toml", "Cargo.lock"], 4),
    ("java", ["pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle"], 5),
    ("ruby", ["Gemfile", "Gemfile.lock", ".ruby-version"], 6),
]

EXTRA_SENSITIVE_PATTERNS: list[str] = [
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/credentials*",
    "**/.npmrc",
    "**/.pypirc",
    "**/serviceAccountKey*.json",
    "**/*.keystore",
]

_SKIP_DIRS = frozenset({
    ".git", ".exo", "node_modules", "__pycache__", ".venv",
    "venv", ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".next", ".nuxt", "target",
})

BUILD_DIRS: dict[str, list[str]] = {
    "common": ["dist", "build", ".cache"],
    "python": ["__pycache__", ".eggs", "*.egg-info", ".tox", ".mypy_cache", ".pytest_cache"],
    "node": ["node_modules", ".next", ".nuxt", ".output"],
    "go": [],
    "rust": ["target"],
    "java": ["target", "out", ".gradle"],
    "ruby": [".bundle", "vendor/bundle"],
}

LANGUAGE_CHECKS: dict[str, list[str]] = {
    "python": ["python -m pytest", "python3 -m pytest", "python3 -m compileall ."],
    "node": ["npm test", "npm run lint"],
    "go": ["go test ./...", "go vet ./..."],
    "rust": ["cargo test", "cargo clippy"],
    "java": ["./gradlew test", "mvn test"],
    "ruby": ["bundle exec rake test", "bundle exec rspec"],
}

LANGUAGE_BUDGETS: dict[str, dict[str, int]] = {
    "python": {"max_files_changed": 15, "max_loc_changed": 500},
    "node": {"max_files_changed": 20, "max_loc_changed": 600},
    "go": {"max_files_changed": 12, "max_loc_changed": 400},
    "rust": {"max_files_changed": 12, "max_loc_changed": 400},
    "java": {"max_files_changed": 15, "max_loc_changed": 500},
    "ruby": {"max_files_changed": 15, "max_loc_changed": 500},
}

_SOURCE_DIR_CANDIDATES = [
    "src", "lib", "app", "cmd", "pkg", "internal",
    "apps", "packages", "modules",
]


# ── Detection Helpers ──────────────────────────────────────────────


def _detect_languages(repo: Path) -> list[LanguageDetection]:
    """Iterate LANGUAGE_MARKERS, check file existence, return detections."""
    detections: list[LanguageDetection] = []
    for language, markers, _priority in LANGUAGE_MARKERS:
        found = [m for m in markers if (repo / m).exists()]
        if found:
            confidence = min(1.0, len(found) / max(len(markers), 1))
            detections.append(LanguageDetection(
                language=language,
                markers=found,
                confidence=confidence,
            ))
    return detections


def _detect_sensitive_files(repo: Path) -> list[SensitiveFile]:
    """Glob EXTRA_SENSITIVE_PATTERNS, skip .git/node_modules etc."""
    results: list[SensitiveFile] = []
    for pattern in EXTRA_SENSITIVE_PATTERNS:
        matches: list[str] = []
        try:
            for path in repo.glob(pattern):
                parts = path.relative_to(repo).parts
                if any(part in _SKIP_DIRS for part in parts):
                    continue
                matches.append(str(path.relative_to(repo)))
        except Exception:  # noqa: BLE001
            continue
        if matches:
            results.append(SensitiveFile(pattern=pattern, matches=matches))
    return results


def _detect_build_dirs(repo: Path, languages: list[LanguageDetection]) -> list[BuildDir]:
    """Check BUILD_DIRS[common] + language-specific directories."""
    found: list[BuildDir] = []
    seen: set[str] = set()

    def _check(dirs: list[str], lang: str) -> None:
        for d in dirs:
            if d in seen:
                continue
            if (repo / d).is_dir():
                seen.add(d)
                found.append(BuildDir(path=d, language=lang))

    _check(BUILD_DIRS.get("common", []), "common")
    for detection in languages:
        lang_dirs = BUILD_DIRS.get(detection.language, [])
        # Filter out glob patterns (like *.egg-info) — only check real dirs
        real_dirs = [d for d in lang_dirs if "*" not in d]
        _check(real_dirs, detection.language)

    return found


def _detect_existing_governance(repo: Path) -> list[ExistingGovernance]:
    """Check for CLAUDE.md, .cursorrules, AGENTS.md, .exo/ directory.

    For file-based governance (not exo_dir), reads content to determine
    whether exo markers are present and counts user vs total lines.
    """
    from exo.stdlib.adapters import EXO_MARKER_BEGIN, _count_user_lines

    found: list[ExistingGovernance] = []
    checks = [
        ("claude_md", "CLAUDE.md"),
        ("cursorrules", ".cursorrules"),
        ("agents_md", "AGENTS.md"),
        ("exo_dir", ".exo"),
    ]
    for kind, path_str in checks:
        p = repo / path_str
        if p.exists():
            if kind == "exo_dir":
                found.append(ExistingGovernance(kind=kind, path=path_str))
            else:
                try:
                    content = p.read_text(encoding="utf-8")
                    has_markers = EXO_MARKER_BEGIN in content
                    total = len(content.splitlines())
                    user = _count_user_lines(content) if has_markers else total
                    found.append(ExistingGovernance(
                        kind=kind,
                        path=path_str,
                        exo_managed=has_markers,
                        user_lines=user,
                        total_lines=total,
                    ))
                except OSError:
                    found.append(ExistingGovernance(kind=kind, path=path_str))
    return found


def _detect_ci(repo: Path) -> list[CIDetection]:
    """Check for CI system config files."""
    found: list[CIDetection] = []
    if (repo / ".github" / "workflows").is_dir():
        found.append(CIDetection(system="github_actions", path=".github/workflows"))
    if (repo / ".gitlab-ci.yml").exists():
        found.append(CIDetection(system="gitlab_ci", path=".gitlab-ci.yml"))
    if (repo / "Jenkinsfile").exists():
        found.append(CIDetection(system="jenkins", path="Jenkinsfile"))
    return found


def _detect_source_dirs(repo: Path, _languages: list[LanguageDetection]) -> list[str]:
    """Check common source directory candidates."""
    found: list[str] = []
    for candidate in _SOURCE_DIR_CANDIDATES:
        if (repo / candidate).is_dir():
            found.append(candidate)
    return found


# ── Main Scanner ───────────────────────────────────────────────────


def scan_repo(repo: Path) -> ScanReport:
    """Scan repository and return a ScanReport.  Pure file-presence detection — no LLM."""
    repo = Path(repo).resolve()
    languages = _detect_languages(repo)
    return ScanReport(
        languages=languages,
        sensitive_files=_detect_sensitive_files(repo),
        build_dirs=_detect_build_dirs(repo, languages),
        existing_governance=_detect_existing_governance(repo),
        ci_systems=_detect_ci(repo),
        source_dirs=_detect_source_dirs(repo, languages),
        scanned_at=now_iso(),
    )


# ── Constitution Generation ───────────────────────────────────────


def _base_rules() -> list[dict[str, Any]]:
    """Extract the 8 standard rules from DEFAULT_CONSTITUTION as dicts."""
    import re
    blocks: list[dict[str, Any]] = []
    # Match ```yaml exo-policy ... ``` blocks
    pattern = re.compile(
        r"```yaml\s+exo-policy\s*\n(.*?)```",
        re.DOTALL,
    )
    for match in pattern.finditer(DEFAULT_CONSTITUTION):
        try:
            data = json.loads(match.group(1).strip())
            blocks.append(data)
        except (json.JSONDecodeError, ValueError):
            continue
    return blocks


def _constitution_rules(report: ScanReport) -> list[dict[str, Any]]:
    """Customize rules based on scan findings."""
    rules = _base_rules()

    # Customize RULE-DEL-001 patterns for detected source dirs
    if report.source_dirs:
        for rule in rules:
            if rule.get("id") == "RULE-DEL-001":
                rule["patterns"] = [f"{d}/**" for d in report.source_dirs]
                break

    # Add RULE-SEC-002 for extra sensitive files if any found
    sensitive_patterns = [sf.pattern for sf in report.sensitive_files]
    if sensitive_patterns:
        rules.append({
            "id": "RULE-SEC-002",
            "type": "filesystem_deny",
            "patterns": sensitive_patterns,
            "actions": ["read", "write"],
            "message": "Blocked by RULE-SEC-002 (sensitive files detected by scan).",
        })

    return rules


def _rules_to_constitution(rules: list[dict[str, Any]]) -> str:
    """Render rule dicts as markdown with exo-policy blocks."""
    # Map rule IDs to article titles
    article_titles: dict[str, str] = {
        "RULE-SEC-001": "Secrets",
        "RULE-SEC-002": "Sensitive files (scan-detected)",
        "RULE-GIT-001": "Git internals",
        "RULE-KRN-001": "Kernel is out-of-band",
        "RULE-DEL-001": "Protected deletions",
        "RULE-LOCK-001": "Ticket lock required",
        "RULE-CHECK-001": "Checks before done",
        "RULE-EVO-001": "Practice is mutable, governance is sacred",
        "RULE-EVO-002": "Patch-first evolution",
    }

    # Map rule IDs to article descriptions
    article_descriptions: dict[str, str] = {
        "RULE-SEC-001": "Agents must never read or write host credential stores or dotenv secrets.",
        "RULE-SEC-002": "Agents must not access sensitive files detected by repo scan.",
        "RULE-GIT-001": "Agents must not mutate `.git` internals.",
        "RULE-KRN-001": "Governed flows must never mutate kernel sources.",
        "RULE-DEL-001": "Source deletes are denied by default.",
        "RULE-LOCK-001": "Any governed write requires an active ticket lock.",
        "RULE-CHECK-001": "A ticket must pass checks before status can move to done.",
        "RULE-EVO-001": "Practice changes may use lightweight approval; governance changes require human approval.",
        "RULE-EVO-002": "No self-evolution applies without proposal + patch + approval + audit trail.",
    }

    lines = ["# ExoProtocol Constitution (Kernel v0.1)", ""]
    lines.append("This constitution is literate: human guidance plus machine-parsed `exo-policy` blocks.")
    lines.append("")

    for rule in rules:
        rule_id = rule.get("id", "RULE-???")
        title = article_titles.get(rule_id, rule_id)
        desc = article_descriptions.get(rule_id, f"[{rule_id}]")

        lines.append(f"## Article: {title}")
        lines.append(f"[{rule_id}] {desc}")
        lines.append("")
        lines.append("```yaml exo-policy")
        lines.append(json.dumps(rule, indent=2, ensure_ascii=True))
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


def generate_constitution(report: ScanReport) -> str:
    """Generate a project-aware constitution from scan results."""
    rules = _constitution_rules(report)
    return _rules_to_constitution(rules)


# ── Config Generation ──────────────────────────────────────────────


def generate_config(report: ScanReport) -> dict[str, Any]:
    """Generate a project-aware config dict from scan results."""
    config = copy.deepcopy(DEFAULT_CONFIG)

    # Override checks_allowlist with language-specific checks
    detected_langs = [lang.language for lang in report.languages]
    if detected_langs:
        checks: list[str] = []
        for lang in detected_langs:
            checks.extend(LANGUAGE_CHECKS.get(lang, []))
        if checks:
            config["checks_allowlist"] = checks
            # Also set do_allowlist to language-appropriate commands
            do_commands: list[str] = []
            if "node" in detected_langs:
                do_commands.append("npm run build")
            if "python" in detected_langs:
                do_commands.append("python3 -m compileall .")
            if "rust" in detected_langs:
                do_commands.append("cargo build")
            if "go" in detected_langs:
                do_commands.append("go build ./...")
            if do_commands:
                config["do_allowlist"] = do_commands

    # Override ticket_budgets with language-specific budgets
    primary = report.primary_language
    if primary and primary in LANGUAGE_BUDGETS:
        config["defaults"]["ticket_budgets"] = copy.deepcopy(LANGUAGE_BUDGETS[primary])

    # Extend git_controls.ignore_paths with detected build dirs
    build_dir_paths = [bd.path for bd in report.build_dirs]
    if build_dir_paths:
        existing_ignore = config.get("git_controls", {}).get("ignore_paths", [])
        # Add build dir globs that aren't already covered
        for bd_path in build_dir_paths:
            glob_pattern = f"{bd_path}/**"
            if glob_pattern not in existing_ignore:
                existing_ignore.append(glob_pattern)
        config["git_controls"]["ignore_paths"] = existing_ignore

    return config


# ── Serialization ──────────────────────────────────────────────────


def scan_to_dict(report: ScanReport) -> dict[str, Any]:
    """Convert ScanReport to a plain dict for JSON serialization."""
    return {
        "languages": [
            {"language": l.language, "markers": l.markers, "confidence": l.confidence}
            for l in report.languages
        ],
        "sensitive_files": [
            {"pattern": sf.pattern, "matches": sf.matches}
            for sf in report.sensitive_files
        ],
        "build_dirs": [
            {"path": bd.path, "language": bd.language}
            for bd in report.build_dirs
        ],
        "existing_governance": [
            {
                "kind": eg.kind,
                "path": eg.path,
                "exo_managed": eg.exo_managed,
                "user_lines": eg.user_lines,
                "total_lines": eg.total_lines,
            }
            for eg in report.existing_governance
        ],
        "ci_systems": [
            {"system": ci.system, "path": ci.path}
            for ci in report.ci_systems
        ],
        "source_dirs": report.source_dirs,
        "primary_language": report.primary_language,
        "has_existing_exo": report.has_existing_exo,
        "scanned_at": report.scanned_at,
    }


def format_scan_human(report: ScanReport) -> str:
    """Format ScanReport as human-readable text."""
    lines: list[str] = []
    lines.append("Scan Report")
    lines.append("=" * 40)

    # Languages
    if report.languages:
        lines.append(f"\nLanguages detected: {len(report.languages)}")
        for lang in report.languages:
            markers_str = ", ".join(lang.markers)
            lines.append(f"  {lang.language} (confidence: {lang.confidence:.0%}) — {markers_str}")
        if report.primary_language:
            lines.append(f"  Primary: {report.primary_language}")
    else:
        lines.append("\nLanguages detected: (none)")

    # Sensitive files
    if report.sensitive_files:
        total = sum(len(sf.matches) for sf in report.sensitive_files)
        lines.append(f"\nSensitive files: {total} match(es)")
        for sf in report.sensitive_files:
            lines.append(f"  {sf.pattern}: {', '.join(sf.matches)}")
    else:
        lines.append("\nSensitive files: (none)")

    # Build dirs
    if report.build_dirs:
        lines.append(f"\nBuild directories: {len(report.build_dirs)}")
        for bd in report.build_dirs:
            lines.append(f"  {bd.path} ({bd.language})")
    else:
        lines.append("\nBuild directories: (none)")

    # Existing governance
    if report.existing_governance:
        lines.append(f"\nExisting governance: {len(report.existing_governance)}")
        for eg in report.existing_governance:
            if eg.kind == "exo_dir":
                lines.append(f"  {eg.kind}: {eg.path}")
            elif eg.exo_managed:
                lines.append(
                    f"  {eg.kind}: {eg.path} (exo-managed, {eg.user_lines} user lines / {eg.total_lines} total)"
                )
            else:
                lines.append(f"  {eg.kind}: {eg.path} (user-only, {eg.total_lines} lines)")
    else:
        lines.append("\nExisting governance: (none)")

    # CI
    if report.ci_systems:
        lines.append(f"\nCI systems: {len(report.ci_systems)}")
        for ci in report.ci_systems:
            lines.append(f"  {ci.system}: {ci.path}")
    else:
        lines.append("\nCI systems: (none)")

    # Source dirs
    if report.source_dirs:
        lines.append(f"\nSource directories: {', '.join(report.source_dirs)}")
    else:
        lines.append("\nSource directories: (none)")

    return "\n".join(lines)
