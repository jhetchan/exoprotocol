"""Session-start intelligence: scope conflicts, unmerged work, ticket gating.

All checks are advisory — they inject warnings into the bootstrap prompt but
never block session start.  Called from ``AgentSessionManager.start()`` after
sibling session scanning.
"""

from __future__ import annotations

import fnmatch
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SESSION_INDEX_PATH = ".exo/memory/sessions/index.jsonl"


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class StartAdvisory:
    kind: str          # scope_conflict | unmerged_work | ticket_branch_mismatch | ticket_contention
    severity: str      # "warning" | "info"
    message: str       # Human-readable one-liner
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Scope overlap helpers
# ---------------------------------------------------------------------------

def _share_directory_prefix(pa: str, pb: str) -> bool:
    """True if two glob patterns share a non-trivial directory prefix."""
    parts_a = pa.replace("\\", "/").split("/")
    parts_b = pb.replace("\\", "/").split("/")
    # Need at least one concrete directory segment in common
    for a_seg, b_seg in zip(parts_a, parts_b):
        if a_seg == "**" or b_seg == "**":
            return True  # wildcard after shared prefix → overlap
        if a_seg == b_seg:
            return True  # concrete shared segment
        # Check fnmatch at segment level
        if fnmatch.fnmatch(a_seg, b_seg) or fnmatch.fnmatch(b_seg, a_seg):
            return True
        break
    return False


def _common_prefix(pa: str, pb: str) -> str:
    """Return the longest common directory prefix of two patterns."""
    parts_a = pa.replace("\\", "/").split("/")
    parts_b = pb.replace("\\", "/").split("/")
    common: list[str] = []
    for a_seg, b_seg in zip(parts_a, parts_b):
        if a_seg == b_seg:
            common.append(a_seg)
        else:
            break
    if not common:
        return pa if len(pa) <= len(pb) else pb
    return "/".join(common) + "/**"


def _scopes_overlap(
    scope_a: dict[str, Any],
    scope_b: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Check if two ticket scopes could affect overlapping files.

    Returns ``(overlaps, overlapping_patterns)``.

    Design: both ``["**"]`` (the default) → no warning.  This avoids spam when
    nobody has customised scope.  But if one ticket has specific scope, the
    warning fires — that's the point of setting scope.
    """
    allow_a = (scope_a.get("allow") or ["**"])
    allow_b = (scope_b.get("allow") or ["**"])

    # Both default → skip (too noisy)
    if allow_a == ["**"] and allow_b == ["**"]:
        return False, []

    # One specific, one default → specific patterns are the overlap region
    if allow_a == ["**"]:
        return True, list(allow_b)
    if allow_b == ["**"]:
        return True, list(allow_a)

    # Both specific → cross-check with fnmatch
    overlapping: list[str] = []
    for pa in allow_a:
        for pb in allow_b:
            if fnmatch.fnmatch(pa, pb) or fnmatch.fnmatch(pb, pa):
                overlapping.append(pa if len(pa) <= len(pb) else pb)
            elif _share_directory_prefix(pa, pb):
                prefix = _common_prefix(pa, pb)
                overlapping.append(prefix)

    # Dedupe preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for p in overlapping:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return bool(deduped), deduped


# ---------------------------------------------------------------------------
# 1. Scope conflict detection
# ---------------------------------------------------------------------------

def detect_scope_conflicts(
    repo: Path,
    ticket_id: str,
    ticket_scope: dict[str, Any],
    siblings: list[dict[str, Any]],
) -> list[StartAdvisory]:
    """Check if any active sibling session has overlapping scope."""
    from exo.kernel import tickets  # lazy to avoid circular

    advisories: list[StartAdvisory] = []
    for sib in siblings:
        sib_ticket_id = str(sib.get("ticket_id", "")).strip()
        if not sib_ticket_id:
            continue
        try:
            sib_ticket = tickets.load_ticket(repo, sib_ticket_id)
        except Exception:
            continue  # ticket missing or corrupt — skip
        sib_scope = sib_ticket.get("scope") or {}
        overlaps, patterns = _scopes_overlap(ticket_scope, sib_scope)
        if overlaps:
            sib_actor = sib.get("actor", "?")
            sib_branch = sib.get("git_branch", "")
            branch_tag = f" on {sib_branch}" if sib_branch else ""
            advisories.append(StartAdvisory(
                kind="scope_conflict",
                severity="warning",
                message=(
                    f"{sib_actor} working on {sib_ticket_id}{branch_tag} — "
                    f"overlapping scope: {', '.join(patterns)}"
                ),
                detail={
                    "sibling_actor": sib_actor,
                    "sibling_ticket": sib_ticket_id,
                    "sibling_branch": sib_branch,
                    "overlapping_patterns": patterns,
                },
            ))
    return advisories


# ---------------------------------------------------------------------------
# 2. Unmerged work advisory
# ---------------------------------------------------------------------------

def _merged_branches(repo: Path, branch: str) -> set[str]:
    """Return set of branch names that have been merged into *branch*."""
    try:
        result = subprocess.run(
            ["git", "branch", "--merged", branch],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return set()
        names: set[str] = set()
        for line in result.stdout.splitlines():
            name = line.strip().lstrip("* ")
            if name:
                names.add(name)
        return names
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return set()


def _read_session_index(repo: Path) -> list[dict[str, Any]]:
    """Read all rows from the session index JSONL."""
    path = repo / SESSION_INDEX_PATH
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
                if isinstance(item, dict):
                    rows.append(item)
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return rows


def detect_unmerged_work(
    repo: Path,
    current_branch: str,
    ticket_id: str,
    ticket_scope: dict[str, Any],
    *,
    max_age_days: int = 14,
) -> list[StartAdvisory]:
    """Find completed sessions on unmerged branches with overlapping scope."""
    if not current_branch:
        return []

    rows = _read_session_index(repo)
    if not rows:
        return []

    merged = _merged_branches(repo, current_branch)
    now = datetime.now(timezone.utc)
    advisories: list[StartAdvisory] = []
    seen_branches: set[str] = set()  # dedupe per branch

    for row in reversed(rows):  # most recent first
        if str(row.get("mode", "work")).strip() == "audit":
            continue
        row_branch = str(row.get("git_branch", "")).strip()
        if not row_branch or row_branch == current_branch:
            continue
        if row_branch in merged:
            continue
        if row_branch in seen_branches:
            continue  # already reported this branch

        # Age check
        finished_at = str(row.get("finished_at", "")).strip()
        if finished_at:
            try:
                finished = datetime.fromisoformat(finished_at)
                if not finished.tzinfo:
                    finished = finished.replace(tzinfo=timezone.utc)
                age_days = (now - finished).total_seconds() / 86400.0
                if age_days > max_age_days:
                    continue
            except (TypeError, ValueError):
                pass

        # Scope overlap check
        row_ticket_id = str(row.get("ticket_id", "")).strip()
        if row_ticket_id:
            try:
                from exo.kernel import tickets
                row_ticket = tickets.load_ticket(repo, row_ticket_id)
                row_scope = row_ticket.get("scope") or {}
                overlaps, patterns = _scopes_overlap(ticket_scope, row_scope)
                if not overlaps:
                    continue
            except Exception:
                # Can't load ticket — still report if branch is unmerged
                patterns = ["(unknown scope)"]
        else:
            patterns = ["(unknown scope)"]

        seen_branches.add(row_branch)
        row_actor = str(row.get("actor", "?"))
        row_summary = str(row.get("summary", "")).strip()[:80]
        advisories.append(StartAdvisory(
            kind="unmerged_work",
            severity="info",
            message=(
                f"Unmerged work on branch {row_branch} "
                f"(ticket={row_ticket_id or '?'}, actor={row_actor})"
                + (f" — {row_summary}" if row_summary else "")
            ),
            detail={
                "branch": row_branch,
                "ticket_id": row_ticket_id,
                "actor": row_actor,
                "overlapping_patterns": patterns,
                "session_id": str(row.get("session_id", "")),
            },
        ))
    return advisories


# ---------------------------------------------------------------------------
# 3. Ticket gating (branch mismatch + contention)
# ---------------------------------------------------------------------------

def detect_ticket_issues(
    repo: Path,
    ticket_id: str,
    current_branch: str,
    siblings: list[dict[str, Any]],
) -> list[StartAdvisory]:
    """Detect branch mismatch for ticket and contention with siblings."""
    advisories: list[StartAdvisory] = []

    if not ticket_id:
        return advisories

    # --- a) Branch mismatch: prior sessions on same ticket, different branch ---
    if current_branch:
        rows = _read_session_index(repo)
        latest_branch: str = ""
        for row in reversed(rows):
            if str(row.get("ticket_id", "")).strip() == ticket_id:
                if str(row.get("mode", "work")).strip() == "audit":
                    continue
                latest_branch = str(row.get("git_branch", "")).strip()
                if latest_branch:
                    break

        if latest_branch and latest_branch != current_branch:
            advisories.append(StartAdvisory(
                kind="ticket_branch_mismatch",
                severity="warning",
                message=(
                    f"{ticket_id} was previously worked on branch "
                    f"{latest_branch}, but you're on {current_branch}. "
                    f"Ensure prior work has been merged or rebased."
                ),
                detail={
                    "ticket_id": ticket_id,
                    "previous_branch": latest_branch,
                    "current_branch": current_branch,
                },
            ))

    # --- b) Ticket contention: active sibling on same ticket ---
    for sib in siblings:
        sib_ticket = str(sib.get("ticket_id", "")).strip()
        if sib_ticket == ticket_id:
            sib_actor = sib.get("actor", "?")
            sib_branch = sib.get("git_branch", "")
            branch_tag = f" on {sib_branch}" if sib_branch else ""
            advisories.append(StartAdvisory(
                kind="ticket_contention",
                severity="warning",
                message=(
                    f"{sib_actor} is also actively working on "
                    f"{ticket_id}{branch_tag}. "
                    f"Coordinate to avoid conflicting changes."
                ),
                detail={
                    "sibling_actor": sib_actor,
                    "sibling_branch": sib_branch,
                    "ticket_id": ticket_id,
                    "sibling_session_id": sib.get("session_id", ""),
                },
            ))
    return advisories


# ---------------------------------------------------------------------------
# 4. Stale branch detection (branch freshness)
# ---------------------------------------------------------------------------

def _upstream_status(repo: Path, branch: str) -> tuple[int, int]:
    """Return (behind, ahead) commit counts relative to upstream tracking branch.

    Returns ``(0, 0)`` on any failure (no remote, no tracking branch, etc.).
    """
    try:
        result = subprocess.run(
            ["git", "rev-list", "--left-right", "--count", f"{branch}...{branch}@{{u}}"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return 0, 0
        parts = result.stdout.strip().split()
        if len(parts) == 2:
            ahead = int(parts[0])
            behind = int(parts[1])
            return behind, ahead
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        pass
    return 0, 0


def detect_stale_branch(
    repo: Path,
    current_branch: str,
) -> list[StartAdvisory]:
    """Check if the current branch is behind its upstream tracking branch."""
    if not current_branch:
        return []

    behind, ahead = _upstream_status(repo, current_branch)

    if behind == 0:
        return []

    if ahead > 0:
        # Diverged: local commits AND upstream commits
        return [StartAdvisory(
            kind="stale_branch",
            severity="warning",
            message=(
                f"{current_branch} has diverged: {ahead} local commit(s), "
                f"{behind} upstream commit(s). "
                f"Rebase with caution — conflicts may occur."
            ),
            detail={
                "branch": current_branch,
                "behind": behind,
                "ahead": ahead,
                "diverged": True,
            },
        )]
    else:
        # Simply behind
        return [StartAdvisory(
            kind="stale_branch",
            severity="warning",
            message=(
                f"{current_branch} is {behind} commit(s) behind upstream. "
                f"Run `git pull --rebase` before making changes."
            ),
            detail={
                "branch": current_branch,
                "behind": behind,
                "ahead": 0,
                "diverged": False,
            },
        )]


# ---------------------------------------------------------------------------
# Formatting / serialisation
# ---------------------------------------------------------------------------

def format_advisories(advisories: list[StartAdvisory]) -> str:
    """Markdown section for bootstrap injection.  Empty string if no advisories."""
    if not advisories:
        return ""

    # Sort: warnings first, then info
    severity_order = {"warning": 0, "info": 1}
    sorted_adv = sorted(advisories, key=lambda a: severity_order.get(a.severity, 2))

    lines = ["## Start Advisories"]
    for adv in sorted_adv:
        prefix = "WARNING" if adv.severity == "warning" else "INFO"
        lines.append(f"- [{prefix}] {adv.message}")
    lines.append("")
    return "\n".join(lines)


def advisories_to_dicts(advisories: list[StartAdvisory]) -> list[dict[str, Any]]:
    """Serialise advisories for return dict / session payload."""
    return [
        {
            "kind": adv.kind,
            "severity": adv.severity,
            "message": adv.message,
            "detail": adv.detail,
        }
        for adv in advisories
    ]
