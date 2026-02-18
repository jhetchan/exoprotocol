"""Post-execution drift detection: compare what AI did vs declared intent."""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from exo.kernel.tickets import resolve_intent_root
from exo.kernel.utils import any_pattern_matches


@dataclass(frozen=True)
class BudgetUsage:
    used: int
    max: int
    ratio: float


@dataclass(frozen=True)
class DriftReport:
    scope_compliance: float  # 0.0-1.0, fraction of changed files within scope
    budget_files: BudgetUsage
    budget_loc: BudgetUsage
    out_of_scope_files: list[str]
    boundary_violations: list[str]
    drift_score: float  # 0.0 = perfect alignment, 1.0 = total drift
    changed_files: list[str]
    total_loc_changed: int
    intent_root_id: str | None = None
    intent_root_boundary: str = ""


def _run_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _changed_files_since(repo: Path, base_ref: str, ignore_patterns: list[str]) -> list[str]:
    """Get files changed between base_ref and HEAD."""
    proc = _run_git(repo, ["diff", "--name-only", base_ref, "HEAD"])
    if proc.returncode != 0:
        # Fallback: diff against working tree
        proc = _run_git(repo, ["diff", "--name-only", "HEAD"])
        if proc.returncode != 0:
            return []

    files: list[str] = []
    for line in proc.stdout.splitlines():
        path = line.strip()
        if not path:
            continue
        if ignore_patterns and any_pattern_matches(repo / path, ignore_patterns, repo):
            continue
        files.append(path)
    return sorted(files)


def _loc_changed_since(repo: Path, base_ref: str, ignore_patterns: list[str]) -> dict[str, int]:
    """Get lines changed per file between base_ref and HEAD."""
    proc = _run_git(repo, ["diff", "--numstat", base_ref, "HEAD"])
    if proc.returncode != 0:
        proc = _run_git(repo, ["diff", "--numstat", "HEAD"])
        if proc.returncode != 0:
            return {}

    loc_map: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        line = line.rstrip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add_raw, del_raw, path = parts[0], parts[1], parts[-1]
        if not path:
            continue
        if ignore_patterns and any_pattern_matches(repo / path, ignore_patterns, repo):
            continue
        add = int(add_raw) if add_raw.isdigit() else 0
        delete = int(del_raw) if del_raw.isdigit() else 0
        loc_map[path] = add + delete
    return loc_map


def _check_scope_compliance(
    changed_files: list[str],
    scope: dict[str, Any],
    repo: Path,
) -> tuple[float, list[str]]:
    """Check what fraction of changed files fall within declared scope.

    Returns (compliance_ratio, out_of_scope_files).
    """
    if not changed_files:
        return 1.0, []

    allow = scope.get("allow", ["**"])
    deny = scope.get("deny", [])

    out_of_scope: list[str] = []
    in_scope_count = 0

    for rel_path in changed_files:
        full_path = repo / rel_path
        allowed = any_pattern_matches(full_path, allow, repo) if allow else True
        denied = any_pattern_matches(full_path, deny, repo) if deny else False

        if allowed and not denied:
            in_scope_count += 1
        else:
            out_of_scope.append(rel_path)

    compliance = in_scope_count / len(changed_files) if changed_files else 1.0
    return compliance, sorted(out_of_scope)


def _check_boundary_violations(
    changed_files: list[str],
    boundary: str,
    repo: Path,
) -> list[str]:
    """Check if changed files violate the boundary declaration.

    Boundary is plain language, but we extract glob-like patterns from it
    and check against changed files. Patterns are identified by path-like
    tokens (containing / or *).
    """
    if not boundary or not changed_files:
        return []

    # Extract path-like tokens from boundary text
    boundary_patterns: list[str] = []
    for token in boundary.replace(",", " ").split():
        token = token.strip("\"'()[]{}.")
        if "/" in token or "*" in token:
            boundary_patterns.append(token)

    if not boundary_patterns:
        return []

    violations: list[str] = []
    for rel_path in changed_files:
        full_path = repo / rel_path
        if any_pattern_matches(full_path, boundary_patterns, repo):
            violations.append(rel_path)

    return sorted(violations)


def reconcile_session(
    repo: Path,
    ticket: dict[str, Any],
    *,
    git_base: str = "main",
    ignore_patterns: list[str] | None = None,
) -> DriftReport:
    """Compare what the AI actually changed against the declared intent.

    Args:
        repo: Repository root path.
        ticket: The ticket dict (with scope, budgets, boundary, etc).
        git_base: Base branch/ref to diff against.
        ignore_patterns: Git paths to ignore (from git_controls config).

    Returns:
        DriftReport with scope compliance, budget usage, and drift score.
    """
    repo = Path(repo).resolve()
    patterns = ignore_patterns or []

    # Resolve the intent root for this ticket
    intent_root = resolve_intent_root(repo, ticket)
    intent_root_id = str(intent_root["id"]) if intent_root else None

    # Use the most specific scope: ticket's own scope, falling back to intent root
    scope = ticket.get("scope") or {}
    if (not scope.get("allow") or scope.get("allow") == ["**"]) and intent_root:
        root_scope = intent_root.get("scope") or {}
        if root_scope.get("allow") and root_scope.get("allow") != ["**"]:
            scope = root_scope

    # Boundary: merge ticket boundary with intent root boundary
    ticket_boundary = str(ticket.get("boundary") or "")
    root_boundary = str(intent_root.get("boundary", "")) if intent_root else ""
    effective_boundary = ticket_boundary or root_boundary

    # Get actual changes
    changed_files = _changed_files_since(repo, git_base, patterns)
    loc_map = _loc_changed_since(repo, git_base, patterns)
    total_loc = sum(loc_map.values())

    # Scope compliance
    scope_compliance, out_of_scope = _check_scope_compliance(changed_files, scope, repo)

    # Budget usage
    budgets = ticket.get("budgets") or {}
    max_files = int(budgets.get("max_files_changed", 12))
    max_loc = int(budgets.get("max_loc_changed", 400))

    files_used = len(changed_files)
    files_ratio = files_used / max_files if max_files > 0 else 0.0
    loc_ratio = total_loc / max_loc if max_loc > 0 else 0.0

    budget_files = BudgetUsage(used=files_used, max=max_files, ratio=round(files_ratio, 3))
    budget_loc = BudgetUsage(used=total_loc, max=max_loc, ratio=round(loc_ratio, 3))

    # Boundary violations
    boundary_violations = _check_boundary_violations(changed_files, effective_boundary, repo)

    # Drift score:
    # 50% scope violations, 35% file budget, 15% boundary violations
    # (LOC budget removed — poor proxy for scope; kept as advisory in DriftReport)
    drift_score = (
        0.5 * (1.0 - scope_compliance) + 0.35 * min(files_ratio, 1.0) + 0.15 * (1.0 if boundary_violations else 0.0)
    )
    drift_score = round(min(max(drift_score, 0.0), 1.0), 3)

    return DriftReport(
        scope_compliance=round(scope_compliance, 3),
        budget_files=budget_files,
        budget_loc=budget_loc,
        out_of_scope_files=out_of_scope,
        boundary_violations=boundary_violations,
        drift_score=drift_score,
        changed_files=changed_files,
        total_loc_changed=total_loc,
        intent_root_id=intent_root_id,
        intent_root_boundary=root_boundary,
    )


def drift_report_to_dict(report: DriftReport) -> dict[str, Any]:
    """Convert DriftReport to a plain dict for serialization."""
    return asdict(report)


def format_drift_section(report: DriftReport) -> str:
    """Format drift report as a markdown section for mementos."""
    files_pct = f"{int(report.budget_files.ratio * 100)}%" if report.budget_files.max > 0 else "n/a"
    loc_pct = f"{int(report.budget_loc.ratio * 100)}%" if report.budget_loc.max > 0 else "n/a"
    scope_pct = f"{int(report.scope_compliance * 100)}%"

    in_scope = len(report.changed_files) - len(report.out_of_scope_files)
    total = len(report.changed_files)

    lines = [
        "## Drift Report",
        f"- drift_score: {report.drift_score}",
        f"- scope_compliance: {scope_pct} ({in_scope}/{total} files in scope)",
        f"- budget_files: {report.budget_files.used}/{report.budget_files.max} ({files_pct})",
        f"- budget_loc: {report.budget_loc.used}/{report.budget_loc.max} ({loc_pct})",
    ]

    if report.intent_root_id:
        lines.append(f"- intent_root: {report.intent_root_id}")

    if report.out_of_scope_files:
        lines.append(f"- out_of_scope: {report.out_of_scope_files}")
    else:
        lines.append("- out_of_scope: []")

    if report.boundary_violations:
        lines.append(f"- boundary_violations: {report.boundary_violations}")
    else:
        lines.append("- boundary_violations: []")

    return "\n".join(lines)
