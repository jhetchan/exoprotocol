"""Requirement registry and traceability linter.

Loads `.exo/requirements.yaml`, validates requirement definitions, scans source
files for `@req:` annotations, and cross-references code bindings against the
manifest to detect orphan references, uncovered requirements, and deleted usage.

This is deterministic (regex-based, no LLM) and designed to run as a governed
check at session-finish or in CI.
"""
# @feature:requirement-traceability
# @req: REQ-TRACE-002

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from exo.kernel.errors import ExoError
from exo.kernel.utils import load_yaml, now_iso

REQUIREMENTS_PATH = Path(".exo/requirements.yaml")

VALID_STATUSES = frozenset({"active", "deprecated", "deleted"})
VALID_PRIORITIES = frozenset({"high", "medium", "low"})

# Regex: matches requirement annotations (req or implements prefix, comma-separated IDs)
REQ_TAG_PATTERN = re.compile(
    r"[#/]\s*@(?:req|implements):\s*(.+)",
    re.IGNORECASE,
)

# Default file extensions to scan (same as features module)
DEFAULT_SCAN_GLOBS = [
    "**/*.py",
    "**/*.ts",
    "**/*.tsx",
    "**/*.js",
    "**/*.jsx",
    "**/*.rs",
    "**/*.go",
    "**/*.java",
    "**/*.kt",
    "**/*.c",
    "**/*.cpp",
    "**/*.h",
    "**/*.hpp",
    "**/*.rb",
    "**/*.swift",
    "**/*.cs",
]

# Directories to always skip
SKIP_DIRS = frozenset(
    {
        ".git",
        ".exo",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        "dist",
        "build",
        "tests",
        "test",
    }
)


@dataclass(frozen=True)
class RequirementDef:
    """A requirement definition from the manifest."""

    id: str
    title: str
    status: str = "active"  # active | deprecated | deleted
    description: str = ""
    priority: str = "medium"  # high | medium | low
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReqCodeRef:
    """A @req: reference found in source code."""

    req_id: str
    file: str  # relative path
    line: int


@dataclass(frozen=True)
class ReqTraceViolation:
    """A requirement traceability violation found by the linter."""

    kind: str  # orphan_ref | deleted_ref | deprecated_ref | uncovered_req
    req_id: str
    file: str  # relative path or "(manifest)"
    line: int | None
    message: str
    severity: str = "error"  # error | warning


@dataclass
class ReqTraceReport:
    """Result of running the requirement traceability linter."""

    reqs_total: int
    reqs_active: int
    reqs_deprecated: int
    reqs_deleted: int
    refs_total: int
    violations: list[ReqTraceViolation]
    covered_reqs: list[str]  # requirement IDs with at least one code ref
    uncovered_reqs: list[str]  # active requirement IDs with no code refs
    deprecated_with_refs: list[str]  # deprecated reqs still referenced
    checked_at: str = ""

    @property
    def passed(self) -> bool:
        return not any(v.severity == "error" for v in self.violations)


def load_requirements(repo: Path) -> list[RequirementDef]:
    """Load and validate requirement definitions from .exo/requirements.yaml."""
    repo = Path(repo).resolve()
    req_path = repo / REQUIREMENTS_PATH
    if not req_path.exists():
        raise ExoError(
            code="REQUIREMENTS_MANIFEST_MISSING",
            message="requirements.yaml not found — create .exo/requirements.yaml with requirement definitions",
            blocked=True,
        )

    raw = load_yaml(req_path)
    entries = raw.get("requirements", [])
    if not isinstance(entries, list):
        raise ExoError(
            code="REQUIREMENTS_MANIFEST_INVALID",
            message="requirements.yaml 'requirements' must be a list",
            blocked=True,
        )

    reqs: list[RequirementDef] = []
    seen_ids: set[str] = set()

    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ExoError(
                code="REQUIREMENTS_ENTRY_INVALID",
                message=f"requirements[{i}] must be a mapping",
                blocked=True,
            )

        rid = str(entry.get("id", "")).strip()
        if not rid:
            raise ExoError(
                code="REQUIREMENTS_ENTRY_MISSING_ID",
                message=f"requirements[{i}] missing 'id' field",
                blocked=True,
            )

        if rid in seen_ids:
            raise ExoError(
                code="REQUIREMENTS_DUPLICATE_ID",
                message=f"duplicate requirement id: {rid}",
                blocked=True,
            )
        seen_ids.add(rid)

        title = str(entry.get("title", "")).strip()
        if not title:
            raise ExoError(
                code="REQUIREMENTS_ENTRY_MISSING_TITLE",
                message=f"requirement '{rid}' missing 'title' field",
                blocked=True,
            )

        status = str(entry.get("status", "active")).strip().lower()
        if status not in VALID_STATUSES:
            raise ExoError(
                code="REQUIREMENTS_INVALID_STATUS",
                message=f"requirement '{rid}' has invalid status '{status}'; valid: {sorted(VALID_STATUSES)}",
                blocked=True,
            )

        priority = str(entry.get("priority", "medium")).strip().lower()
        if priority not in VALID_PRIORITIES:
            raise ExoError(
                code="REQUIREMENTS_INVALID_PRIORITY",
                message=f"requirement '{rid}' has invalid priority '{priority}'; valid: {sorted(VALID_PRIORITIES)}",
                blocked=True,
            )

        tags_raw = entry.get("tags", [])
        tags = tuple(str(t).strip() for t in tags_raw if str(t).strip()) if isinstance(tags_raw, list) else ()

        reqs.append(
            RequirementDef(
                id=rid,
                title=title,
                status=status,
                description=str(entry.get("description", "")).strip(),
                priority=priority,
                tags=tags,
            )
        )

    return reqs


def _scan_files(repo: Path, globs: list[str] | None = None) -> list[Path]:
    """Find source files to scan, respecting skip directories."""
    patterns = globs or DEFAULT_SCAN_GLOBS
    found: set[Path] = set()
    for pattern in patterns:
        for path in repo.glob(pattern):
            parts = path.relative_to(repo).parts
            if any(part in SKIP_DIRS for part in parts):
                continue
            if path.is_file():
                found.add(path)
    return sorted(found)


def scan_req_refs(repo: Path, *, globs: list[str] | None = None) -> list[ReqCodeRef]:
    """Scan source files for requirement and implements annotations."""
    repo = Path(repo).resolve()
    files = _scan_files(repo, globs)
    refs: list[ReqCodeRef] = []

    for filepath in files:
        try:
            lines = filepath.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        rel = str(filepath.relative_to(repo))

        for line_num, line in enumerate(lines, start=1):
            match = REQ_TAG_PATTERN.search(line)
            if match:
                # Parse comma-separated requirement IDs
                raw_ids = match.group(1)
                for rid in raw_ids.split(","):
                    rid = rid.strip()
                    if rid:
                        refs.append(
                            ReqCodeRef(
                                req_id=rid,
                                file=rel,
                                line=line_num,
                            )
                        )

    return refs


def trace_requirements(
    repo: Path,
    *,
    globs: list[str] | None = None,
    check_uncovered: bool = True,
) -> ReqTraceReport:
    """Run the requirement traceability linter.

    Checks:
    1. Every @req: annotation references a valid requirement ID
    2. Deprecated requirements with code refs produce warnings
    3. Deleted requirements with code refs produce errors
    4. (optional) Active requirements with no code refs are flagged as uncovered

    Args:
        repo: Repository root path.
        globs: Optional file globs to scan.
        check_uncovered: Whether to flag active requirements with no code refs.

    Returns:
        ReqTraceReport with violations and coverage data.
    """
    repo = Path(repo).resolve()
    reqs = load_requirements(repo)
    refs = scan_req_refs(repo, globs=globs)

    req_map: dict[str, RequirementDef] = {r.id: r for r in reqs}
    violations: list[ReqTraceViolation] = []
    covered_ids: set[str] = set()

    for ref in refs:
        req = req_map.get(ref.req_id)

        if req is None:
            violations.append(
                ReqTraceViolation(
                    kind="orphan_ref",
                    req_id=ref.req_id,
                    file=ref.file,
                    line=ref.line,
                    message=f"@req: {ref.req_id} is not defined in requirements.yaml",
                    severity="error",
                )
            )
            continue

        covered_ids.add(ref.req_id)

        if req.status == "deleted":
            violations.append(
                ReqTraceViolation(
                    kind="deleted_ref",
                    req_id=ref.req_id,
                    file=ref.file,
                    line=ref.line,
                    message=f"requirement '{ref.req_id}' is deleted — code reference should be removed",
                    severity="error",
                )
            )
        elif req.status == "deprecated":
            violations.append(
                ReqTraceViolation(
                    kind="deprecated_ref",
                    req_id=ref.req_id,
                    file=ref.file,
                    line=ref.line,
                    message=f"requirement '{ref.req_id}' is deprecated — schedule for removal",
                    severity="warning",
                )
            )

    # Check for uncovered requirements
    uncovered: list[str] = []
    deprecated_with_refs: list[str] = []
    if check_uncovered:
        for req in reqs:
            if req.status == "deleted":
                continue  # deleted requirements SHOULD have no code refs
            if req.id not in covered_ids:
                uncovered.append(req.id)
                if req.status == "active":
                    violations.append(
                        ReqTraceViolation(
                            kind="uncovered_req",
                            req_id=req.id,
                            file="(manifest)",
                            line=None,
                            message=f"requirement '{req.id}' has no @req: annotations in code",
                            severity="warning",
                        )
                    )

    for req in reqs:
        if req.status == "deprecated" and req.id in covered_ids:
            deprecated_with_refs.append(req.id)

    return ReqTraceReport(
        reqs_total=len(reqs),
        reqs_active=sum(1 for r in reqs if r.status == "active"),
        reqs_deprecated=sum(1 for r in reqs if r.status == "deprecated"),
        reqs_deleted=sum(1 for r in reqs if r.status == "deleted"),
        refs_total=len(refs),
        violations=violations,
        covered_reqs=sorted(covered_ids),
        uncovered_reqs=sorted(uncovered),
        deprecated_with_refs=sorted(deprecated_with_refs),
        checked_at=now_iso(),
    )


def req_trace_to_dict(report: ReqTraceReport) -> dict[str, Any]:
    """Convert ReqTraceReport to a plain dict for serialization."""
    return {
        "reqs_total": report.reqs_total,
        "reqs_active": report.reqs_active,
        "reqs_deprecated": report.reqs_deprecated,
        "reqs_deleted": report.reqs_deleted,
        "refs_total": report.refs_total,
        "passed": report.passed,
        "violations": [asdict(v) for v in report.violations],
        "violation_count": len(report.violations),
        "error_count": sum(1 for v in report.violations if v.severity == "error"),
        "warning_count": sum(1 for v in report.violations if v.severity == "warning"),
        "covered_reqs": report.covered_reqs,
        "uncovered_reqs": report.uncovered_reqs,
        "deprecated_with_refs": report.deprecated_with_refs,
        "checked_at": report.checked_at,
    }


def format_req_trace_human(report: ReqTraceReport) -> str:
    """Format requirement trace report as human-readable text."""
    icon = "PASS" if report.passed else "FAIL"
    lines = [
        f"Requirement Traceability: {icon}",
        f"  requirements: {report.reqs_total} total, {report.reqs_active} active, "
        f"{report.reqs_deprecated} deprecated, {report.reqs_deleted} deleted",
        f"  code refs: {report.refs_total}",
        f"  covered: {len(report.covered_reqs)}, uncovered: {len(report.uncovered_reqs)}",
    ]

    if report.deprecated_with_refs:
        lines.append(f"  deprecated with refs: {report.deprecated_with_refs}")

    errors = [v for v in report.violations if v.severity == "error"]
    warnings = [v for v in report.violations if v.severity == "warning"]

    if errors:
        lines.append(f"  errors ({len(errors)}):")
        for v in errors:
            loc = f"{v.file}:{v.line}" if v.line else v.file
            lines.append(f"    - [{v.kind}] {loc}: {v.message}")

    if warnings:
        lines.append(f"  warnings ({len(warnings)}):")
        for v in warnings:
            loc = f"{v.file}:{v.line}" if v.line else v.file
            lines.append(f"    - [{v.kind}] {loc}: {v.message}")

    return "\n".join(lines)


def requirements_to_list(reqs: list[RequirementDef]) -> list[dict[str, Any]]:
    """Convert requirement definitions to plain dicts for serialization."""
    return [asdict(r) for r in reqs]
