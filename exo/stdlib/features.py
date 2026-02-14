"""Feature manifest and traceability linter.

Loads `.exo/features.yaml`, validates feature definitions, scans source files
for `@feature:` / `@endfeature` tags, and cross-references code bindings
against the manifest to detect orphan code, invalid tags, and deprecated usage.

This is deterministic (regex-based, no LLM) and designed to run as a governed
check at session-finish or in CI.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from exo.kernel.errors import ExoError
from exo.kernel.utils import load_yaml, now_iso

FEATURES_PATH = Path(".exo/features.yaml")

VALID_STATUSES = frozenset({"active", "experimental", "deprecated", "deleted"})

# Regex patterns for code tags
TAG_PATTERN = re.compile(r"[#/]\s*@feature:\s*(\S+)", re.IGNORECASE)
END_TAG_PATTERN = re.compile(r"[#/]\s*@endfeature", re.IGNORECASE)

# Default file extensions to scan
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
    }
)


@dataclass(frozen=True)
class FeatureDef:
    """A feature definition from the manifest."""

    id: str
    status: str  # active | experimental | deprecated | deleted
    description: str = ""
    owner: str = ""
    files: tuple[str, ...] = ()  # expected file globs
    allow_agent_edit: bool = True


@dataclass(frozen=True)
class CodeTag:
    """A @feature tag found in source code."""

    feature_id: str
    file: str  # relative path
    line: int
    end_line: int | None = None  # line of @endfeature, if present


@dataclass(frozen=True)
class TraceViolation:
    """A traceability violation found by the linter."""

    kind: str  # invalid_tag | deprecated_usage | deleted_usage | unbound_feature | locked_edit
    feature_id: str
    file: str  # relative path or "(manifest)"
    line: int | None
    message: str
    severity: str = "error"  # error | warning


@dataclass
class TraceReport:
    """Result of running the traceability linter."""

    features_total: int
    features_active: int
    features_deprecated: int
    features_deleted: int
    tags_total: int
    violations: list[TraceViolation]
    bound_features: list[str]  # feature IDs with at least one code tag
    unbound_features: list[str]  # feature IDs with no code tags
    deprecated_with_code: list[str]  # deprecated features still with code
    checked_at: str = ""

    @property
    def passed(self) -> bool:
        return not any(v.severity == "error" for v in self.violations)


def load_features(repo: Path) -> list[FeatureDef]:
    """Load and validate feature definitions from .exo/features.yaml."""
    repo = Path(repo).resolve()
    features_path = repo / FEATURES_PATH
    if not features_path.exists():
        raise ExoError(
            code="FEATURES_MANIFEST_MISSING",
            message="features.yaml not found — create .exo/features.yaml with feature definitions",
            blocked=True,
        )

    raw = load_yaml(features_path)
    entries = raw.get("features", [])
    if not isinstance(entries, list):
        raise ExoError(
            code="FEATURES_MANIFEST_INVALID",
            message="features.yaml 'features' must be a list",
            blocked=True,
        )

    features: list[FeatureDef] = []
    seen_ids: set[str] = set()

    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ExoError(
                code="FEATURES_ENTRY_INVALID",
                message=f"features[{i}] must be a mapping",
                blocked=True,
            )

        fid = str(entry.get("id", "")).strip()
        if not fid:
            raise ExoError(
                code="FEATURES_ENTRY_MISSING_ID",
                message=f"features[{i}] missing 'id' field",
                blocked=True,
            )

        if fid in seen_ids:
            raise ExoError(
                code="FEATURES_DUPLICATE_ID",
                message=f"duplicate feature id: {fid}",
                blocked=True,
            )
        seen_ids.add(fid)

        status = str(entry.get("status", "active")).strip().lower()
        if status not in VALID_STATUSES:
            raise ExoError(
                code="FEATURES_INVALID_STATUS",
                message=f"feature '{fid}' has invalid status '{status}'; valid: {sorted(VALID_STATUSES)}",
                blocked=True,
            )

        files_raw = entry.get("files", [])
        files = tuple(str(f).strip() for f in files_raw if str(f).strip()) if isinstance(files_raw, list) else ()

        features.append(
            FeatureDef(
                id=fid,
                status=status,
                description=str(entry.get("description", "")).strip(),
                owner=str(entry.get("owner", "")).strip(),
                files=files,
                allow_agent_edit=bool(entry.get("allow_agent_edit", True)),
            )
        )

    return features


def _scan_files(repo: Path, globs: list[str] | None = None) -> list[Path]:
    """Find source files to scan, respecting skip directories."""
    patterns = globs or DEFAULT_SCAN_GLOBS
    found: set[Path] = set()
    for pattern in patterns:
        for path in repo.glob(pattern):
            # Skip files in excluded directories
            parts = path.relative_to(repo).parts
            if any(part in SKIP_DIRS for part in parts):
                continue
            if path.is_file():
                found.add(path)
    return sorted(found)


def scan_tags(repo: Path, *, globs: list[str] | None = None) -> list[CodeTag]:
    """Scan source files for @feature: / @endfeature tags."""
    repo = Path(repo).resolve()
    files = _scan_files(repo, globs)
    tags: list[CodeTag] = []

    for filepath in files:
        try:
            lines = filepath.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        rel = str(filepath.relative_to(repo))
        open_tags: list[tuple[str, int]] = []  # (feature_id, start_line)

        for line_num, line in enumerate(lines, start=1):
            start_match = TAG_PATTERN.search(line)
            if start_match:
                feature_id = start_match.group(1)
                open_tags.append((feature_id, line_num))
                continue

            end_match = END_TAG_PATTERN.search(line)
            if end_match and open_tags:
                fid, start = open_tags.pop()
                tags.append(
                    CodeTag(
                        feature_id=fid,
                        file=rel,
                        line=start,
                        end_line=line_num,
                    )
                )

        # Any tags without @endfeature are still valid (single-point binding)
        for fid, start in open_tags:
            tags.append(
                CodeTag(
                    feature_id=fid,
                    file=rel,
                    line=start,
                    end_line=None,
                )
            )

    return tags


def trace(
    repo: Path,
    *,
    globs: list[str] | None = None,
    check_unbound: bool = True,
) -> TraceReport:
    """Run the traceability linter: cross-reference code tags against the feature manifest.

    Checks:
    1. Every @feature: tag references a valid feature ID
    2. Deprecated features with code tags produce warnings
    3. Deleted features with code tags produce errors
    4. Features with allow_agent_edit=false flag locked edits
    5. (optional) Features with no code tags are flagged as unbound

    Args:
        repo: Repository root path.
        globs: Optional file globs to scan (default: common source extensions).
        check_unbound: Whether to flag features with no code bindings.

    Returns:
        TraceReport with violations and coverage data.
    """
    repo = Path(repo).resolve()
    features = load_features(repo)
    tags = scan_tags(repo, globs=globs)

    feature_map: dict[str, FeatureDef] = {f.id: f for f in features}
    violations: list[TraceViolation] = []
    bound_ids: set[str] = set()

    for tag in tags:
        feat = feature_map.get(tag.feature_id)

        if feat is None:
            # Invalid tag — references non-existent feature
            violations.append(
                TraceViolation(
                    kind="invalid_tag",
                    feature_id=tag.feature_id,
                    file=tag.file,
                    line=tag.line,
                    message=f"@feature: {tag.feature_id} is not defined in features.yaml",
                    severity="error",
                )
            )
            continue

        bound_ids.add(tag.feature_id)

        if feat.status == "deleted":
            violations.append(
                TraceViolation(
                    kind="deleted_usage",
                    feature_id=tag.feature_id,
                    file=tag.file,
                    line=tag.line,
                    message=f"feature '{tag.feature_id}' is deleted — code should be removed",
                    severity="error",
                )
            )
        elif feat.status == "deprecated":
            violations.append(
                TraceViolation(
                    kind="deprecated_usage",
                    feature_id=tag.feature_id,
                    file=tag.file,
                    line=tag.line,
                    message=f"feature '{tag.feature_id}' is deprecated — schedule for removal",
                    severity="warning",
                )
            )

        if not feat.allow_agent_edit:
            violations.append(
                TraceViolation(
                    kind="locked_edit",
                    feature_id=tag.feature_id,
                    file=tag.file,
                    line=tag.line,
                    message=f"feature '{tag.feature_id}' has allow_agent_edit=false — human-only modification",
                    severity="warning",
                )
            )

    # Check for unbound features (features with no code tags)
    unbound: list[str] = []
    deprecated_with_code: list[str] = []
    if check_unbound:
        for feat in features:
            if feat.status in ("deleted",):
                continue  # deleted features SHOULD have no code
            if feat.id not in bound_ids:
                unbound.append(feat.id)
                if feat.status == "active":
                    violations.append(
                        TraceViolation(
                            kind="unbound_feature",
                            feature_id=feat.id,
                            file="(manifest)",
                            line=None,
                            message=f"feature '{feat.id}' has no @feature: tags in code",
                            severity="warning",
                        )
                    )

    for feat in features:
        if feat.status == "deprecated" and feat.id in bound_ids:
            deprecated_with_code.append(feat.id)

    return TraceReport(
        features_total=len(features),
        features_active=sum(1 for f in features if f.status == "active"),
        features_deprecated=sum(1 for f in features if f.status == "deprecated"),
        features_deleted=sum(1 for f in features if f.status == "deleted"),
        tags_total=len(tags),
        violations=violations,
        bound_features=sorted(bound_ids),
        unbound_features=sorted(unbound),
        deprecated_with_code=sorted(deprecated_with_code),
        checked_at=now_iso(),
    )


def trace_to_dict(report: TraceReport) -> dict[str, Any]:
    """Convert TraceReport to a plain dict for serialization."""
    return {
        "features_total": report.features_total,
        "features_active": report.features_active,
        "features_deprecated": report.features_deprecated,
        "features_deleted": report.features_deleted,
        "tags_total": report.tags_total,
        "passed": report.passed,
        "violations": [asdict(v) for v in report.violations],
        "violation_count": len(report.violations),
        "error_count": sum(1 for v in report.violations if v.severity == "error"),
        "warning_count": sum(1 for v in report.violations if v.severity == "warning"),
        "bound_features": report.bound_features,
        "unbound_features": report.unbound_features,
        "deprecated_with_code": report.deprecated_with_code,
        "checked_at": report.checked_at,
    }


def format_trace_human(report: TraceReport) -> str:
    """Format trace report as human-readable text."""
    icon = "PASS" if report.passed else "FAIL"
    lines = [
        f"Feature Traceability: {icon}",
        f"  features: {report.features_total} total, {report.features_active} active, "
        f"{report.features_deprecated} deprecated, {report.features_deleted} deleted",
        f"  code tags: {report.tags_total}",
        f"  bound: {len(report.bound_features)}, unbound: {len(report.unbound_features)}",
    ]

    if report.deprecated_with_code:
        lines.append(f"  deprecated with code: {report.deprecated_with_code}")

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


def features_to_list(features: list[FeatureDef]) -> list[dict[str, Any]]:
    """Convert feature definitions to plain dicts for serialization."""
    return [asdict(f) for f in features]


def generate_scope_deny(features: list[FeatureDef]) -> list[str]:
    """Generate scope.deny globs from features with allow_agent_edit=false."""
    deny: list[str] = []
    for feat in features:
        if not feat.allow_agent_edit and feat.files:
            deny.extend(feat.files)
    return sorted(set(deny))


@dataclass(frozen=True)
class PruneAction:
    """A single code block removal performed by prune."""

    feature_id: str
    file: str  # relative path
    start_line: int
    end_line: int  # inclusive
    lines_removed: int


@dataclass
class PruneReport:
    """Result of running the prune operation."""

    pruned: list[PruneAction]
    files_modified: list[str]
    total_lines_removed: int
    features_pruned: list[str]  # unique feature IDs that had code removed
    dry_run: bool
    pruned_at: str = ""


def prune(
    repo: Path,
    *,
    include_deprecated: bool = False,
    globs: list[str] | None = None,
    dry_run: bool = False,
) -> PruneReport:
    """Remove code blocks tagged with deleted (or deprecated) features.

    Scans for @feature: / @endfeature tag pairs where the referenced feature
    has status 'deleted' (or 'deprecated' if include_deprecated=True).
    Removes the entire block including the tag lines.

    Single-point tags (no @endfeature) remove only the tag line itself.

    Args:
        repo: Repository root path.
        include_deprecated: Also prune deprecated features (default: only deleted).
        globs: Optional file globs to scan.
        dry_run: Preview removals without modifying files.

    Returns:
        PruneReport with details of what was (or would be) removed.
    """
    repo = Path(repo).resolve()
    features = load_features(repo)
    {f.id: f for f in features}

    prune_statuses = {"deleted"}
    if include_deprecated:
        prune_statuses.add("deprecated")

    # Collect prunable feature IDs
    prunable_ids: set[str] = {f.id for f in features if f.status in prune_statuses}

    if not prunable_ids:
        return PruneReport(
            pruned=[],
            files_modified=[],
            total_lines_removed=0,
            features_pruned=[],
            dry_run=dry_run,
            pruned_at=now_iso(),
        )

    files = _scan_files(repo, globs)
    actions: list[PruneAction] = []
    modified_files: set[str] = set()

    for filepath in files:
        try:
            content = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        lines = content.splitlines(keepends=True)
        rel = str(filepath.relative_to(repo))

        # Find all blocks to remove (work backwards to preserve line numbers)
        removals: list[tuple[int, int, str]] = []  # (start_idx, end_idx_inclusive, feature_id)
        open_tags: list[tuple[str, int]] = []  # (feature_id, start_line_idx)

        for idx, line in enumerate(lines):
            start_match = TAG_PATTERN.search(line)
            if start_match:
                fid = start_match.group(1)
                open_tags.append((fid, idx))
                continue

            end_match = END_TAG_PATTERN.search(line)
            if end_match and open_tags:
                fid, start_idx = open_tags.pop()
                if fid in prunable_ids:
                    removals.append((start_idx, idx, fid))

        # Unclosed tags — remove just the tag line
        for fid, start_idx in open_tags:
            if fid in prunable_ids:
                removals.append((start_idx, start_idx, fid))

        if not removals:
            continue

        # Sort removals by start line (descending) so we can remove from bottom up
        removals.sort(key=lambda r: r[0], reverse=True)

        new_lines = list(lines)
        for start_idx, end_idx, fid in removals:
            lines_removed = end_idx - start_idx + 1
            actions.append(
                PruneAction(
                    feature_id=fid,
                    file=rel,
                    start_line=start_idx + 1,  # 1-indexed
                    end_line=end_idx + 1,  # 1-indexed
                    lines_removed=lines_removed,
                )
            )
            del new_lines[start_idx : end_idx + 1]

        modified_files.add(rel)

        if not dry_run:
            filepath.write_text("".join(new_lines), encoding="utf-8")

    # Sort actions by file then line for deterministic output
    actions.sort(key=lambda a: (a.file, a.start_line))
    pruned_feature_ids = sorted({a.feature_id for a in actions})

    return PruneReport(
        pruned=actions,
        files_modified=sorted(modified_files),
        total_lines_removed=sum(a.lines_removed for a in actions),
        features_pruned=pruned_feature_ids,
        dry_run=dry_run,
        pruned_at=now_iso(),
    )


def prune_to_dict(report: PruneReport) -> dict[str, Any]:
    """Convert PruneReport to a plain dict for serialization."""
    return {
        "pruned": [asdict(a) for a in report.pruned],
        "pruned_count": len(report.pruned),
        "files_modified": report.files_modified,
        "files_modified_count": len(report.files_modified),
        "total_lines_removed": report.total_lines_removed,
        "features_pruned": report.features_pruned,
        "dry_run": report.dry_run,
        "pruned_at": report.pruned_at,
    }


def format_prune_human(report: PruneReport) -> str:
    """Format prune report as human-readable text."""
    mode = "DRY RUN" if report.dry_run else "PRUNED"
    lines = [
        f"Feature Prune: {mode}",
        f"  blocks removed: {len(report.pruned)}",
        f"  lines removed: {report.total_lines_removed}",
        f"  files modified: {len(report.files_modified)}",
        f"  features: {report.features_pruned}",
    ]

    if report.pruned:
        lines.append("  details:")
        for action in report.pruned:
            loc = (
                f"{action.file}:{action.start_line}-{action.end_line}"
                if action.start_line != action.end_line
                else f"{action.file}:{action.start_line}"
            )
            lines.append(f"    - [{action.feature_id}] {loc} ({action.lines_removed} lines)")

    return "\n".join(lines)
