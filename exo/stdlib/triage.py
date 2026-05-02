"""Stale-test triage (closes feedback #6).

When a test fails, agents currently treat all failures as regressions and
patch the new code. But sometimes the test itself is stale (assertions
encoding obsolete behavior, fixtures missing context, environment drift).

``triage_test()`` produces evidence + a classification + a recommended
owner. It does NOT autonomously delete or rewrite tests — that is a
judgment call with too much blast radius for deterministic governance.
The agent (or human) reads the report and decides.
"""
# @feature:test-triage

from __future__ import annotations

import contextlib
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from exo.kernel.errors import ExoError


@dataclass
class TriageReport:
    """Classification of a failing test as stale / regression / ambiguous.

    Fields:
        test_path: Repo-relative path to the test file.
        classification: ``stale`` | ``regression`` | ``ambiguous`` | ``unknown``.
        recommended_owner: Best-guess owner (commit author).
        test_authored_at: ISO 8601 of the test's most recent edit.
        test_authored_by: Author of the test's most recent edit.
        behavior_last_changed_at: ISO 8601 of the last non-test edit
            within ``window_days`` of now.
        behavior_last_changed_by: Author of that edit.
        evidence: Free-form lines describing the reasoning.
        rationale: One-line summary suitable for a memento or PR comment.
    """

    test_path: str
    classification: str
    recommended_owner: str = ""
    test_authored_at: str = ""
    test_authored_by: str = ""
    behavior_last_changed_at: str = ""
    behavior_last_changed_by: str = ""
    evidence: list[str] = field(default_factory=list)
    rationale: str = ""


def _git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )


def _path_visible_to_outer_repo(repo: Path, path: Path) -> bool:
    """Return False when path lives outside outer-repo blame visibility.

    A path is invisible if it is inside a registered submodule or not tracked
    by the outer repo at all (untracked / runtime-generated / vendored).
    """
    rel = str(path)
    # Check submodule membership
    sub = _git(repo, ["submodule", "status", rel])
    if sub.returncode == 0 and sub.stdout.strip():
        return False
    # Check whether git tracks the file at all
    ls = _git(repo, ["ls-files", "--error-unmatch", rel])
    return ls.returncode == 0


def _last_commit_for(repo: Path, path: Path) -> tuple[str, str, str]:
    """Return (sha, iso_timestamp, author) for the most recent commit
    touching *path*, or ('', '', '') if none.
    """
    rel = str(path)
    proc = _git(repo, ["log", "-1", "--format=%H|%aI|%an", "--", rel])
    if proc.returncode != 0 or not proc.stdout.strip():
        return ("", "", "")
    sha, iso, author = proc.stdout.strip().split("|", 2)
    return (sha, iso, author)


def _last_non_test_commit_in_window(repo: Path, *, since: datetime, exclude: Path) -> tuple[str, str, str, str]:
    """Find the most recent commit changing a non-test file since *since*.

    Returns (sha, iso_timestamp, author, file). Empty strings if none found.
    """
    since_iso = since.astimezone(timezone.utc).isoformat()
    proc = _git(
        repo,
        [
            "log",
            f"--since={since_iso}",
            "--name-only",
            "--format=%H|%aI|%an|::COMMIT::",
        ],
    )
    if proc.returncode != 0:
        return ("", "", "", "")
    rel_excluded = str(exclude)

    sha = iso = author = ""
    for line in proc.stdout.splitlines():
        if line.endswith("::COMMIT::"):
            sha, iso, author, _ = line.split("|", 3)
            continue
        if not line.strip():
            continue
        # Skip the test file itself, anything under tests/, anything inside .exo/
        if line == rel_excluded:
            continue
        if line.startswith("tests/") or line.startswith("test/"):
            continue
        if line.startswith(".exo/"):
            continue
        return (sha, iso, author, line)
    return ("", "", "", "")


def _parse_iso(iso_str: str) -> datetime | None:
    if not iso_str:
        return None
    s = iso_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def triage_test(
    repo: Path | str,
    test_path: str | Path,
    *,
    window_days: int = 30,
    now: datetime | None = None,
) -> TriageReport:
    """Classify a failing test as stale, regression, or ambiguous.

    Algorithm (deterministic, evidence-only — no LLM):

      1. Find the most recent commit that touched the test file
         (``git log -1 -- <test_path>``).
      2. Look back *window_days* before now for the most recent commit
         that changed any non-test, non-governance file.
      3. Classify:
         - ``regression`` if the behavior change is NEWER than the test edit.
         - ``stale`` if the test edit is OLDER than the window AND no
           recent behavior change was found.
         - ``ambiguous`` otherwise (both are recent and within hours of
           each other, or no behavior change found but test is also recent).
         - ``unknown`` if the test file isn't in the repo.

    Returns evidence and a recommended owner; never deletes or rewrites.
    """
    repo = Path(repo).resolve()
    rel_test_path = Path(str(test_path)).as_posix().lstrip("./")
    abs_test_path = repo / rel_test_path
    if not abs_test_path.exists():
        raise ExoError(
            code="TEST_NOT_FOUND",
            message=f"Test path not found in repo: {rel_test_path}",
            details={"repo": str(repo), "path": rel_test_path},
            blocked=True,
        )

    rel = Path(rel_test_path)

    if not _path_visible_to_outer_repo(repo, rel):
        return TriageReport(
            test_path=rel_test_path,
            classification="unknown",
            recommended_owner="",
            evidence=["test lives outside outer-repo blame visibility (submodule / untracked / runtime-generated)"],
            rationale="Cannot classify: test file is not visible to the outer repo's git history.",
        )

    test_sha, test_iso, test_author = _last_commit_for(repo, rel)
    test_dt = _parse_iso(test_iso)
    now_dt = now or datetime.now(timezone.utc)
    window_start = now_dt - timedelta(days=max(window_days, 1))

    behavior_sha, behavior_iso, behavior_author, behavior_file = _last_non_test_commit_in_window(
        repo, since=window_start, exclude=rel
    )
    behavior_dt = _parse_iso(behavior_iso)

    evidence: list[str] = []
    if test_sha:
        evidence.append(f"test edit: {test_sha[:8]} on {test_iso} by {test_author}")
    else:
        evidence.append("test file has no commits (untracked or new)")
    if behavior_sha:
        evidence.append(
            f"recent behavior edit: {behavior_sha[:8]} on {behavior_iso} by {behavior_author} ({behavior_file})"
        )
    else:
        evidence.append(f"no non-test edits in the last {window_days} days")

    classification = "ambiguous"
    rationale = ""
    recommended_owner = test_author

    if not test_sha:
        classification = "unknown"
        rationale = "Test file is not committed; cannot infer authorship."
    elif behavior_dt and test_dt and behavior_dt > test_dt:
        classification = "regression"
        rationale = (
            "Behavior changed AFTER the test was last touched — likely a regression introduced by the recent edit."
        )
        recommended_owner = behavior_author or test_author
    elif test_dt and not behavior_dt and test_dt < window_start:
        # Distinguish a quiet repo (no commits at all in the window) from a truly
        # stale test (commits exist but none touched behaviour code).
        rev_count = _git(
            repo, ["rev-list", "--count", f"--since={window_start.astimezone(timezone.utc).isoformat()}", "HEAD"]
        )
        total_in_window = 0
        with contextlib.suppress(ValueError, AttributeError):
            total_in_window = int(rev_count.stdout.strip())
        if total_in_window == 0:
            classification = "ambiguous"
            rationale = (
                f"Test predates the {window_days}-day window but the repo has zero commits "
                f"in that period — repo is simply quiet, not the test stale."
            )
        else:
            classification = "stale"
            rationale = (
                f"Test predates the {window_days}-day window and no recent behavior "
                f"changes were found — likely encoding obsolete behavior."
            )
    elif test_dt and behavior_dt:
        delta = abs((test_dt - behavior_dt).total_seconds())
        if delta < 24 * 3600:
            rationale = (
                "Test and behavior changes happened within ~24h — both may be in flight; human judgment required."
            )
        else:
            rationale = "Insufficient timing signal — classify manually."
    else:
        rationale = "Insufficient git history — classify manually."

    return TriageReport(
        test_path=rel_test_path,
        classification=classification,
        recommended_owner=recommended_owner,
        test_authored_at=test_iso,
        test_authored_by=test_author,
        behavior_last_changed_at=behavior_iso,
        behavior_last_changed_by=behavior_author,
        evidence=evidence,
        rationale=rationale,
    )


def triage_to_dict(report: TriageReport) -> dict[str, Any]:
    return {
        "test_path": report.test_path,
        "classification": report.classification,
        "recommended_owner": report.recommended_owner,
        "test_authored_at": report.test_authored_at,
        "test_authored_by": report.test_authored_by,
        "behavior_last_changed_at": report.behavior_last_changed_at,
        "behavior_last_changed_by": report.behavior_last_changed_by,
        "evidence": list(report.evidence),
        "rationale": report.rationale,
    }


def format_triage_human(report: TriageReport) -> str:
    lines: list[str] = []
    lines.append(f"Test Triage: {report.test_path}")
    lines.append("=" * 32)
    lines.append(f"  classification: {report.classification.upper()}")
    if report.recommended_owner:
        lines.append(f"  recommended owner: {report.recommended_owner}")
    if report.rationale:
        lines.append(f"  rationale: {report.rationale}")
    if report.evidence:
        lines.append("  evidence:")
        for ev in report.evidence:
            lines.append(f"    - {ev}")
    return "\n".join(lines)
