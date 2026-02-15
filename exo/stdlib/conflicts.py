"""Session-start intelligence: scope conflicts, unmerged work, ticket gating.

All checks are advisory — they inject warnings into the bootstrap prompt but
never block session start.  Called from ``AgentSessionManager.start()`` after
sibling session scanning.
"""

from __future__ import annotations

import fnmatch
import json
import os
import platform
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
    kind: str  # scope_conflict | unmerged_work | ticket_branch_mismatch | ticket_contention
    severity: str  # "warning" | "info"
    message: str  # Human-readable one-liner
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Scope overlap helpers
# ---------------------------------------------------------------------------


def _share_directory_prefix(pa: str, pb: str) -> bool:
    """True if two glob patterns share a non-trivial directory prefix."""
    parts_a = pa.replace("\\", "/").split("/")
    parts_b = pb.replace("\\", "/").split("/")
    # Need at least one concrete directory segment in common
    for a_seg, b_seg in zip(parts_a, parts_b, strict=False):
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
    for a_seg, b_seg in zip(parts_a, parts_b, strict=False):
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
    allow_a = scope_a.get("allow") or ["**"]
    allow_b = scope_b.get("allow") or ["**"]

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
            advisories.append(
                StartAdvisory(
                    kind="scope_conflict",
                    severity="warning",
                    message=(
                        f"{sib_actor} working on {sib_ticket_id}{branch_tag} — overlapping scope: {', '.join(patterns)}"
                    ),
                    detail={
                        "sibling_actor": sib_actor,
                        "sibling_ticket": sib_ticket_id,
                        "sibling_branch": sib_branch,
                        "overlapping_patterns": patterns,
                    },
                )
            )
    return advisories


def enforce_scope_partition(
    repo: Path,
    ticket_id: str,
    ticket_scope: dict[str, Any],
    siblings: list[dict[str, Any]],
) -> None:
    """Enforce scope disjointness with active siblings.

    Unlike ``detect_scope_conflicts()`` (advisory), this raises ``ExoError``
    on the first overlap found.  Used by concurrent session mode where scope
    partitioning replaces lock-based exclusion.
    """
    from exo.kernel import tickets as _tickets  # lazy to avoid circular
    from exo.kernel.errors import ExoError

    own_allow = (ticket_scope.get("allow") or ["**"])

    for sib in siblings:
        sib_ticket_id = str(sib.get("ticket_id", "")).strip()
        if not sib_ticket_id or sib_ticket_id == ticket_id:
            continue
        try:
            sib_ticket = _tickets.load_ticket(repo, sib_ticket_id)
        except Exception:
            continue
        sib_scope = sib_ticket.get("scope") or {}
        sib_allow = sib_scope.get("allow") or ["**"]

        # Both default ["**"] → can't partition without explicit scope
        if own_allow == ["**"] and sib_allow == ["**"]:
            raise ExoError(
                code="SCOPE_PARTITION_VIOLATION",
                message=(
                    f"concurrent sessions require explicit scope — both {ticket_id} and "
                    f"{sib_ticket_id} use default scope ['**']"
                ),
                details={
                    "ticket_id": ticket_id,
                    "sibling_ticket": sib_ticket_id,
                    "sibling_actor": sib.get("actor", "?"),
                },
                blocked=True,
            )

        overlaps, patterns = _scopes_overlap(ticket_scope, sib_scope)
        if overlaps:
            sib_actor = sib.get("actor", "?")
            raise ExoError(
                code="SCOPE_PARTITION_VIOLATION",
                message=(
                    f"scope overlap with {sib_actor} ({sib_ticket_id}): {', '.join(patterns)}"
                ),
                details={
                    "ticket_id": ticket_id,
                    "sibling_ticket": sib_ticket_id,
                    "sibling_actor": sib_actor,
                    "overlapping_patterns": patterns,
                },
                blocked=True,
            )


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
        advisories.append(
            StartAdvisory(
                kind="unmerged_work",
                severity="info",
                message=(
                    f"Unmerged work on branch {row_branch} "
                    f"(ticket={row_ticket_id or '?'}, actor={row_actor})" + (f" — {row_summary}" if row_summary else "")
                ),
                detail={
                    "branch": row_branch,
                    "ticket_id": row_ticket_id,
                    "actor": row_actor,
                    "overlapping_patterns": patterns,
                    "session_id": str(row.get("session_id", "")),
                },
            )
        )
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
            advisories.append(
                StartAdvisory(
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
                )
            )

    # --- b) Ticket contention: active sibling on same ticket ---
    for sib in siblings:
        sib_ticket = str(sib.get("ticket_id", "")).strip()
        if sib_ticket == ticket_id:
            sib_actor = sib.get("actor", "?")
            sib_branch = sib.get("git_branch", "")
            branch_tag = f" on {sib_branch}" if sib_branch else ""
            advisories.append(
                StartAdvisory(
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
                )
            )
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
        return [
            StartAdvisory(
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
            )
        ]
    else:
        # Simply behind
        return [
            StartAdvisory(
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
            )
        ]


# ---------------------------------------------------------------------------
# 5. Base branch divergence
# ---------------------------------------------------------------------------


def _base_divergence(repo: Path, current_branch: str, base_branch: str) -> tuple[int, int]:
    """Return (behind_base, ahead_of_base) commit counts.

    ``behind_base`` = commits on *base_branch* that *current_branch* doesn't have.
    ``ahead_of_base`` = commits on *current_branch* that *base_branch* doesn't have.
    Returns ``(0, 0)`` on any failure.
    """
    try:
        result = subprocess.run(
            ["git", "rev-list", "--left-right", "--count", f"{current_branch}...{base_branch}"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return 0, 0
        parts = result.stdout.strip().split()
        if len(parts) == 2:
            ahead = int(parts[0])  # left side = current_branch
            behind = int(parts[1])  # right side = base_branch
            return behind, ahead
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        pass
    return 0, 0


def detect_base_divergence(
    repo: Path,
    current_branch: str,
    base_branch: str,
    *,
    threshold: int = 15,
) -> list[StartAdvisory]:
    """Check if the current branch has fallen behind the base branch (e.g. main).

    Only fires when ``behind >= threshold`` to avoid noise on small lag.
    Skips when current_branch == base_branch (working directly on main).
    """
    if not current_branch or not base_branch:
        return []
    if current_branch == base_branch:
        return []

    behind, ahead = _base_divergence(repo, current_branch, base_branch)

    if behind < threshold:
        return []

    if ahead > 0:
        return [
            StartAdvisory(
                kind="base_divergence",
                severity="warning",
                message=(
                    f"{current_branch} is {behind} commit(s) behind {base_branch} "
                    f"(with {ahead} local commit(s)). "
                    f"Run `git pull --rebase origin {base_branch}` to reduce merge lag."
                ),
                detail={
                    "branch": current_branch,
                    "base_branch": base_branch,
                    "behind": behind,
                    "ahead": ahead,
                },
            )
        ]
    else:
        return [
            StartAdvisory(
                kind="base_divergence",
                severity="warning",
                message=(
                    f"{current_branch} is {behind} commit(s) behind {base_branch}. "
                    f"Run `git pull --rebase origin {base_branch}` before making changes."
                ),
                detail={
                    "branch": current_branch,
                    "base_branch": base_branch,
                    "behind": behind,
                    "ahead": 0,
                },
            )
        ]


# ---------------------------------------------------------------------------
# 6. Git workflow directives
# ---------------------------------------------------------------------------


def format_git_workflow(base_branch: str) -> str:
    """Static git workflow directives for the bootstrap prompt.

    Uses the actual base branch from the lock workspace — never hardcoded.
    """
    return "\n".join(
        [
            "## Git Workflow",
            f"- Before pushing, rebase on base branch: `git pull --rebase origin {base_branch}`",
            "- Pull latest before starting work: `git pull --rebase`",
            "- Keep commits atomic and branches short-lived",
            "",
        ]
    )


# ---------------------------------------------------------------------------
# 7. Machine context / resource awareness
# ---------------------------------------------------------------------------

RESOURCE_PROFILES = {"default", "light", "heavy"}


def _ram_info_darwin() -> tuple[float | None, float | None]:
    """Return (total_gb, available_gb) on macOS.  None on failure."""
    total_gb: float | None = None
    available_gb: float | None = None
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            total_gb = round(int(result.stdout.strip()) / (1024**3), 1)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        pass
    try:
        result = subprocess.run(
            ["vm_stat"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            lines = result.stdout.splitlines()
            page_size = 16384
            for line in lines:
                if "page size of" in line:
                    for token in line.split():
                        if token.isdigit():
                            page_size = int(token)
                            break
                    break
            free_pages = 0
            for line in lines:
                if "Pages free:" in line or "Pages inactive:" in line:
                    val = line.split(":")[1].strip().rstrip(".")
                    free_pages += int(val)
            available_gb = round(free_pages * page_size / (1024**3), 1)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        pass
    return total_gb, available_gb


def _ram_info_linux() -> tuple[float | None, float | None]:
    """Return (total_gb, available_gb) on Linux.  None on failure."""
    total_gb: float | None = None
    available_gb: float | None = None
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    total_gb = round(int(line.split()[1]) / (1024**2), 1)
                elif line.startswith("MemAvailable:"):
                    available_gb = round(int(line.split()[1]) / (1024**2), 1)
    except (OSError, ValueError):
        pass
    return total_gb, available_gb


def machine_snapshot() -> dict[str, Any]:
    """Lightweight one-shot read of machine resources.  All-stdlib, no psutil."""
    snap: dict[str, Any] = {
        "cpu_count": os.cpu_count() or 0,
        "load_avg_1m": None,
        "ram_total_gb": None,
        "ram_available_gb": None,
    }

    # Load average (POSIX — macOS + Linux)
    try:
        load = os.getloadavg()
        snap["load_avg_1m"] = round(load[0], 1)
    except (OSError, AttributeError):
        pass

    # RAM
    system = platform.system()
    if system == "Darwin":
        snap["ram_total_gb"], snap["ram_available_gb"] = _ram_info_darwin()
    elif system == "Linux":
        snap["ram_total_gb"], snap["ram_available_gb"] = _ram_info_linux()

    return snap


def format_machine_context(snap: dict[str, Any], resource_profile: str = "default") -> str:
    """Format the machine context bootstrap section.  Always included (small)."""
    cpu = snap.get("cpu_count") or "?"
    load = snap.get("load_avg_1m")
    total = snap.get("ram_total_gb")
    avail = snap.get("ram_available_gb")

    lines = ["## Machine Context"]
    lines.append(f"- cpu_cores: {cpu}")
    if load is not None:
        lines.append(f"- load_avg_1m: {load}")
    if total is not None and avail is not None:
        lines.append(f"- ram: {avail}GB available / {total}GB total")
    elif total is not None:
        lines.append(f"- ram_total: {total}GB")
    if resource_profile != "default":
        lines.append(f"- resource_profile: {resource_profile}")
    if resource_profile == "heavy":
        lines.append("- DIRECTIVE: This ticket is resource-heavy. Serialize CPU/memory-intensive operations.")
    lines.append("")
    return "\n".join(lines)


def detect_machine_load(
    snap: dict[str, Any],
    sibling_count: int = 0,
    resource_profile: str = "default",
) -> list[StartAdvisory]:
    """Emit advisory when the machine is under pressure."""
    advisories: list[StartAdvisory] = []
    cpu = snap.get("cpu_count") or 0
    load = snap.get("load_avg_1m")
    avail_gb = snap.get("ram_available_gb")

    # High CPU load: load average > 70% of cores
    load_high = False
    if cpu and load is not None and load > cpu * 0.7:
        load_high = True

    # Low RAM: less than 2GB available
    ram_low = False
    if avail_gb is not None and avail_gb < 2.0:
        ram_low = True

    if load_high or ram_low:
        parts: list[str] = []
        if load_high:
            parts.append(f"CPU load {load}/{cpu} cores")
        if ram_low:
            parts.append(f"RAM {avail_gb}GB available")
        msg = f"System under load ({', '.join(parts)}). Prefer sequential execution."
        if sibling_count > 0:
            msg += f" {sibling_count} sibling session(s) also running."
        advisories.append(
            StartAdvisory(
                kind="machine_load",
                severity="warning",
                message=msg,
                detail={
                    "cpu_count": cpu,
                    "load_avg_1m": load,
                    "ram_available_gb": avail_gb,
                    "sibling_count": sibling_count,
                    "load_high": load_high,
                    "ram_low": ram_low,
                },
            )
        )
    elif resource_profile == "heavy":
        advisories.append(
            StartAdvisory(
                kind="machine_load",
                severity="info",
                message=(
                    "Ticket resource_profile is 'heavy'. "
                    "Serialize CPU/memory-intensive operations even if system load is normal."
                ),
                detail={
                    "resource_profile": resource_profile,
                    "cpu_count": cpu,
                    "load_avg_1m": load,
                    "ram_available_gb": avail_gb,
                },
            )
        )

    return advisories


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
