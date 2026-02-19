"""Unified Governance Health Check (``exo doctor``).

Runs all diagnostic subsystems in one pass and produces a single report:
- Governance drift (constitution ↔ lock, adapters, features, requirements, sessions)
- Config schema validation
- Scan freshness (does current repo still match what init generated?)

Each section is independent — a failure in one doesn't block the others.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from exo.kernel.utils import now_iso


@dataclass
class DoctorSection:
    """Result of one diagnostic check."""

    name: str
    status: str  # pass | fail | skip | error
    summary: str
    errors: int = 0
    warnings: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class DoctorReport:
    """Composite result of all doctor checks."""

    sections: list[DoctorSection] = field(default_factory=list)
    overall: str = ""  # pass | fail
    checked_at: str = ""

    @property
    def passed(self) -> bool:
        return self.overall == "pass"

    @property
    def total_errors(self) -> int:
        return sum(s.errors for s in self.sections)

    @property
    def total_warnings(self) -> int:
        return sum(s.warnings for s in self.sections)


# ── Section Checks ─────────────────────────────────────────────────


def _check_drift(repo: Path, stale_hours: float) -> DoctorSection:
    """Run composite governance drift check."""
    try:
        from exo.stdlib.drift import drift, drift_to_dict

        report = drift(repo, stale_hours=stale_hours)
        return DoctorSection(
            name="governance_drift",
            status="pass" if report.passed else "fail",
            summary=f"Drift: {report.overall} ({report.total_errors} errors, {report.total_warnings} warnings)",
            errors=report.total_errors,
            warnings=report.total_warnings,
            details=drift_to_dict(report),
        )
    except Exception as exc:  # noqa: BLE001
        return DoctorSection(
            name="governance_drift",
            status="error",
            summary=f"Drift check failed: {exc}",
            errors=1,
        )


def _check_config(repo: Path) -> DoctorSection:
    """Run config schema validation."""
    try:
        from exo.stdlib.config_schema import validate_config, validation_to_dict

        result = validate_config(repo)
        return DoctorSection(
            name="config_validation",
            status="pass" if result.passed else "fail",
            summary=f"Config: {'PASS' if result.passed else 'FAIL'} ({result.error_count} errors, {result.warning_count} warnings)",
            errors=result.error_count,
            warnings=result.warning_count,
            details=validation_to_dict(result),
        )
    except Exception as exc:  # noqa: BLE001
        return DoctorSection(
            name="config_validation",
            status="error",
            summary=f"Config validation failed: {exc}",
            errors=1,
        )


def _check_scan_freshness(repo: Path) -> DoctorSection:
    """Check if scan findings match what's actually in the repo now."""
    try:
        from exo.stdlib.scan import scan_repo

        report = scan_repo(repo)

        issues: list[str] = []
        warnings = 0

        # Check if constitution covers detected sensitive files
        if report.sensitive_files:
            lock_path = repo / ".exo" / "governance.lock.json"
            if lock_path.exists():
                import json

                lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
                rules = lock_data.get("rules", [])
                deny_patterns: set[str] = set()
                for rule in rules:
                    if rule.get("type") == "filesystem_deny":
                        deny_patterns.update(rule.get("patterns", []))

                for sf in report.sensitive_files:
                    if sf.pattern not in deny_patterns:
                        issues.append(f"Sensitive pattern {sf.pattern} not in constitution deny rules")
                        warnings += 1

        # Check if detected languages match config checks
        if report.languages:
            config_path = repo / ".exo" / "config.yaml"
            if config_path.exists():
                from exo.kernel.utils import load_yaml

                config = load_yaml(config_path)
                if isinstance(config, dict):
                    checks = config.get("checks_allowlist", [])
                    from exo.stdlib.scan import LANGUAGE_CHECKS

                    for lang in report.languages:
                        lang_checks = LANGUAGE_CHECKS.get(lang.language, [])
                        missing = [c for c in lang_checks if c not in checks]
                        if missing:
                            issues.append(f"Language {lang.language} detected but checks missing: {', '.join(missing)}")
                            warnings += 1

        if issues:
            return DoctorSection(
                name="scan_freshness",
                status="pass",  # warnings only — don't fail
                summary=f"Scan: {len(issues)} stale finding(s)",
                warnings=warnings,
                details={"issues": issues, "primary_language": report.primary_language},
            )

        return DoctorSection(
            name="scan_freshness",
            status="pass",
            summary="Scan: governance matches repo state",
            details={"primary_language": report.primary_language},
        )
    except Exception as exc:  # noqa: BLE001
        return DoctorSection(
            name="scan_freshness",
            status="error",
            summary=f"Scan freshness check failed: {exc}",
            errors=1,
        )


def _check_scaffold(repo: Path) -> DoctorSection:
    """Check that required .exo directories and files exist."""
    required_dirs = [
        ".exo",
        ".exo/tickets",
        ".exo/locks",
        ".exo/logs",
        ".exo/memory",
        ".exo/cache",
    ]
    required_files = [
        ".exo/CONSTITUTION.md",
        ".exo/governance.lock.json",
        ".exo/config.yaml",
    ]

    missing_dirs = [d for d in required_dirs if not (repo / d).is_dir()]
    missing_files = [f for f in required_files if not (repo / f).exists()]

    errors = len(missing_dirs) + len(missing_files)
    if errors:
        return DoctorSection(
            name="scaffold",
            status="fail",
            summary=f"Scaffold: {errors} missing path(s)",
            errors=errors,
            details={"missing_dirs": missing_dirs, "missing_files": missing_files},
        )

    return DoctorSection(
        name="scaffold",
        status="pass",
        summary="Scaffold: all required paths present",
    )


def _check_governance_tracked(repo: Path) -> DoctorSection:
    """Check if .exo/ governance files are tracked by git."""
    try:
        from exo.stdlib.install import _is_git_repo, is_exo_tracked

        if not _is_git_repo(repo):
            return DoctorSection(
                name="governance_tracked",
                status="skip",
                summary="Tracked: not a git repo (skipped)",
            )

        if is_exo_tracked(repo):
            return DoctorSection(
                name="governance_tracked",
                status="pass",
                summary="Tracked: .exo/ governance files are in git",
            )
        return DoctorSection(
            name="governance_tracked",
            status="fail",
            summary="Untracked: .exo/ governance files not committed to git",
            errors=1,
            details={"fix": "Run: git add .exo/ && git commit -m 'chore: track governance' OR re-run: exo install"},
        )
    except Exception as exc:  # noqa: BLE001
        return DoctorSection(
            name="governance_tracked",
            status="error",
            summary=f"Git tracking check failed: {exc}",
            errors=1,
        )


# ── Main Entry ─────────────────────────────────────────────────────


def doctor(
    repo: Path,
    *,
    stale_hours: float = 48.0,
) -> DoctorReport:
    """Run all diagnostic checks and return a unified report."""
    repo = Path(repo).resolve()

    sections: list[DoctorSection] = []

    # 1. Scaffold check (fast, prerequisite for others)
    sections.append(_check_scaffold(repo))

    # 2. Config validation
    sections.append(_check_config(repo))

    # 3. Governance drift (subsumes governance, adapters, features, requirements, sessions)
    sections.append(_check_drift(repo, stale_hours))

    # 4. Scan freshness
    sections.append(_check_scan_freshness(repo))

    # 5. Governance tracked by git
    sections.append(_check_governance_tracked(repo))

    has_failure = any(s.status in ("fail", "error") for s in sections)

    return DoctorReport(
        sections=sections,
        overall="fail" if has_failure else "pass",
        checked_at=now_iso(),
    )


# ── Serialization ──────────────────────────────────────────────────


def doctor_to_dict(report: DoctorReport) -> dict[str, Any]:
    """Convert DoctorReport to a plain dict for JSON."""
    return {
        "overall": report.overall,
        "passed": report.passed,
        "checked_at": report.checked_at,
        "total_errors": report.total_errors,
        "total_warnings": report.total_warnings,
        "section_count": len(report.sections),
        "sections": [
            {
                "name": s.name,
                "status": s.status,
                "summary": s.summary,
                "errors": s.errors,
                "warnings": s.warnings,
                "details": s.details,
            }
            for s in report.sections
        ],
    }


def format_doctor_human(report: DoctorReport) -> str:
    """Format DoctorReport as human-readable text."""
    verdict = "PASS" if report.passed else "FAIL"
    lines = [
        f"Doctor Report: {verdict}",
        f"  checks: {len(report.sections)}, errors: {report.total_errors}, warnings: {report.total_warnings}",
        "",
    ]
    for section in report.sections:
        icon = {"pass": "+", "fail": "X", "skip": "-", "error": "!"}
        marker = icon.get(section.status, "?")
        lines.append(f"  [{marker}] {section.name}: {section.summary}")
        if section.details.get("issues"):
            for issue in section.details["issues"]:
                lines.append(f"      - {issue}")
        if section.details.get("missing_dirs"):
            for d in section.details["missing_dirs"]:
                lines.append(f"      missing dir: {d}")
        if section.details.get("missing_files"):
            for f in section.details["missing_files"]:
                lines.append(f"      missing file: {f}")

    return "\n".join(lines)
