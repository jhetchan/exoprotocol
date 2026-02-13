"""Composite governance drift detection.

Runs all available deterministic checks against the repository and produces
a unified drift report covering governance integrity, adapter freshness,
feature traceability, requirement traceability, and session health.

This is designed to run as a single health-check command (`exo drift`)
that aggregates results from all governance subsystems.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from exo.kernel.errors import ExoError
from exo.kernel.governance import (
    CONSTITUTION_PATH,
    LOCK_PATH,
    load_governance_lock,
    verify_governance,
    load_governance,
)
from exo.kernel.utils import now_iso, sha256_text
from exo.stdlib.adapters import TARGET_FILES, GOVERNANCE_LOCK_PATH

REQUIREMENTS_PATH = Path(".exo/requirements.yaml")
FEATURES_PATH = Path(".exo/features.yaml")


@dataclass
class DriftSection:
    """Result of a single drift check subsystem."""
    name: str
    status: str  # pass | fail | skip | error
    summary: str
    errors: int = 0
    warnings: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class DriftReport:
    """Composite result of all drift checks."""
    sections: list[DriftSection]
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


def _check_governance(repo: Path) -> DriftSection:
    """Check governance lock matches constitution (hash integrity)."""
    constitution_path = repo / CONSTITUTION_PATH
    lock_path = repo / LOCK_PATH

    if not constitution_path.exists():
        return DriftSection(
            name="governance",
            status="skip",
            summary="no CONSTITUTION.md found",
        )

    if not lock_path.exists():
        return DriftSection(
            name="governance",
            status="fail",
            summary="governance.lock.json missing — run `exo build-governance`",
            errors=1,
        )

    try:
        gov = load_governance(repo)
        report = verify_governance(gov)
        if report.valid:
            return DriftSection(
                name="governance",
                status="pass",
                summary="constitution hash matches lock",
                details={
                    "source_hash": gov.source_hash[:16] + "..." if gov.source_hash else "",
                },
            )
        else:
            return DriftSection(
                name="governance",
                status="fail",
                summary="governance drift detected",
                errors=len(report.reasons),
                details={
                    "reasons": report.reasons,
                    "expected_hash": report.expected_hash,
                    "actual_hash": report.actual_hash,
                },
            )
    except ExoError as e:
        return DriftSection(
            name="governance",
            status="error",
            summary=str(e.message),
            errors=1,
        )
    except Exception as e:
        return DriftSection(
            name="governance",
            status="error",
            summary=f"unexpected error: {e}",
            errors=1,
        )


# Regex to extract governance hash from adapter file header comments
_GOVERNANCE_HASH_RE = re.compile(r"Governance hash:\s*([a-f0-9]+)")


def _check_adapters(repo: Path) -> DriftSection:
    """Check if adapter files are stale (governance hash mismatch)."""
    lock_path = repo / GOVERNANCE_LOCK_PATH
    if not lock_path.exists():
        return DriftSection(
            name="adapters",
            status="skip",
            summary="no governance lock — adapters cannot be checked",
        )

    try:
        from exo.kernel.utils import load_json
        lock = load_json(lock_path)
    except Exception:
        return DriftSection(
            name="adapters",
            status="error",
            summary="could not load governance lock",
            errors=1,
        )

    current_hash = str(lock.get("source_hash", ""))
    if not current_hash:
        return DriftSection(
            name="adapters",
            status="error",
            summary="governance lock has no source_hash",
            errors=1,
        )

    stale: list[str] = []
    missing: list[str] = []
    fresh: list[str] = []

    for target, filename in TARGET_FILES.items():
        adapter_path = repo / filename
        if not adapter_path.exists():
            missing.append(filename)
            continue

        try:
            content = adapter_path.read_text(encoding="utf-8")
        except OSError:
            missing.append(filename)
            continue

        match = _GOVERNANCE_HASH_RE.search(content)
        if match:
            embedded_hash = match.group(1)
            if current_hash.startswith(embedded_hash):
                fresh.append(filename)
            else:
                stale.append(filename)
        else:
            # No hash marker — can't determine freshness, treat as stale
            stale.append(filename)

    if not fresh and not stale:
        return DriftSection(
            name="adapters",
            status="skip",
            summary="no adapter files found — run `exo adapter-generate`",
            details={"missing": missing},
        )

    if stale:
        return DriftSection(
            name="adapters",
            status="fail",
            summary=f"{len(stale)} adapter(s) stale — regenerate with `exo adapter-generate`",
            errors=len(stale),
            details={"stale": stale, "fresh": fresh, "missing": missing},
        )

    return DriftSection(
        name="adapters",
        status="pass",
        summary=f"{len(fresh)} adapter(s) up to date",
        details={"fresh": fresh, "missing": missing},
    )


def _check_features(repo: Path) -> DriftSection:
    """Run feature traceability check if features.yaml exists."""
    if not (repo / FEATURES_PATH).exists():
        return DriftSection(
            name="features",
            status="skip",
            summary="no features.yaml found",
        )

    try:
        from exo.stdlib.features import trace, trace_to_dict
        report = trace(repo)
        errors = sum(1 for v in report.violations if v.severity == "error")
        warnings = sum(1 for v in report.violations if v.severity == "warning")

        if report.passed:
            return DriftSection(
                name="features",
                status="pass",
                summary=f"{report.features_total} features, {report.tags_total} tags, {len(report.bound_features)} bound",
                warnings=warnings,
                details=trace_to_dict(report),
            )
        else:
            return DriftSection(
                name="features",
                status="fail",
                summary=f"{errors} error(s), {warnings} warning(s) in feature traceability",
                errors=errors,
                warnings=warnings,
                details=trace_to_dict(report),
            )
    except ExoError as e:
        return DriftSection(
            name="features",
            status="error",
            summary=str(e.message),
            errors=1,
        )
    except Exception as e:
        return DriftSection(
            name="features",
            status="error",
            summary=f"unexpected error: {e}",
            errors=1,
        )


def _check_requirements(repo: Path) -> DriftSection:
    """Run requirement traceability check if requirements.yaml exists."""
    if not (repo / REQUIREMENTS_PATH).exists():
        return DriftSection(
            name="requirements",
            status="skip",
            summary="no requirements.yaml found",
        )

    try:
        from exo.stdlib.requirements import trace_requirements, req_trace_to_dict
        report = trace_requirements(repo)
        errors = sum(1 for v in report.violations if v.severity == "error")
        warnings = sum(1 for v in report.violations if v.severity == "warning")

        if report.passed:
            return DriftSection(
                name="requirements",
                status="pass",
                summary=f"{report.reqs_total} requirements, {report.refs_total} refs, {len(report.covered_reqs)} covered",
                warnings=warnings,
                details=req_trace_to_dict(report),
            )
        else:
            return DriftSection(
                name="requirements",
                status="fail",
                summary=f"{errors} error(s), {warnings} warning(s) in requirement traceability",
                errors=errors,
                warnings=warnings,
                details=req_trace_to_dict(report),
            )
    except ExoError as e:
        return DriftSection(
            name="requirements",
            status="error",
            summary=str(e.message),
            errors=1,
        )
    except Exception as e:
        return DriftSection(
            name="requirements",
            status="error",
            summary=f"unexpected error: {e}",
            errors=1,
        )


def _check_coherence(repo: Path) -> DriftSection:
    """Run coherence checks if config is present and enabled."""
    config_path = repo / Path(".exo/config.yaml")
    if not config_path.exists():
        return DriftSection(
            name="coherence",
            status="skip",
            summary="no config.yaml found",
        )

    try:
        from exo.stdlib.coherence import check_coherence, coherence_to_dict
        report = check_coherence(repo)
        warnings = report.warning_count

        if report.passed and not report.violations:
            return DriftSection(
                name="coherence",
                status="pass",
                summary=f"{report.files_checked} files, {report.functions_checked} functions — no issues",
                details=coherence_to_dict(report),
            )
        elif report.passed:
            return DriftSection(
                name="coherence",
                status="pass",
                summary=f"{warnings} warning(s) in coherence check",
                warnings=warnings,
                details=coherence_to_dict(report),
            )
        else:
            errors = sum(1 for v in report.violations if v.severity == "error")
            return DriftSection(
                name="coherence",
                status="fail",
                summary=f"{errors} error(s), {warnings} warning(s) in coherence check",
                errors=errors,
                warnings=warnings,
                details=coherence_to_dict(report),
            )
    except ExoError as e:
        return DriftSection(
            name="coherence",
            status="error",
            summary=str(e.message),
            errors=1,
        )
    except Exception as e:
        return DriftSection(
            name="coherence",
            status="error",
            summary=f"unexpected error: {e}",
            errors=1,
        )


def _check_sessions(repo: Path, stale_hours: float = 48.0) -> DriftSection:
    """Check for stale or orphaned sessions."""
    try:
        from exo.orchestrator import scan_sessions
        data = scan_sessions(repo, stale_hours=stale_hours)
        stale = data.get("stale_sessions", [])
        active = data.get("active_sessions", [])
        suspended = data.get("suspended_sessions", [])

        total = len(active) + len(suspended)
        if total == 0:
            return DriftSection(
                name="sessions",
                status="pass",
                summary="no active or suspended sessions",
            )

        if stale:
            return DriftSection(
                name="sessions",
                status="fail",
                summary=f"{len(stale)} stale session(s) (>{stale_hours}h) — run `exo session-cleanup`",
                warnings=len(stale),
                details={
                    "active": len(active),
                    "suspended": len(suspended),
                    "stale": len(stale),
                    "stale_sessions": [
                        {"session_id": s.get("session_id", ""), "age_hours": s.get("age_hours", 0)}
                        for s in stale
                    ],
                },
            )

        return DriftSection(
            name="sessions",
            status="pass",
            summary=f"{len(active)} active, {len(suspended)} suspended — none stale",
            details={"active": len(active), "suspended": len(suspended)},
        )
    except Exception as e:
        return DriftSection(
            name="sessions",
            status="error",
            summary=f"session scan failed: {e}",
            errors=1,
        )


def drift(
    repo: Path,
    *,
    stale_hours: float = 48.0,
    skip_adapters: bool = False,
    skip_features: bool = False,
    skip_requirements: bool = False,
    skip_sessions: bool = False,
    skip_coherence: bool = False,
) -> DriftReport:
    """Run all governance drift checks and produce a composite report.

    Checks:
    1. Governance integrity (constitution hash vs lock hash)
    2. Adapter freshness (governance hash embedded in adapter files)
    3. Feature traceability (if .exo/features.yaml exists)
    4. Requirement traceability (if .exo/requirements.yaml exists)
    5. Coherence (co-update rules + docstring freshness)
    6. Session health (stale/orphaned sessions)

    Args:
        repo: Repository root path.
        stale_hours: Threshold for flagging stale sessions.
        skip_adapters: Skip adapter freshness check.
        skip_features: Skip feature traceability check.
        skip_requirements: Skip requirement traceability check.
        skip_sessions: Skip session health check.
        skip_coherence: Skip coherence check.

    Returns:
        DriftReport with per-subsystem results and overall verdict.
    """
    repo = Path(repo).resolve()
    sections: list[DriftSection] = []

    # 1. Governance integrity (always runs)
    sections.append(_check_governance(repo))

    # 2. Adapter freshness
    if not skip_adapters:
        sections.append(_check_adapters(repo))

    # 3. Feature traceability
    if not skip_features:
        sections.append(_check_features(repo))

    # 4. Requirement traceability
    if not skip_requirements:
        sections.append(_check_requirements(repo))

    # 5. Coherence
    if not skip_coherence:
        sections.append(_check_coherence(repo))

    # 6. Session health
    if not skip_sessions:
        sections.append(_check_sessions(repo, stale_hours=stale_hours))

    # Overall verdict: fail if ANY section has status "fail" or "error"
    has_failure = any(s.status in ("fail", "error") for s in sections)
    overall = "fail" if has_failure else "pass"

    return DriftReport(
        sections=sections,
        overall=overall,
        checked_at=now_iso(),
    )


def drift_to_dict(report: DriftReport) -> dict[str, Any]:
    """Convert DriftReport to a plain dict for serialization."""
    return {
        "overall": report.overall,
        "passed": report.passed,
        "total_errors": report.total_errors,
        "total_warnings": report.total_warnings,
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
        "section_count": len(report.sections),
        "checked_at": report.checked_at,
    }


def format_drift_human(report: DriftReport) -> str:
    """Format drift report as human-readable text."""
    icon = "PASS" if report.passed else "FAIL"
    lines = [
        f"Governance Drift: {icon}",
        f"  checks: {len(report.sections)}, errors: {report.total_errors}, warnings: {report.total_warnings}",
    ]

    for section in report.sections:
        status_icon = {
            "pass": "OK",
            "fail": "FAIL",
            "skip": "SKIP",
            "error": "ERR",
        }.get(section.status, "?")
        lines.append(f"  [{status_icon}] {section.name}: {section.summary}")

    return "\n".join(lines)
