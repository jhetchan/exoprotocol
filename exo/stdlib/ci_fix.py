"""CI failure detection and auto-fix for ExoProtocol.

Fetches failed CI run logs via the gh CLI, parses errors into structured
entries, suggests (and optionally applies) fixes, and can commit + push
the result to retrigger CI.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from exo.kernel.errors import ExoError

# ── gh CLI helpers ──────────────────────────────────────────────


def _run_gh(args: list[str], cwd: Path) -> tuple[int, str, str]:
    """Run a gh CLI command and return (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=60,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError as exc:
        raise ExoError(
            code="GH_NOT_FOUND",
            message="gh CLI not found — install from https://cli.github.com",
            blocked=True,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ExoError(
            code="GH_TIMEOUT",
            message="gh command timed out after 60s",
            blocked=True,
        ) from exc


# ── Error parsing ──────────────────────────────────────────────


def parse_errors(logs: str) -> list[dict[str, Any]]:
    """Parse CI logs into structured error entries."""
    errors: list[dict[str, Any]] = []

    # ruff format: "N files would be reformatted"
    fmt_match = re.search(r"(\d+) files? would be reformatted", logs)
    if fmt_match:
        file_lines = re.findall(r"Would reformat: (.+)", logs)
        errors.append(
            {
                "tool": "ruff-format",
                "severity": "error",
                "message": f"{fmt_match.group(1)} files would be reformatted",
                "files": file_lines,
                "auto_fixable": True,
            }
        )

    # ruff lint: path.py:line:col: CODE message
    lint_hits = re.findall(r"([\w/._-]+\.py):(\d+):(\d+): ([A-Z]+\d+) (.+)", logs)
    for filepath, line, col, code, msg in lint_hits:
        errors.append(
            {
                "tool": "ruff-lint",
                "severity": "error",
                "file": filepath.strip(),
                "line": int(line),
                "col": int(col),
                "code": code,
                "message": msg.strip(),
                "auto_fixable": False,
            }
        )

    # pytest failures: FAILED path::Class::method
    test_fails = re.findall(r"FAILED ([\w/._-]+\.py::\S+)", logs)
    for test in test_fails:
        errors.append(
            {
                "tool": "pytest",
                "severity": "error",
                "test": test,
                "message": f"Test failed: {test}",
                "auto_fixable": False,
            }
        )

    # Python compile / syntax errors
    for err_type in ("SyntaxError", "IndentationError", "TabError"):
        for m in re.finditer(rf"({err_type}): (.+)", logs):
            errors.append(
                {
                    "tool": "python-compile",
                    "severity": "error",
                    "error_type": m.group(1),
                    "message": f"{m.group(1)}: {m.group(2)}",
                    "auto_fixable": False,
                }
            )

    if not errors:
        errors.append(
            {
                "tool": "unknown",
                "severity": "error",
                "message": "CI failed — check logs for details",
                "auto_fixable": False,
            }
        )

    return errors


# ── Fix suggestions ────────────────────────────────────────────


def suggest_fixes(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Generate fix suggestions from parsed errors."""
    fixes: list[dict[str, Any]] = []
    seen: set[str] = set()

    for err in errors:
        tool = err["tool"]

        if tool == "ruff-format" and tool not in seen:
            seen.add(tool)
            dirs: set[str] = set()
            for f in err.get("files", []):
                parts = Path(f).parts
                if parts:
                    dirs.add(parts[0])
            target = " ".join(sorted(dirs)) if dirs else "."
            fixes.append(
                {
                    "tool": "ruff-format",
                    "description": f"Reformat {err['message'].split()[0]} files",
                    "command": f"ruff format {target}",
                    "auto_fixable": True,
                }
            )

        elif tool == "ruff-lint":
            fixes.append(
                {
                    "tool": "ruff-lint",
                    "description": (
                        f"Fix {err.get('code', '?')} in {err.get('file', '?')}:{err.get('line', '?')}: {err['message']}"
                    ),
                    "file": err.get("file", ""),
                    "line": err.get("line", 0),
                    "auto_fixable": False,
                }
            )

        elif tool == "pytest":
            fixes.append(
                {
                    "tool": "pytest",
                    "description": f"Fix failing test: {err['test']}",
                    "test": err.get("test", ""),
                    "auto_fixable": False,
                }
            )

        elif tool == "python-compile":
            fixes.append(
                {
                    "tool": "python-compile",
                    "description": err["message"],
                    "auto_fixable": False,
                }
            )

    return fixes


# ── Fetch CI failure ───────────────────────────────────────────


def fetch_ci_failure(
    repo: Path | str = ".",
    run_id: str = "",
) -> dict[str, Any]:
    """Fetch the latest failed CI run and produce a structured report."""
    repo = Path(repo).resolve()

    if not run_id:
        rc, out, err = _run_gh(
            [
                "run",
                "list",
                "--limit",
                "1",
                "--status",
                "failure",
                "--json",
                "databaseId,conclusion,name,headBranch,createdAt,url",
            ],
            repo,
        )
        if rc != 0:
            raise ExoError(
                code="GH_RUN_LIST_FAILED",
                message=f"gh run list failed: {err.strip()}",
                blocked=True,
            )
        runs = json.loads(out) if out.strip() else []
        if not runs:
            return {"status": "no_failures", "message": "No failed CI runs found"}
        run_info = runs[0]
        run_id = str(run_info["databaseId"])
    else:
        rc, out, err = _run_gh(
            [
                "run",
                "view",
                run_id,
                "--json",
                "databaseId,conclusion,name,headBranch,createdAt,url",
            ],
            repo,
        )
        run_info = json.loads(out) if rc == 0 and out.strip() else {"databaseId": run_id}

    rc, logs, err = _run_gh(["run", "view", run_id, "--log-failed"], repo)
    if rc != 0:
        raise ExoError(
            code="GH_LOG_FAILED",
            message=f"Failed to fetch logs for run {run_id}: {err.strip()}",
            blocked=True,
        )

    errors = parse_errors(logs)
    fixes = suggest_fixes(errors)

    max_log_len = 10_000
    truncated = len(logs) > max_log_len

    return {
        "status": "failure",
        "run_id": run_id,
        "run_info": run_info,
        "errors": errors,
        "fixes": fixes,
        "fix_commands": [f["command"] for f in fixes if f.get("command")],
        "logs": logs[:max_log_len] + ("\n... (truncated)" if truncated else ""),
        "logs_truncated": truncated,
    }


# ── Apply fixes ────────────────────────────────────────────────


def apply_fixes(
    repo: Path | str = ".",
    report: dict[str, Any] | None = None,
    run_id: str = "",
) -> dict[str, Any]:
    """Apply auto-fixable CI errors."""
    repo = Path(repo).resolve()

    if report is None:
        report = fetch_ci_failure(repo, run_id=run_id)

    if report.get("status") == "no_failures":
        return report

    applied: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []

    for fix in report.get("fixes", []):
        if fix.get("auto_fixable") and fix.get("command"):
            try:
                proc = subprocess.run(
                    fix["command"],
                    shell=True,
                    capture_output=True,
                    text=True,
                    cwd=str(repo),
                    timeout=120,
                )
                applied.append(
                    {
                        "command": fix["command"],
                        "description": fix["description"],
                        "success": proc.returncode == 0,
                        "output": (proc.stdout + proc.stderr).strip()[:2000],
                    }
                )
            except subprocess.TimeoutExpired:
                applied.append(
                    {
                        "command": fix["command"],
                        "description": fix["description"],
                        "success": False,
                        "output": "Command timed out after 120s",
                    }
                )
        else:
            remaining.append(fix)

    all_ok = bool(applied) and all(a["success"] for a in applied)
    return {
        "status": "fixed" if all_ok else "partial",
        "applied": applied,
        "remaining": remaining,
        "run_id": report.get("run_id", ""),
    }


# ── Commit + push ──────────────────────────────────────────────


def commit_and_push(
    repo: Path | str = ".",
    run_id: str = "",
    message: str = "",
) -> dict[str, Any]:
    """Stage all changes, commit with a CI-fix message, and push."""
    repo = Path(repo).resolve()

    if not message:
        message = f"fix: auto-fix CI failure (run {run_id})" if run_id else "fix: auto-fix CI failure"

    # Stage
    proc = subprocess.run(
        ["git", "add", "-A"],
        capture_output=True,
        text=True,
        cwd=str(repo),
    )
    if proc.returncode != 0:
        return {"pushed": False, "error": f"git add failed: {proc.stderr.strip()}"}

    # Check if there's anything to commit
    proc = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        capture_output=True,
        text=True,
        cwd=str(repo),
    )
    if proc.returncode == 0:
        return {"pushed": False, "error": "Nothing to commit after applying fixes"}

    # Commit
    proc = subprocess.run(
        ["git", "commit", "-m", message],
        capture_output=True,
        text=True,
        cwd=str(repo),
    )
    if proc.returncode != 0:
        return {"pushed": False, "error": f"git commit failed: {proc.stderr.strip()}"}

    commit_sha = ""
    sha_proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        cwd=str(repo),
    )
    if sha_proc.returncode == 0:
        commit_sha = sha_proc.stdout.strip()

    # Push
    proc = subprocess.run(
        ["git", "push"],
        capture_output=True,
        text=True,
        cwd=str(repo),
    )
    if proc.returncode != 0:
        return {
            "pushed": False,
            "committed": True,
            "commit_sha": commit_sha,
            "error": f"git push failed: {proc.stderr.strip()}",
        }

    return {
        "pushed": True,
        "committed": True,
        "commit_sha": commit_sha,
        "message": message,
    }


# ── Human-readable output ─────────────────────────────────────


def format_ci_fix_human(report: dict[str, Any]) -> str:
    """Format CI failure report for human-readable CLI output."""
    lines: list[str] = []

    status = report.get("status", "unknown")
    if status == "no_failures":
        return "No failed CI runs found."

    run_info = report.get("run_info", {})
    lines.append(f"CI Failure: run {report.get('run_id', '?')}")
    if run_info.get("name"):
        lines.append(f"  workflow: {run_info['name']}")
    if run_info.get("headBranch"):
        lines.append(f"  branch: {run_info['headBranch']}")
    lines.append("")

    errors = report.get("errors", [])
    if errors:
        lines.append(f"Errors ({len(errors)}):")
        for err in errors:
            tag = "[auto-fixable]" if err.get("auto_fixable") else "[manual]"
            if err.get("file"):
                lines.append(f"  {tag} {err['tool']}: {err['file']}:{err.get('line', '?')} {err['message']}")
            else:
                lines.append(f"  {tag} {err['tool']}: {err['message']}")
        lines.append("")

    fixes = report.get("fixes", [])
    auto = [f for f in fixes if f.get("auto_fixable")]
    manual = [f for f in fixes if not f.get("auto_fixable")]

    if auto:
        lines.append("Auto-fix commands:")
        for fix in auto:
            lines.append(f"  $ {fix['command']}")
        lines.append("")

    if manual:
        lines.append("Manual fixes needed:")
        for fix in manual:
            lines.append(f"  - {fix['description']}")
        lines.append("")

    # Applied results (from apply_fixes)
    applied = report.get("applied", [])
    if applied:
        lines.append("Applied fixes:")
        for a in applied:
            icon = "OK" if a["success"] else "FAIL"
            lines.append(f"  [{icon}] {a['command']}")
            if a.get("output"):
                for out_line in a["output"].split("\n")[:3]:
                    lines.append(f"       {out_line}")
        lines.append("")

    remaining = report.get("remaining", [])
    if remaining:
        lines.append("Manual fixes still needed:")
        for r in remaining:
            lines.append(f"  - {r['description']}")

    # Push results
    if report.get("pushed"):
        lines.append(f"Pushed: {report.get('commit_sha', '?')[:8]}")
    elif report.get("committed"):
        lines.append(f"Committed: {report.get('commit_sha', '?')[:8]} (push failed)")

    return "\n".join(lines)
