"""Semantic coherence detection.

Deterministic checks for co-update rules and docstring freshness.
Detects when AI agents change code without updating corresponding
documentation, co-dependent files, or inline docstrings.

Two check types:
1. Co-update rules — config-driven file pairs that must change together.
2. Docstring freshness — flags functions whose body changed but docstring didn't.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from exo.kernel.utils import load_yaml, now_iso


# ── Dataclasses ──────────────────────────────────────────────────

@dataclass
class CoherenceViolation:
    """A single coherence violation."""
    kind: str       # "co_update" | "stale_docstring"
    severity: str   # "warning"
    file: str       # relative path
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class CoherenceReport:
    """Result of coherence checks."""
    violations: list[CoherenceViolation]
    files_checked: int
    functions_checked: int
    checked_at: str

    @property
    def passed(self) -> bool:
        return not any(v.severity == "error" for v in self.violations)

    @property
    def warning_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "warning")


@dataclass
class FunctionRegion:
    """Describes a Python function's layout in a file."""
    name: str
    def_line: int           # 1-based line of `def`
    docstring_start: int    # 1-based, 0 if no docstring
    docstring_end: int      # 1-based, 0 if no docstring
    body_start: int         # 1-based first line after docstring (or after def)
    body_end: int           # 1-based last line of function body


# ── Git helpers ──────────────────────────────────────────────────

def _run_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _changed_files_since(repo: Path, base: str) -> list[str]:
    """Get list of changed files between base and HEAD."""
    proc = _run_git(repo, ["diff", "--name-only", base, "HEAD"])
    if proc.returncode != 0:
        proc = _run_git(repo, ["diff", "--name-only", "HEAD"])
        if proc.returncode != 0:
            return []
    files: list[str] = []
    for line in proc.stdout.splitlines():
        path = line.strip()
        if path:
            files.append(path)
    return sorted(files)


def _changed_line_ranges(repo: Path, base: str, filepath: str) -> list[tuple[int, int]]:
    """Get changed line ranges (1-based, inclusive) for a file.

    Uses ``git diff -U0`` to extract exact changed hunks in the new file.
    """
    proc = _run_git(repo, ["diff", "-U0", base, "HEAD", "--", filepath])
    if proc.returncode != 0:
        return []

    ranges: list[tuple[int, int]] = []
    # Match @@ -old +new @@ hunk headers
    hunk_re = re.compile(r"^@@\s+\S+\s+\+(\d+)(?:,(\d+))?\s+@@", re.MULTILINE)
    for m in hunk_re.finditer(proc.stdout):
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) is not None else 1
        if count == 0:
            # Pure deletion hunk — no new lines
            continue
        end = start + count - 1
        ranges.append((start, end))
    return ranges


# ── Symbol extraction ────────────────────────────────────────────

_DEF_RE = re.compile(r"^([ \t]*)def\s+(\w+)\s*\(", re.MULTILINE)
_DOCSTRING_OPEN_RE = re.compile(r'^\s*("""|\'\'\')')


def _find_python_functions(content: str) -> list[FunctionRegion]:
    """Extract function regions from Python source.

    Returns list of FunctionRegion with 1-based line numbers.
    """
    lines = content.splitlines()
    if not lines:
        return []

    # Find all def positions
    defs: list[tuple[int, str, int]] = []  # (line_1based, name, indent_len)
    for m in _DEF_RE.finditer(content):
        line_0 = content[:m.start()].count("\n")
        indent = len(m.group(1))
        name = m.group(2)
        defs.append((line_0 + 1, name, indent))

    regions: list[FunctionRegion] = []
    for i, (def_line, name, indent) in enumerate(defs):
        # Find function end: next def at same/lesser indent, or end of file
        body_end = len(lines)
        for j in range(i + 1, len(defs)):
            next_def_line, _, next_indent = defs[j]
            if next_indent <= indent:
                # Function ends at line before next def at same/outer level
                body_end = next_def_line - 1
                break

        # Find docstring (immediately after def line, possibly multi-line def)
        # Skip past the def line(s) to find the colon
        doc_start = 0
        doc_end = 0

        # Walk from def_line to find the first non-blank content line after `:`
        first_body_line = def_line + 1
        # Handle multi-line def signatures
        for ln in range(def_line - 1, min(def_line + 20, len(lines))):
            if ln < 0:
                continue
            if ":" in lines[ln]:
                first_body_line = ln + 2  # 1-based: line after the colon line
                break

        # Check if first non-blank line after def is a docstring
        for ln in range(first_body_line - 1, min(first_body_line + 5, len(lines))):
            stripped = lines[ln].strip() if ln < len(lines) else ""
            if not stripped:
                continue
            m_doc = _DOCSTRING_OPEN_RE.match(lines[ln])
            if m_doc:
                quote = m_doc.group(1)
                doc_start = ln + 1  # 1-based
                # Find closing triple-quote
                if lines[ln].strip().count(quote) >= 2 and len(lines[ln].strip()) > len(quote):
                    # Single-line docstring
                    doc_end = ln + 1
                else:
                    for k in range(ln + 1, len(lines)):
                        if quote in lines[k]:
                            doc_end = k + 1  # 1-based
                            break
                    else:
                        doc_end = doc_start  # unterminated
            break  # only check first non-blank line

        body_start = (doc_end + 1) if doc_end else first_body_line

        regions.append(FunctionRegion(
            name=name,
            def_line=def_line,
            docstring_start=doc_start,
            docstring_end=doc_end,
            body_start=body_start,
            body_end=body_end,
        ))

    return regions


def _ranges_overlap(ranges: list[tuple[int, int]], start: int, end: int) -> bool:
    """Check if any range overlaps with [start, end] (1-based inclusive)."""
    for r_start, r_end in ranges:
        if r_start <= end and r_end >= start:
            return True
    return False


# ── Co-update check ──────────────────────────────────────────────

def check_co_updates(
    changed_files: list[str],
    rules: list[dict[str, Any]],
) -> list[CoherenceViolation]:
    """Check co-update rules against changed files.

    Each rule: {"files": ["A", "B"], "label": "description"}
    If any file in the group changed but not all → violation per missing file.
    """
    violations: list[CoherenceViolation] = []
    changed_set = set(changed_files)

    for rule in rules:
        files = rule.get("files", [])
        if not files or not isinstance(files, list):
            continue
        label = str(rule.get("label", "co-update rule"))

        changed_in_group = [f for f in files if f in changed_set]
        missing_in_group = [f for f in files if f not in changed_set]

        if changed_in_group and missing_in_group:
            for missing in missing_in_group:
                violations.append(CoherenceViolation(
                    kind="co_update",
                    severity="warning",
                    file=missing,
                    message=f"{label}: {', '.join(changed_in_group)} changed but {missing} was not updated",
                    detail={
                        "label": label,
                        "changed": changed_in_group,
                        "missing": missing_in_group,
                    },
                ))

    return violations


# ── Docstring freshness check ────────────────────────────────────

def check_docstring_freshness(
    repo: Path,
    changed_files: list[str],
    base: str,
    languages: list[str],
    skip_patterns: list[str] | None = None,
) -> tuple[list[CoherenceViolation], int]:
    """Check for stale docstrings in changed Python files.

    Returns (violations, functions_checked).
    """
    violations: list[CoherenceViolation] = []
    total_functions = 0

    ext_set = set(f".{lang}" for lang in languages)

    for filepath in changed_files:
        p = Path(filepath)
        if p.suffix not in ext_set:
            continue

        if skip_patterns:
            from exo.kernel.utils import any_pattern_matches
            if any_pattern_matches(repo / filepath, skip_patterns, repo):
                continue

        full_path = repo / filepath
        if not full_path.is_file():
            continue

        try:
            content = full_path.read_text(encoding="utf-8")
        except OSError:
            continue

        functions = _find_python_functions(content)
        if not functions:
            continue

        changed_ranges = _changed_line_ranges(repo, base, filepath)
        if not changed_ranges:
            continue

        for fn in functions:
            total_functions += 1

            body_changed = _ranges_overlap(changed_ranges, fn.body_start, fn.body_end)
            if not body_changed:
                continue

            # No docstring → nothing to be stale
            if not fn.docstring_start:
                continue

            docstring_changed = _ranges_overlap(
                changed_ranges, fn.docstring_start, fn.docstring_end
            )
            if not docstring_changed:
                violations.append(CoherenceViolation(
                    kind="stale_docstring",
                    severity="warning",
                    file=filepath,
                    message=f"function `{fn.name}` body changed but docstring was not updated",
                    detail={
                        "function": fn.name,
                        "def_line": fn.def_line,
                        "docstring_lines": [fn.docstring_start, fn.docstring_end],
                        "body_lines": [fn.body_start, fn.body_end],
                    },
                ))

    return violations, total_functions


# ── Orchestrator ─────────────────────────────────────────────────

CONFIG_PATH = Path(".exo/config.yaml")


def check_coherence(
    repo: Path,
    *,
    base: str = "main",
    skip_co_updates: bool = False,
    skip_docstrings: bool = False,
) -> CoherenceReport:
    """Run all coherence checks.

    Args:
        repo: Repository root path.
        base: Git base ref to diff against.
        skip_co_updates: Skip co-update rule checks.
        skip_docstrings: Skip docstring freshness checks.

    Returns:
        CoherenceReport with violations and metadata.
    """
    repo = Path(repo).resolve()
    violations: list[CoherenceViolation] = []
    functions_checked = 0

    # Load config
    config: dict[str, Any] = {}
    config_path = repo / CONFIG_PATH
    if config_path.exists():
        try:
            full_config = load_yaml(config_path)
            config = full_config.get("coherence", {}) if isinstance(full_config, dict) else {}
        except Exception:
            pass

    if not config.get("enabled", True):
        return CoherenceReport(
            violations=[],
            files_checked=0,
            functions_checked=0,
            checked_at=now_iso(),
        )

    # Get changed files
    changed_files = _changed_files_since(repo, base)

    # Co-update rules
    if not skip_co_updates:
        rules = config.get("co_update_rules", [])
        if rules:
            violations.extend(check_co_updates(changed_files, rules))

    # Docstring freshness
    if not skip_docstrings:
        languages = config.get("docstring_languages", ["py"])
        skip_patterns = config.get("skip_patterns", [])
        doc_violations, fn_count = check_docstring_freshness(
            repo, changed_files, base, languages, skip_patterns or None,
        )
        violations.extend(doc_violations)
        functions_checked = fn_count

    return CoherenceReport(
        violations=violations,
        files_checked=len(changed_files),
        functions_checked=functions_checked,
        checked_at=now_iso(),
    )


# ── Serialization ────────────────────────────────────────────────

def coherence_to_dict(report: CoherenceReport) -> dict[str, Any]:
    """Convert CoherenceReport to a plain dict."""
    return {
        "passed": report.passed,
        "warning_count": report.warning_count,
        "files_checked": report.files_checked,
        "functions_checked": report.functions_checked,
        "violations": [
            {
                "kind": v.kind,
                "severity": v.severity,
                "file": v.file,
                "message": v.message,
                "detail": v.detail,
            }
            for v in report.violations
        ],
        "checked_at": report.checked_at,
    }


def format_coherence_human(report: CoherenceReport) -> str:
    """Format CoherenceReport as human-readable text."""
    icon = "PASS" if report.passed else "FAIL"
    co_update_count = sum(1 for v in report.violations if v.kind == "co_update")
    docstring_count = sum(1 for v in report.violations if v.kind == "stale_docstring")

    lines = [
        f"Coherence: {icon}",
        f"  files: {report.files_checked}, functions: {report.functions_checked}, warnings: {report.warning_count}",
    ]

    if co_update_count:
        lines.append(f"  co-update violations: {co_update_count}")
    if docstring_count:
        lines.append(f"  stale docstrings: {docstring_count}")

    for v in report.violations:
        tag = "WARN" if v.severity == "warning" else "ERR"
        lines.append(f"  [{tag}] {v.file}: {v.message}")

    if not report.violations:
        lines.append("  No issues found.")

    return "\n".join(lines)
