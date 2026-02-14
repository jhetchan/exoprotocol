"""PR governance check: verify that all commits in a PR range are governed by ExoProtocol sessions.

Designed to run in CI or be consumed by a review agent. Returns structured
data about governance coverage, session compliance, and drift scores.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from exo.kernel.errors import ExoError
from exo.kernel.governance import verify_integrity
from exo.kernel.utils import any_pattern_matches, now_iso

SESSION_INDEX_PATH = Path(".exo/memory/sessions/index.jsonl")


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    timestamp: str  # ISO 8601
    author: str
    message: str


@dataclass(frozen=True)
class CommitVerdict:
    sha: str
    governed: bool
    session_id: str | None = None
    ticket_id: str | None = None
    actor: str | None = None
    in_scope: bool = True


@dataclass(frozen=True)
class SessionVerdict:
    session_id: str
    ticket_id: str
    actor: str
    vendor: str
    model: str
    mode: str
    verify: str
    drift_score: float | None
    started_at: str
    finished_at: str
    commit_count: int


@dataclass
class PRCheckReport:
    base_ref: str
    head_ref: str
    total_commits: int
    governed_commits: int
    ungoverned_commits: int
    sessions: list[SessionVerdict]
    commits: list[CommitVerdict]
    ungoverned_shas: list[str]
    governance_intact: bool
    governance_hash: str
    changed_files: list[str]
    scope_violations: list[str]  # files not covered by any session scope
    verdict: str  # "pass", "fail", "warn"
    reasons: list[str]  # reasons for fail/warn
    checked_at: str


def _run_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _list_commits(repo: Path, base_ref: str, head_ref: str) -> list[CommitInfo]:
    """List commits in base_ref..head_ref range."""
    proc = _run_git(
        repo,
        [
            "log",
            "--format=%H|%aI|%an|%s",
            f"{base_ref}..{head_ref}",
        ],
    )
    if proc.returncode != 0:
        return []

    commits: list[CommitInfo] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue
        commits.append(
            CommitInfo(
                sha=parts[0],
                timestamp=parts[1],
                author=parts[2],
                message=parts[3],
            )
        )
    return commits


def _changed_files_in_range(repo: Path, base_ref: str, head_ref: str) -> list[str]:
    """Get all files changed between base_ref and head_ref."""
    proc = _run_git(repo, ["diff", "--name-only", base_ref, head_ref])
    if proc.returncode != 0:
        return []
    return sorted(f.strip() for f in proc.stdout.splitlines() if f.strip())


def _load_session_index(repo: Path) -> list[dict[str, Any]]:
    """Load all entries from the session index."""
    path = repo / SESSION_INDEX_PATH
    if not path.exists():
        return []

    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                entries.append(item)
    return entries


def _parse_ts(iso_str: str) -> datetime:
    """Parse ISO 8601 timestamp.

    Handles the trailing ``Z`` that git ``%aI`` may emit on some platforms
    (Python < 3.11 ``fromisoformat`` rejects ``Z``).  Also normalises
    timezone-naive strings to UTC so comparisons never raise TypeError.
    """
    if iso_str.endswith("Z"):
        iso_str = iso_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _match_commits_to_sessions(
    commits: list[CommitInfo],
    sessions: list[dict[str, Any]],
) -> dict[str, dict[str, Any] | None]:
    """Map each commit SHA to the session that covers it (by timestamp window).

    Returns dict of {sha: session_entry_or_None}.
    """
    # Parse session time windows
    windows: list[tuple[datetime, datetime, dict[str, Any]]] = []
    for entry in sessions:
        started = entry.get("started_at", "")
        finished = entry.get("finished_at", "")
        if not started or not finished:
            continue
        try:
            t_start = _parse_ts(started)
            t_finish = _parse_ts(finished)
            windows.append((t_start, t_finish, entry))
        except (ValueError, TypeError):
            continue

    result: dict[str, dict[str, Any] | None] = {}
    for commit in commits:
        try:
            t_commit = _parse_ts(commit.timestamp)
        except (ValueError, TypeError):
            result[commit.sha] = None
            continue

        matched: dict[str, Any] | None = None
        for t_start, t_finish, entry in windows:
            if t_start <= t_commit <= t_finish:
                matched = entry
                break  # first match wins (sessions are chronological)

        result[commit.sha] = matched

    return result


def _check_scope_coverage(
    changed_files: list[str],
    sessions: list[dict[str, Any]],
    repo: Path,
) -> list[str]:
    """Find files not covered by any session's ticket scope.

    Loads ticket for each session and checks scope.allow / scope.deny.
    Returns list of uncovered file paths.
    """
    if not changed_files:
        return []

    # Collect all scope patterns from sessions
    from exo.kernel.tickets import load_ticket

    covered: set[str] = set()
    for entry in sessions:
        ticket_id = entry.get("ticket_id", "")
        if not ticket_id:
            continue
        try:
            ticket = load_ticket(repo, ticket_id)
        except Exception:
            continue

        scope = ticket.get("scope") or {}
        allow = scope.get("allow", ["**"])
        deny = scope.get("deny", [])

        for rel_path in changed_files:
            if rel_path in covered:
                continue
            full_path = repo / rel_path
            allowed = any_pattern_matches(full_path, allow, repo) if allow else True
            denied = any_pattern_matches(full_path, deny, repo) if deny else False
            if allowed and not denied:
                covered.add(rel_path)

    return sorted(set(changed_files) - covered)


def pr_check(
    repo: Path,
    *,
    base_ref: str = "main",
    head_ref: str = "HEAD",
    drift_threshold: float = 0.7,
) -> PRCheckReport:
    """Check governance compliance for all commits in a PR range.

    Args:
        repo: Repository root path.
        base_ref: Base branch/ref (e.g., "main").
        head_ref: Head ref (e.g., "HEAD" or branch name).
        drift_threshold: Drift score above which a session is flagged.

    Returns:
        PRCheckReport with structured governance compliance data.
    """
    repo = Path(repo).resolve()
    reasons: list[str] = []

    # 1. Governance integrity
    governance_intact = True
    governance_hash = ""
    try:
        integrity = verify_integrity(repo)
        governance_hash = str(integrity.get("source_hash", ""))
    except ExoError:
        governance_intact = False
        reasons.append("Governance integrity check failed — constitution drift or missing lock")

    # 2. List commits in range
    commits = _list_commits(repo, base_ref, head_ref)
    changed_files = _changed_files_in_range(repo, base_ref, head_ref)

    if not commits:
        return PRCheckReport(
            base_ref=base_ref,
            head_ref=head_ref,
            total_commits=0,
            governed_commits=0,
            ungoverned_commits=0,
            sessions=[],
            commits=[],
            ungoverned_shas=[],
            governance_intact=governance_intact,
            governance_hash=governance_hash,
            changed_files=changed_files,
            scope_violations=[],
            verdict="pass" if governance_intact else "fail",
            reasons=reasons or ["No commits in range"],
            checked_at=now_iso(),
        )

    # 3. Load session index and match commits
    all_sessions = _load_session_index(repo)
    commit_map = _match_commits_to_sessions(commits, all_sessions)

    # 4. Build commit verdicts and collect matched sessions
    commit_verdicts: list[CommitVerdict] = []
    ungoverned_shas: list[str] = []
    matched_session_ids: set[str] = set()
    matched_sessions: list[dict[str, Any]] = []

    for commit in commits:
        session = commit_map.get(commit.sha)
        if session:
            sid = session.get("session_id", "")
            commit_verdicts.append(
                CommitVerdict(
                    sha=commit.sha,
                    governed=True,
                    session_id=sid,
                    ticket_id=session.get("ticket_id"),
                    actor=session.get("actor"),
                )
            )
            if sid not in matched_session_ids:
                matched_session_ids.add(sid)
                matched_sessions.append(session)
        else:
            commit_verdicts.append(
                CommitVerdict(
                    sha=commit.sha,
                    governed=False,
                )
            )
            ungoverned_shas.append(commit.sha)

    # 5. Build session verdicts
    session_verdicts: list[SessionVerdict] = []
    session_commit_counts: dict[str, int] = {}
    for cv in commit_verdicts:
        if cv.session_id:
            session_commit_counts[cv.session_id] = session_commit_counts.get(cv.session_id, 0) + 1

    for entry in matched_sessions:
        sid = entry.get("session_id", "")
        drift = entry.get("drift_score")
        verify = entry.get("verify", "")

        session_verdicts.append(
            SessionVerdict(
                session_id=sid,
                ticket_id=entry.get("ticket_id", ""),
                actor=entry.get("actor", ""),
                vendor=entry.get("vendor", ""),
                model=entry.get("model", ""),
                mode=entry.get("mode", "work"),
                verify=verify,
                drift_score=drift,
                started_at=entry.get("started_at", ""),
                finished_at=entry.get("finished_at", ""),
                commit_count=session_commit_counts.get(sid, 0),
            )
        )

        # Flag sessions with issues
        if verify == "failed":
            reasons.append(f"Session {sid}: verification failed")
        elif verify == "bypassed":
            reasons.append(f"Session {sid}: verification bypassed (break-glass)")
        if drift is not None and drift > drift_threshold:
            reasons.append(f"Session {sid}: drift score {drift:.2f} exceeds threshold {drift_threshold}")

    # 6. Check scope coverage
    scope_violations = _check_scope_coverage(changed_files, matched_sessions, repo)
    if scope_violations:
        reasons.append(
            f"{len(scope_violations)} file(s) not covered by any session scope: "
            + ", ".join(scope_violations[:5])
            + ("..." if len(scope_violations) > 5 else "")
        )

    # 7. Ungoverned commits
    if ungoverned_shas:
        reasons.append(f"{len(ungoverned_shas)} commit(s) made outside any governed session")

    # 8. Determine verdict
    governed = len(commits) - len(ungoverned_shas)
    has_ungoverned = len(ungoverned_shas) > 0
    has_failed_verify = any(sv.verify == "failed" for sv in session_verdicts)
    has_high_drift = any(sv.drift_score is not None and sv.drift_score > drift_threshold for sv in session_verdicts)

    if not governance_intact or has_failed_verify or has_ungoverned:
        verdict = "fail"
    elif has_high_drift or scope_violations:
        verdict = "warn"
    else:
        verdict = "pass"

    return PRCheckReport(
        base_ref=base_ref,
        head_ref=head_ref,
        total_commits=len(commits),
        governed_commits=governed,
        ungoverned_commits=len(ungoverned_shas),
        sessions=session_verdicts,
        commits=commit_verdicts,
        ungoverned_shas=ungoverned_shas,
        governance_intact=governance_intact,
        governance_hash=governance_hash,
        changed_files=changed_files,
        scope_violations=scope_violations,
        verdict=verdict,
        reasons=reasons,
        checked_at=now_iso(),
    )


def pr_check_to_dict(report: PRCheckReport) -> dict[str, Any]:
    """Convert PRCheckReport to a plain dict for serialization."""
    return asdict(report)


def format_pr_check_human(report: PRCheckReport) -> str:
    """Format PR check report as human-readable text."""
    icon = {"pass": "PASS", "fail": "FAIL", "warn": "WARN"}[report.verdict]
    lines = [
        f"PR Governance Check: {icon}",
        f"  range: {report.base_ref}..{report.head_ref}",
        f"  commits: {report.total_commits} total, {report.governed_commits} governed, {report.ungoverned_commits} ungoverned",
        f"  governance: {'intact' if report.governance_intact else 'DRIFT DETECTED'}",
        f"  changed files: {len(report.changed_files)}",
    ]

    if report.sessions:
        lines.append("  sessions:")
        for sv in report.sessions:
            drift_str = f", drift={sv.drift_score:.2f}" if sv.drift_score is not None else ""
            lines.append(
                f"    - {sv.session_id} ({sv.actor}@{sv.vendor}/{sv.model}) "
                f"ticket={sv.ticket_id} verify={sv.verify}{drift_str} "
                f"commits={sv.commit_count}"
            )

    if report.scope_violations:
        lines.append(f"  scope violations: {report.scope_violations}")

    if report.reasons:
        lines.append("  reasons:")
        for r in report.reasons:
            lines.append(f"    - {r}")

    return "\n".join(lines)
