# @feature:dispatch-scheduling
from __future__ import annotations

import difflib
import importlib
import importlib.util
import json
import os
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from exo.kernel import governance, ledger, open_session, seal_result, tickets
from exo.kernel.audit import append_audit, event_template
from exo.kernel.errors import ExoError
from exo.kernel.types import AuditRef
from exo.kernel.types import to_dict as type_to_dict
from exo.kernel.utils import (
    any_pattern_matches,
    dump_yaml,
    ensure_dir,
    gen_timestamp_id,
    load_yaml,
    now_iso,
    relative_posix,
)
from exo.stdlib import (
    dispatch,
    evolution,
    scratchpad,
)
from exo.stdlib import (
    distributed_leases as distributed_leases_mod,
)
from exo.stdlib import (
    recall as recall_mod,
)
from exo.stdlib import (
    sidecar as sidecar_mod,
)
from exo.stdlib.defaults import DEFAULT_CONFIG, DEFAULT_CONSTITUTION, seed_kernel_tickets

SCRIPT_FALLBACKS = {
    "plan": "exo.scripts.default_plan",
    "do": "exo.scripts.default_do",
    "check": "exo.scripts.default_check",
    "dispatch": "exo.scripts.default_dispatch",
    "compile_constitution": "exo.scripts.compile_constitution",
}

OVERLAY_TEMPLATES = {
    "plan.py": "from exo.scripts.default_plan import run\n",
    "do.py": "from exo.scripts.default_do import run\n",
    "check.py": "from exo.scripts.default_check import run\n",
    "dispatch.py": "from exo.scripts.default_dispatch import run\n",
    "compile_constitution.py": "from exo.scripts.compile_constitution import run\n",
}


_GITIGNORE_LOG_ENTRIES = [
    "# ExoProtocol: audit logs excluded (privacy.commit_logs=false)",
    ".exo/logs/",
]


def _manage_gitignore(repo: Path, config_path: Path) -> bool:
    """Add .exo/logs/ to .gitignore when privacy.commit_logs is False.

    Returns True if .gitignore was modified.
    """
    try:
        config = load_yaml(config_path) if config_path.exists() else {}
    except Exception:  # noqa: BLE001
        config = {}

    privacy = config.get("privacy", {})
    commit_logs = privacy.get("commit_logs", False)

    gitignore_path = repo / ".gitignore"
    sentinel = ".exo/logs/"

    if commit_logs:
        # If commit_logs is True, remove our entries if present
        if gitignore_path.exists():
            content = gitignore_path.read_text(encoding="utf-8")
            if sentinel in content:
                lines = content.splitlines()
                new_lines = [ln for ln in lines if ln.strip() not in (sentinel, _GITIGNORE_LOG_ENTRIES[0])]
                new_content = "\n".join(new_lines)
                if not new_content.endswith("\n"):
                    new_content += "\n"
                gitignore_path.write_text(new_content, encoding="utf-8")
                return True
        return False

    # commit_logs is False (default) — ensure .exo/logs/ is in .gitignore
    if gitignore_path.exists():
        content = gitignore_path.read_text(encoding="utf-8")
        if sentinel in content:
            return False  # already present
        if not content.endswith("\n"):
            content += "\n"
    else:
        content = ""

    content += "\n".join(_GITIGNORE_LOG_ENTRIES) + "\n"
    gitignore_path.write_text(content, encoding="utf-8")
    return True


class KernelEngine:
    def __init__(self, repo: Path | str = ".", *, actor: str = "human", no_llm: bool = False) -> None:
        self.repo = Path(repo).resolve()
        self.actor = actor
        self.no_llm = no_llm
        self.events: list[dict[str, Any]] = []
        self._budget_usage: dict[str, dict[str, Any]] = {}

    @property
    def exo_dir(self) -> Path:
        return self.repo / ".exo"

    def _begin(self) -> None:
        self.events = []
        self._budget_usage = {}

    def _audit(
        self,
        action: str,
        result: str,
        *,
        ticket: str | None = None,
        path: Path | None = None,
        rule: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditRef:
        rel_path = relative_posix(path, self.repo) if path else None
        event = event_template(
            actor=self.actor,
            action=action,
            result=result,
            ticket=ticket,
            path=rel_path,
            rule=rule,
            details=details,
        )
        ref = append_audit(self.repo, event)
        self.events.append(event)
        return ref

    def _response(self, data: dict[str, Any], *, blocked: bool = False) -> dict[str, Any]:
        return {
            "ok": True,
            "data": data,
            "events": self.events,
            "blocked": blocked,
        }

    def sidecar_init(
        self,
        *,
        branch: str = "exo-governance",
        sidecar: str = ".exo",
        remote: str = "origin",
        init_git: bool = True,
        default_branch: str = "main",
        fetch_remote: bool = True,
        commit_migration: bool = True,
    ) -> dict[str, Any]:
        self._begin()
        data = sidecar_mod.init_sidecar_worktree(
            self.repo,
            branch=branch,
            sidecar=sidecar,
            remote=remote,
            init_git=init_git,
            default_branch=default_branch,
            fetch_remote=fetch_remote,
            commit_migration=commit_migration,
        )
        return self._response(data)

    def _require_scaffold(self) -> None:
        if not self.exo_dir.exists():
            raise ExoError(code="EXO_NOT_INITIALIZED", message="Missing .exo/. Run: exo init")

    def _verify_integrity(self) -> dict[str, Any]:
        self._require_scaffold()
        return governance.verify_integrity(self.repo)

    def _enforce_kernel_mutation_boundary(self, path: Path, action: str) -> None:
        rel = relative_posix(path, self.repo)
        normalized = rel.replace("\\", "/")
        if normalized == "exo/kernel" or normalized.startswith("exo/kernel/"):
            raise ExoError(
                code="KERNEL_MUTATION_FORBIDDEN",
                message=(
                    "Kernel updates are out-of-band and human-only. "
                    "Governed ticket/proposal execution cannot mutate exo/kernel/**."
                ),
                details={"path": normalized, "action": action},
                blocked=True,
            )

    def _enforce_memory_mutation_boundary(self, path: Path, action: str) -> None:
        rel = relative_posix(path, self.repo)
        normalized = rel.replace("\\", "/")
        if normalized == ".exo/memory" or normalized.startswith(".exo/memory/"):
            raise ExoError(
                code="MEMORY_MUTATION_FORBIDDEN",
                message=(
                    "Layer-4 memory is advisory/read-only during governed execution. "
                    "Use explicit distillation flow to update memory artifacts."
                ),
                details={"path": normalized, "action": action},
                blocked=True,
            )

    def _config(self) -> dict[str, Any]:
        config_path = self.exo_dir / "config.yaml"
        if not config_path.exists():
            return dict(DEFAULT_CONFIG)
        loaded = load_yaml(config_path)
        merged = dict(DEFAULT_CONFIG)
        merged.update(loaded)
        return merged

    def _control_caps(self) -> dict[str, list[str]]:
        defaults = DEFAULT_CONFIG.get("control_caps", {})
        loaded = self._config().get("control_caps", {})

        merged: dict[str, Any] = {}
        if isinstance(defaults, dict):
            merged.update(defaults)
        if isinstance(loaded, dict):
            merged.update(loaded)

        normalized: dict[str, list[str]] = {}
        for capability, values in merged.items():
            if not isinstance(capability, str):
                continue
            if not isinstance(values, list):
                continue
            normalized[capability] = [str(value) for value in values if isinstance(value, str) and value.strip()]
        return normalized

    def _require_control_cap(self, capability: str, provided_cap: str | None) -> str:
        if not isinstance(provided_cap, str) or not provided_cap.strip():
            raise ExoError(
                code="CAPABILITY_REQUIRED",
                message=f"{capability} requires a non-empty capability token",
                blocked=True,
            )

        token = provided_cap.strip()
        allowed = self._control_caps().get(capability, [])
        if not allowed:
            raise ExoError(
                code="CAPABILITY_NOT_CONFIGURED",
                message=f"No configured capability grants for {capability}",
                blocked=True,
            )
        if "*" in allowed or token in allowed:
            return token
        raise ExoError(
            code="CAPABILITY_DENIED",
            message=f"Capability denied for {capability}",
            details={"required_any_of": allowed},
            blocked=True,
        )

    def _resolve_reference(self, ref: str) -> dict[str, str]:
        value = ref.strip()
        if not value:
            raise ExoError(code="REFERENCE_INVALID", message="reference is required", blocked=True)

        if value.startswith("line:"):
            line_no = value.split(":", 1)[1]
            if (
                line_no.isdigit()
                and int(line_no) > 0
                and ledger.read_records(self.repo, since_line=int(line_no), limit=1)
            ):
                return {"kind": "ledger", "value": value}
        elif ledger.read_records(self.repo, ref_id=value, limit=1):
            return {"kind": "ledger", "value": value}

        try:
            path = self._resolve_repo_path(value)
        except ExoError:
            path = None
        if isinstance(path, Path) and path.exists():
            return {"kind": "path", "value": relative_posix(path, self.repo)}

        raise ExoError(
            code="REFERENCE_NOT_FOUND",
            message=f"Reference not found in ledger or repository: {value}",
            details={"reference": value},
            blocked=True,
        )

    def _build_control_receipt(
        self,
        *,
        action: dict[str, Any],
        result: dict[str, Any],
        audit_refs: list[AuditRef],
        ticket_id: str,
    ) -> dict[str, Any]:
        session = open_session(self.repo, self.actor)
        now = now_iso()
        synthetic_ticket = {
            "id": ticket_id,
            "intent": action.get("kind", "control_action"),
            "scope": {"allow": [".exo/**"], "deny": []},
            "ttl_hours": 1,
            "created_at": now,
            "expires_at": now,
            "nonce": "CONTROL",
        }
        receipt = seal_result(session, synthetic_ticket, action, result, audit_refs)
        return type_to_dict(receipt)

    def _write_system_text(self, path: Path, content: str) -> bool:
        ensure_dir(path.parent)
        existed = path.exists()
        path.write_text(content, encoding="utf-8")
        self._audit("write_file", "ok", path=path)
        return not existed

    def _write_system_yaml(self, path: Path, payload: dict[str, Any]) -> bool:
        ensure_dir(path.parent)
        existed = path.exists()
        dump_yaml(path, payload)
        self._audit("write_file", "ok", path=path)
        return not existed

    def _git_controls(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "enabled": True,
            "strict_diff_budgets": True,
            "enforce_lock_branch": True,
            "auto_create_lock_branch": True,
            "auto_switch_lock_branch": True,
            "require_clean_worktree_before_do": True,
            "base_branch_fallback": "main",
            "ignore_paths": [
                ".exo/logs/**",
                ".exo/cache/**",
                ".exo/locks/**",
                ".exo/tickets/**",
                "**/__pycache__/**",
                "**/*.pyc",
            ],
        }
        raw = self._config().get("git_controls", {})
        if isinstance(raw, dict):
            defaults.update(raw)

        ignore_paths = defaults.get("ignore_paths", [])
        if not isinstance(ignore_paths, list):
            ignore_paths = []
        defaults["ignore_paths"] = [str(item) for item in ignore_paths]
        return defaults

    def _run_git(
        self,
        args: list[str],
        *,
        check: bool = True,
        error_code: str = "GIT_COMMAND_FAILED",
        message: str = "Git command failed",
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )
        if check and proc.returncode != 0:
            raise ExoError(
                code=error_code,
                message=message,
                details={
                    "command": "git " + " ".join(args),
                    "returncode": proc.returncode,
                    "stdout": (proc.stdout or "")[-1200:],
                    "stderr": (proc.stderr or "")[-1200:],
                },
                blocked=True,
            )
        return proc

    def _is_git_repo(self) -> bool:
        proc = self._run_git(
            ["rev-parse", "--is-inside-work-tree"],
            check=False,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def _require_git_repo(self) -> None:
        if self._is_git_repo():
            return
        raise ExoError(
            code="GIT_REQUIRED",
            message="Strict git controls require a git repository. Run: git init",
            blocked=True,
        )

    def _git_has_head(self) -> bool:
        proc = self._run_git(["rev-parse", "--verify", "HEAD"], check=False)
        return proc.returncode == 0

    def _git_current_branch(self) -> str:
        proc = self._run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            error_code="GIT_BRANCH_ERROR",
            message="Failed to determine current git branch",
        )
        branch = proc.stdout.strip()
        if not branch:
            raise ExoError(
                code="GIT_BRANCH_ERROR",
                message="Failed to determine current git branch",
                blocked=True,
            )
        return branch

    def _git_branch_exists(self, branch: str) -> bool:
        proc = self._run_git(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            check=False,
        )
        return proc.returncode == 0

    def _git_remote_branch_exists(self, branch: str) -> bool:
        proc = self._run_git(
            ["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
            check=False,
        )
        return proc.returncode == 0

    def _normalize_git_path(self, raw_path: str) -> str:
        path = raw_path.strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path.startswith('"') and path.endswith('"') and len(path) >= 2:
            path = path[1:-1]
        return path

    def _is_ignored_git_path(self, rel_path: str, ignore_patterns: list[str]) -> bool:
        if not ignore_patterns:
            return False
        return any_pattern_matches(self.repo / rel_path, ignore_patterns, self.repo)

    def _git_worktree_changes(self, ignore_patterns: list[str]) -> list[dict[str, str]]:
        proc = self._run_git(
            ["status", "--porcelain=1", "--untracked-files=all"],
            error_code="GIT_STATUS_ERROR",
            message="Failed to inspect git worktree status",
        )
        changes: list[dict[str, str]] = []
        for line in proc.stdout.splitlines():
            line = line.rstrip()
            if not line:
                continue
            if len(line) < 4:
                continue
            status = line[:2]
            path = self._normalize_git_path(line[3:])
            if not path:
                continue
            if self._is_ignored_git_path(path, ignore_patterns):
                continue
            changes.append({"status": status, "path": path})
        return changes

    def _count_file_lines(self, path: Path) -> int:
        if not path.exists() or not path.is_file():
            return 0
        data = path.read_bytes()
        if not data:
            return 0
        count = data.count(b"\n")
        if not data.endswith(b"\n"):
            count += 1
        return count

    def _git_numstat_map(self, ignore_patterns: list[str]) -> dict[str, int]:
        has_head = self._git_has_head()
        args = ["diff", "--numstat", "--relative"]
        if has_head:
            args.append("HEAD")
        args.append("--")

        proc = self._run_git(
            args,
            error_code="GIT_DIFF_ERROR",
            message="Failed to compute git numstat diff",
        )
        loc_by_path: dict[str, int] = {}
        for line in proc.stdout.splitlines():
            line = line.rstrip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            add_raw, del_raw, path_raw = parts[0], parts[1], parts[-1]
            path = self._normalize_git_path(path_raw)
            if not path or self._is_ignored_git_path(path, ignore_patterns):
                continue
            add = int(add_raw) if add_raw.isdigit() else 0
            delete = int(del_raw) if del_raw.isdigit() else 0
            loc_by_path[path] = add + delete

        untracked = self._run_git(
            ["ls-files", "--others", "--exclude-standard"],
            error_code="GIT_UNTRACKED_ERROR",
            message="Failed to list untracked files",
        )
        for line in untracked.stdout.splitlines():
            path = line.strip()
            if not path or self._is_ignored_git_path(path, ignore_patterns):
                continue
            loc_by_path[path] = max(loc_by_path.get(path, 0), self._count_file_lines(self.repo / path))

        return loc_by_path

    def _git_action_map(self, ignore_patterns: list[str]) -> dict[str, str]:
        has_head = self._git_has_head()
        args = ["diff", "--name-status", "--relative"]
        if has_head:
            args.append("HEAD")
        args.append("--")

        proc = self._run_git(
            args,
            error_code="GIT_DIFF_ERROR",
            message="Failed to compute git name-status diff",
        )
        actions: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            line = line.rstrip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue

            status = parts[0]
            code = status[:1]

            if code in {"R", "C"} and len(parts) >= 3:
                old_path = self._normalize_git_path(parts[1])
                new_path = self._normalize_git_path(parts[2])
                if old_path and not self._is_ignored_git_path(old_path, ignore_patterns):
                    actions[old_path] = "delete"
                if new_path and not self._is_ignored_git_path(new_path, ignore_patterns):
                    actions[new_path] = "write"
                continue

            path = self._normalize_git_path(parts[-1])
            if not path or self._is_ignored_git_path(path, ignore_patterns):
                continue
            actions[path] = "delete" if code == "D" else "write"

        untracked = self._run_git(
            ["ls-files", "--others", "--exclude-standard"],
            error_code="GIT_UNTRACKED_ERROR",
            message="Failed to list untracked files",
        )
        for line in untracked.stdout.splitlines():
            path = line.strip()
            if not path or self._is_ignored_git_path(path, ignore_patterns):
                continue
            actions[path] = "write"

        return actions

    def _git_change_snapshot(self, ignore_patterns: list[str]) -> dict[str, Any]:
        loc_by_path = self._git_numstat_map(ignore_patterns)
        actions = self._git_action_map(ignore_patterns)
        for path in actions:
            loc_by_path.setdefault(path, 0)
        return {
            "loc_by_path": loc_by_path,
            "actions": actions,
        }

    def _git_snapshot_delta(self, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        before_loc = before.get("loc_by_path", {})
        after_loc = after.get("loc_by_path", {})
        before_actions = before.get("actions", {})
        after_actions = after.get("actions", {})

        delta_paths: list[str] = []
        delta_actions: dict[str, str] = {}
        delta_loc: dict[str, int] = {}

        for path, action in after_actions.items():
            prev_action = before_actions.get(path)
            prev_loc = int(before_loc.get(path, 0))
            curr_loc = int(after_loc.get(path, 0))

            if prev_action == action and prev_loc == curr_loc:
                continue

            delta_paths.append(path)
            delta_actions[path] = action
            delta_loc[path] = max(curr_loc - prev_loc, 0)

        unique_paths = sorted(set(delta_paths))
        return {
            "paths": unique_paths,
            "actions": delta_actions,
            "loc_by_path": delta_loc,
        }

    def _enforce_lock_branch(self, lock: dict[str, Any], git_controls: dict[str, Any]) -> dict[str, Any]:
        expected_branch = str(
            ((lock.get("workspace") or {}).get("branch")) or f"codex/{lock.get('ticket_id', 'unknown')}"
        )
        base_branch = str(
            ((lock.get("workspace") or {}).get("base")) or git_controls.get("base_branch_fallback", "main")
        )
        current_branch = self._git_current_branch()
        actions: list[str] = []

        if current_branch != expected_branch:
            if not bool(git_controls.get("auto_switch_lock_branch", True)):
                raise ExoError(
                    code="BRANCH_MISMATCH",
                    message=f"Current branch {current_branch} does not match lock branch {expected_branch}",
                    details={"expected_branch": expected_branch, "current_branch": current_branch},
                    blocked=True,
                )

            dirty = self._git_worktree_changes(list(git_controls.get("ignore_paths", [])))
            if dirty:
                raise ExoError(
                    code="BRANCH_SWITCH_DIRTY",
                    message="Cannot switch to lock branch with uncommitted changes",
                    details={"changes": dirty[:50]},
                    blocked=True,
                )

            if self._git_branch_exists(expected_branch):
                self._run_git(
                    ["checkout", expected_branch],
                    error_code="BRANCH_SWITCH_FAILED",
                    message=f"Failed to switch to lock branch {expected_branch}",
                )
                actions.append("switched")
            else:
                if not bool(git_controls.get("auto_create_lock_branch", True)):
                    raise ExoError(
                        code="BRANCH_MISSING",
                        message=f"Lock branch does not exist: {expected_branch}",
                        details={"expected_branch": expected_branch},
                        blocked=True,
                    )

                start_ref: str | None = None
                if base_branch and self._git_branch_exists(base_branch):
                    start_ref = base_branch
                elif base_branch and self._git_remote_branch_exists(base_branch):
                    start_ref = f"origin/{base_branch}"
                elif self._git_has_head():
                    start_ref = current_branch

                create_args = ["checkout", "-b", expected_branch]
                if start_ref:
                    create_args.append(start_ref)
                self._run_git(
                    create_args,
                    error_code="BRANCH_CREATE_FAILED",
                    message=f"Failed to create lock branch {expected_branch}",
                )
                actions.append("created")
                if start_ref:
                    actions.append(f"from:{start_ref}")

        final_branch = self._git_current_branch()
        if final_branch != expected_branch:
            raise ExoError(
                code="BRANCH_MISMATCH",
                message=f"Current branch {final_branch} does not match lock branch {expected_branch}",
                details={"expected_branch": expected_branch, "current_branch": final_branch},
                blocked=True,
            )
        return {
            "expected_branch": expected_branch,
            "base_branch": base_branch,
            "current_branch": final_branch,
            "actions": actions,
        }

    def _enforce_git_change_scope(self, ticket: dict[str, Any], delta: dict[str, Any]) -> None:
        paths = delta.get("paths", [])
        if not isinstance(paths, list):
            return

        lock_data = self._verify_integrity()
        actions = delta.get("actions", {})
        if not isinstance(actions, dict):
            actions = {}

        for rel_path in paths:
            if not isinstance(rel_path, str):
                continue
            target = (self.repo / rel_path).resolve()
            if not target.is_relative_to(self.repo):
                raise ExoError(
                    code="PATH_OUTSIDE_REPO",
                    message=f"Git diff path escaped repository: {rel_path}",
                    blocked=True,
                )

            action = str(actions.get(rel_path, "write"))
            self._check_ticket_scope(ticket, target)
            deny_rule = governance.evaluate_filesystem_rules(lock_data, action, target, self.repo)
            if deny_rule:
                rule_id = str(deny_rule.get("id", "RULE_UNKNOWN"))
                raise ExoError(
                    code="RULE_BLOCKED",
                    message=deny_rule.get("message", "Action blocked by governance rule"),
                    details={"rule": rule_id, "path": rel_path, "action": action},
                    blocked=True,
                )

    def _enforce_git_budget(self, ticket: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
        paths = delta.get("paths", [])
        if not isinstance(paths, list):
            paths = []
        loc_by_path = delta.get("loc_by_path", {})
        if not isinstance(loc_by_path, dict):
            loc_by_path = {}

        files_changed = len(paths)
        loc_changed = 0
        for path in paths:
            loc_changed += int(loc_by_path.get(path, 0))

        budgets = ticket.get("budgets") or {}
        max_files = int(budgets.get("max_files_changed", 12))
        max_loc = int(budgets.get("max_loc_changed", 400))

        if files_changed > max_files:
            raise ExoError(
                code="BUDGET_FILES_EXCEEDED",
                message=f"Budget exceeded by git diff: files changed {files_changed}/{max_files}",
                blocked=True,
            )
        if loc_changed > max_loc:
            raise ExoError(
                code="BUDGET_LOC_EXCEEDED",
                message=f"Budget exceeded by git diff: LOC changed {loc_changed}/{max_loc}",
                blocked=True,
            )
        return {
            "files_changed": files_changed,
            "loc_changed": loc_changed,
            "max_files_changed": max_files,
            "max_loc_changed": max_loc,
            "paths": paths,
        }

    def _git_branch_divergence(self, base_ref: str, branch_ref: str) -> dict[str, int]:
        proc = self._run_git(
            ["rev-list", "--left-right", "--count", f"{base_ref}...{branch_ref}"],
            check=False,
        )
        if proc.returncode != 0:
            return {"ahead": 0, "behind": 0}
        text = proc.stdout.strip()
        if not text:
            return {"ahead": 0, "behind": 0}
        parts = text.split()
        if len(parts) < 2:
            return {"ahead": 0, "behind": 0}
        behind = int(parts[0]) if parts[0].isdigit() else 0
        ahead = int(parts[1]) if parts[1].isdigit() else 0
        return {"ahead": ahead, "behind": behind}

    def _audit_lock_branch_policy(
        self,
        lock: dict[str, Any],
        issues: list[str],
    ) -> dict[str, Any] | None:
        git_controls = self._git_controls()
        if not bool(git_controls.get("enabled", True)):
            return None
        if not self._is_git_repo():
            return {"enabled": True, "git_repo": False}

        report: dict[str, Any] = {
            "enabled": True,
            "git_repo": True,
        }
        current_branch = self._git_current_branch()
        expected_branch = str(
            ((lock.get("workspace") or {}).get("branch")) or f"codex/{lock.get('ticket_id', 'unknown')}"
        )
        base_branch = str(
            ((lock.get("workspace") or {}).get("base")) or git_controls.get("base_branch_fallback", "main")
        )
        report["current_branch"] = current_branch
        report["expected_branch"] = expected_branch
        report["base_branch"] = base_branch

        expected_exists = self._git_branch_exists(expected_branch)
        report["expected_branch_exists"] = expected_exists
        if not expected_exists:
            issues.append(f"LOCK_BRANCH_MISSING: lock branch does not exist: {expected_branch}")

        if bool(git_controls.get("enforce_lock_branch", True)) and current_branch != expected_branch:
            issues.append(
                f"BRANCH_POLICY_VIOLATION: current branch {current_branch} does not match lock branch {expected_branch}"
            )

        base_ref = None
        if base_branch:
            if self._git_branch_exists(base_branch):
                base_ref = base_branch
            elif self._git_remote_branch_exists(base_branch):
                base_ref = f"origin/{base_branch}"
            else:
                issues.append(f"LOCK_BASE_BRANCH_MISSING: base branch not found locally/remotely: {base_branch}")

        divergence: dict[str, int] = {"ahead": 0, "behind": 0}
        if base_ref and expected_exists:
            divergence = self._git_branch_divergence(base_ref, expected_branch)
        report["divergence"] = divergence

        try:
            created_at = datetime.fromisoformat(str(lock.get("created_at")))
            lock_age_hours = max((datetime.now().astimezone() - created_at).total_seconds() / 3600.0, 0.0)
        except Exception:
            lock_age_hours = 0.0
        report["lock_age_hours"] = round(lock_age_hours, 2)

        stale_hours = int(git_controls.get("stale_lock_drift_hours", 24))
        if lock_age_hours >= stale_hours and (divergence.get("ahead", 0) > 0 or divergence.get("behind", 0) > 0):
            issues.append(
                "STALE_LOCK_BRANCH_DRIFT: "
                f"lock branch {expected_branch} diverged from {base_ref or base_branch} "
                f"(ahead={divergence.get('ahead', 0)}, behind={divergence.get('behind', 0)}) "
                f"for {round(lock_age_hours, 2)}h"
            )

        ignore_patterns = list(git_controls.get("ignore_paths", []))
        snapshot = self._git_change_snapshot(ignore_patterns)
        actions = snapshot.get("actions", {})
        paths = sorted(actions.keys()) if isinstance(actions, dict) else []
        report["worktree_paths"] = paths
        report["worktree_paths_count"] = len(paths)

        ticket_id = str(lock.get("ticket_id", "")).strip()
        if ticket_id:
            try:
                ticket = tickets.load_ticket(self.repo, ticket_id)
                if ticket.get("status") in {"done", "archived"}:
                    issues.append(
                        f"STALE_LOCK_DONE_TICKET: active lock on ticket {ticket_id} with status {ticket.get('status')}"
                    )

                if paths:
                    out_of_scope: list[str] = []
                    denied: list[str] = []
                    lock_data = governance.load_governance_lock(self.repo)
                    for rel_path in paths:
                        action = str(actions.get(rel_path, "write"))
                        target = (self.repo / rel_path).resolve()
                        try:
                            self._check_ticket_scope(ticket, target)
                        except ExoError:
                            out_of_scope.append(rel_path)
                            continue
                        deny_rule = governance.evaluate_filesystem_rules(lock_data, action, target, self.repo)
                        if deny_rule:
                            rule_id = str(deny_rule.get("id", "RULE_UNKNOWN"))
                            denied.append(f"{rel_path} ({rule_id})")

                    if out_of_scope:
                        issues.append(
                            "LOCK_BRANCH_SCOPE_DRIFT: changed paths outside ticket scope: "
                            + ", ".join(out_of_scope[:10])
                        )
                    if denied:
                        issues.append(
                            "LOCK_BRANCH_RULE_DRIFT: changed paths denied by governance: " + ", ".join(denied[:10])
                        )

                    delta = {
                        "paths": paths,
                        "actions": actions,
                        "loc_by_path": snapshot.get("loc_by_path", {}),
                    }
                    try:
                        budget = self._enforce_git_budget(ticket, delta)
                        report["worktree_budget"] = budget
                    except ExoError as err:
                        issues.append(f"LOCK_BRANCH_BUDGET_DRIFT: {err.message}")
            except ExoError as err:
                issues.append(f"LOCK_TICKET_INVALID: {err.message}")

        return report

    def _ensure_evolution_layout(self) -> None:
        evolution.ensure_layout(self.repo)

    def _validate_proposal_schema(self, proposal: dict[str, Any]) -> None:
        errors = evolution.validate_proposal(self.repo, proposal)
        if errors:
            raise ExoError(
                code="PROPOSAL_SCHEMA_INVALID",
                message="Proposal failed schema validation",
                details={"errors": errors},
                blocked=True,
            )

    def _load_proposal(self, proposal_id: str) -> tuple[dict[str, Any], Path]:
        try:
            proposal, path = evolution.load_proposal(self.repo, proposal_id)
        except FileNotFoundError:
            raise ExoError(
                code="PROPOSAL_NOT_FOUND",
                message=f"Proposal not found: {proposal_id}",
            ) from None
        self._validate_proposal_schema(proposal)
        return proposal, path

    def _proposal_gate_summary(self, proposal: dict[str, Any]) -> dict[str, Any]:
        config = self._config()
        evo = config.get("self_evolution", {})
        trusted = evo.get("trusted_approvers", []) if isinstance(evo, dict) else []
        if not isinstance(trusted, list):
            trusted = []
        return evolution.gate_summary(proposal, [str(item) for item in trusted])

    def _enforce_governance_cooldown(self) -> None:
        config = self._config()
        evo = config.get("self_evolution", {})
        if not isinstance(evo, dict):
            return
        cooldown_hours = int(evo.get("governance_cooldown_hours", 24))
        if cooldown_hours <= 0:
            return

        latest = evolution.last_governance_apply_ts(self.repo)
        if not latest:
            return
        now = datetime.now().astimezone()
        if now < latest + timedelta(hours=cooldown_hours):
            delta = latest + timedelta(hours=cooldown_hours) - now
            remaining = int(max(delta.total_seconds() // 3600, 0))
            raise ExoError(
                code="GOVERNANCE_COOLDOWN",
                message=(f"Governance proposal cooldown active. Try again in about {remaining}h."),
                blocked=True,
            )

    def _validate_evolution_ticket(self, ticket: dict[str, Any], kind: str) -> None:
        ticket_id = str(ticket.get("id", ""))
        ticket_type = str(ticket.get("type", ""))
        labels_raw = ticket.get("labels", [])
        labels = set(str(item) for item in labels_raw) if isinstance(labels_raw, list) else set()

        if kind == "governance_change":
            if ticket_id.startswith("GOV-") or ticket_type == "governance":
                return
            raise ExoError(
                code="GOVERNANCE_TICKET_REQUIRED",
                message="governance_change requires GOV-* ticket id or ticket type governance",
                blocked=True,
            )

        if kind == "practice_change":
            if ticket_id.startswith("PRACTICE-") or "practice" in labels or ticket_type in {"docs", "governance"}:
                return
            raise ExoError(
                code="PRACTICE_TICKET_REQUIRED",
                message="practice_change requires PRACTICE-* ticket id or practice/docs/governance ticket semantics",
                blocked=True,
            )

    def _import_runner(self, script_name: str) -> tuple[Callable[[dict[str, Any]], dict[str, Any]], str]:
        local = self.exo_dir / "scripts" / f"{script_name}.py"
        if local.exists():
            module_name = f"exo_user_{script_name}_{os.getpid()}"
            spec = importlib.util.spec_from_file_location(module_name, local)
            if not spec or not spec.loader:
                raise ExoError(
                    code="SCRIPT_LOAD_ERROR",
                    message=f"Cannot load script: {local}",
                )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            runner = getattr(module, "run", None)
            if not callable(runner):
                raise ExoError(
                    code="SCRIPT_INVALID",
                    message=f"Script {local} must export run(payload: dict) -> dict",
                )
            return runner, str(local.relative_to(self.repo))

        fallback = SCRIPT_FALLBACKS[script_name]
        module = importlib.import_module(fallback)
        runner = getattr(module, "run", None)
        if not callable(runner):
            raise ExoError(code="SCRIPT_INVALID", message=f"Fallback script invalid: {fallback}")
        return runner, fallback

    def _run_script(self, script_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        runner, source = self._import_runner(script_name)
        result = runner(payload)
        if result is None:
            result = {}
        if not isinstance(result, dict):
            raise ExoError(
                code="SCRIPT_RESULT_INVALID",
                message=f"{script_name} script must return dict",
                details={"script": source},
            )
        result["_script_source"] = source
        return result

    def _resolve_repo_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path)
        resolved = candidate.resolve() if candidate.is_absolute() else (self.repo / candidate).resolve()
        if not resolved.is_relative_to(self.repo):
            raise ExoError(
                code="PATH_OUTSIDE_REPO",
                message=f"Path escapes repository: {raw_path}",
                blocked=True,
            )
        return resolved

    def _check_ticket_scope(self, ticket: dict[str, Any], path: Path) -> None:
        scope = ticket.get("scope") or {}
        allow = scope.get("allow") or ["**"]
        deny = scope.get("deny") or []

        if any_pattern_matches(path, deny, self.repo):
            raise ExoError(
                code="SCOPE_DENY",
                message=f"Path denied by ticket scope: {relative_posix(path, self.repo)}",
                blocked=True,
            )

        if not any_pattern_matches(path, allow, self.repo):
            raise ExoError(
                code="SCOPE_ALLOW_REQUIRED",
                message=f"Path outside ticket allow scope: {relative_posix(path, self.repo)}",
                blocked=True,
            )

    def _estimate_loc_delta(self, old_text: str, new_text: str, action: str) -> int:
        if action == "delete":
            return len(old_text.splitlines())
        if old_text == new_text:
            return 0
        diff = difflib.ndiff(old_text.splitlines(), new_text.splitlines())
        changed = sum(1 for line in diff if line.startswith("+") or line.startswith("-"))
        return changed

    def _preview_budget(
        self,
        ticket: dict[str, Any],
        path: Path,
        action: str,
        old_text: str,
        new_text: str,
        usage_state: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[bool, int]:
        tid = str(ticket["id"])
        usage_map = usage_state if usage_state is not None else self._budget_usage
        usage = usage_map.setdefault(tid, {"files": set(), "loc": 0})
        files: set[str] = usage["files"]
        loc: int = int(usage["loc"])

        rel = relative_posix(path, self.repo)
        is_new_file = rel not in files
        loc_delta = self._estimate_loc_delta(old_text, new_text, action)

        budgets = ticket.get("budgets") or {}
        max_files = int(budgets.get("max_files_changed", 12))
        max_loc = int(budgets.get("max_loc_changed", 400))

        predicted_files = len(files) + (1 if is_new_file else 0)
        predicted_loc = loc + loc_delta

        if predicted_files > max_files:
            raise ExoError(
                code="BUDGET_FILES_EXCEEDED",
                message=f"Budget exceeded: files changed {predicted_files}/{max_files}",
                blocked=True,
            )
        if predicted_loc > max_loc:
            raise ExoError(
                code="BUDGET_LOC_EXCEEDED",
                message=f"Budget exceeded: LOC changed {predicted_loc}/{max_loc}",
                blocked=True,
            )
        return is_new_file, loc_delta

    def _commit_budget(
        self,
        ticket: dict[str, Any],
        path: Path,
        is_new_file: bool,
        loc_delta: int,
        usage_state: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        tid = str(ticket["id"])
        usage_map = usage_state if usage_state is not None else self._budget_usage
        usage = usage_map.setdefault(tid, {"files": set(), "loc": 0})
        rel = relative_posix(path, self.repo)
        if is_new_file:
            usage["files"].add(rel)
        usage["loc"] = int(usage["loc"]) + loc_delta

    def _authorize_write(
        self,
        ticket: dict[str, Any],
        path: Path,
        action: str,
        old_text: str,
        new_text: str,
        usage_state: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[bool, int]:
        lock = tickets.ensure_lock(self.repo, str(ticket["id"]))
        _ = lock
        self._enforce_kernel_mutation_boundary(path, action)
        self._enforce_memory_mutation_boundary(path, action)
        self._check_ticket_scope(ticket, path)

        lock_data = self._verify_integrity()
        deny_rule = governance.evaluate_filesystem_rules(lock_data, action, path, self.repo)
        if deny_rule:
            rule_id = str(deny_rule.get("id", "RULE_UNKNOWN"))
            raise ExoError(
                code="RULE_BLOCKED",
                message=deny_rule.get("message", "Action blocked by governance rule"),
                details={"rule": rule_id},
                blocked=True,
            )

        return self._preview_budget(ticket, path, action, old_text, new_text, usage_state=usage_state)

    def _write_ticket_file(self, ticket: dict[str, Any], target: Path, content: str) -> None:
        old = target.read_text(encoding="utf-8") if target.exists() else ""
        is_new_file, loc_delta = self._authorize_write(ticket, target, "write", old, content)

        ensure_dir(target.parent)
        target.write_text(content, encoding="utf-8")
        self._commit_budget(ticket, target, is_new_file, loc_delta)
        self._audit("write_file", "ok", ticket=str(ticket["id"]), path=target)

    def _run_allowlisted_command(self, command: str, allowlist: list[str], action: str) -> dict[str, Any]:
        if command not in allowlist:
            raise ExoError(
                code="COMMAND_BLOCKED",
                message=f"Command not allowlisted: {command}",
                details={"allowlist": allowlist},
                blocked=True,
            )

        proc = subprocess.run(
            command,
            cwd=self.repo,
            shell=True,
            capture_output=True,
            text=True,
        )
        result = {
            "command": command,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-4000:],
            "stderr": (proc.stderr or "")[-4000:],
            "ok": proc.returncode == 0,
        }
        self._audit(
            action,
            "ok" if proc.returncode == 0 else "failed",
            details={"command": command, "returncode": proc.returncode},
        )
        return result

    def _current_ticket(self, ticket_id: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        if ticket_id:
            lock = tickets.ensure_lock(self.repo, ticket_id)
            ticket = tickets.load_ticket(self.repo, ticket_id)
            return ticket, lock

        lock = tickets.ensure_lock(self.repo)
        lock_ticket = str(lock["ticket_id"])
        ticket = tickets.load_ticket(self.repo, lock_ticket)
        return ticket, lock

    def _execute_checks(self, ticket: dict[str, Any] | None) -> dict[str, Any]:
        config = self._config()
        payload = {
            "repo": str(self.repo),
            "ticket": ticket,
            "config": config,
            "no_llm": self.no_llm,
        }
        script_out = self._run_script("check", payload)

        checks = script_out.get("checks")
        if checks is None:
            checks = (ticket or {}).get("checks", []) if ticket else []
        if not isinstance(checks, list):
            raise ExoError(code="CHECK_SCRIPT_INVALID", message="check script must return checks list")

        allowlist = list(config.get("checks_allowlist", []))
        results = [self._run_allowlisted_command(str(cmd), allowlist, "run_check") for cmd in checks]
        passed = all(item["ok"] for item in results)
        return {
            "checks": checks,
            "allowlist": allowlist,
            "results": results,
            "passed": passed,
            "script": script_out.get("_script_source"),
        }

    def _next_spec_path(self) -> Path:
        specs_dir = self.exo_dir / "specs"
        ensure_dir(specs_dir)
        spec_id = gen_timestamp_id("SPEC")
        return specs_dir / f"{spec_id}.md"

    def _mark_ticket_status(self, ticket: dict[str, Any], status: str) -> None:
        ticket["status"] = status
        tickets.save_ticket(self.repo, ticket)
        self._audit("update_ticket", "ok", ticket=str(ticket["id"]), details={"status": status})

    def init(self, *, seed: bool = False, scan: bool = True) -> dict[str, Any]:
        self._begin()

        # Run repo scan before writing anything (scan is read-only)
        scan_report = None
        scan_dict = None
        if scan:
            try:
                from exo.stdlib.scan import generate_config, generate_constitution, scan_repo, scan_to_dict

                scan_report = scan_repo(self.repo)
                scan_dict = scan_to_dict(scan_report)
            except Exception:  # noqa: BLE001
                pass

        dirs = [
            self.exo_dir,
            self.exo_dir / "tickets",
            self.exo_dir / "tickets" / "ARCHIVE",
            self.exo_dir / "locks",
            self.exo_dir / "scratchpad",
            self.exo_dir / "scratchpad" / "threads",
            self.exo_dir / "logs",
            self.exo_dir / "scripts",
            self.exo_dir / "cache",
            self.exo_dir / "cache" / "distill",
            self.exo_dir / "specs",
            self.exo_dir / "observations",
            self.exo_dir / "patches",
            self.exo_dir / "proposals",
            self.exo_dir / "reviews",
            self.exo_dir / "practices",
            self.exo_dir / "roles",
            self.exo_dir / "memory",
            self.exo_dir / "templates",
            self.exo_dir / "schemas",
        ]
        for directory in dirs:
            ensure_dir(directory)

        created: list[str] = []
        constitution_path = self.exo_dir / "CONSTITUTION.md"
        if not constitution_path.exists():
            # Use scan-generated constitution if available, otherwise default
            constitution_text = DEFAULT_CONSTITUTION
            if scan_report is not None:
                try:
                    from exo.stdlib.scan import generate_constitution

                    constitution_text = generate_constitution(scan_report)
                except Exception:  # noqa: BLE001
                    pass
            self._write_system_text(constitution_path, constitution_text)
            created.append(str(constitution_path.relative_to(self.repo)))

        config_path = self.exo_dir / "config.yaml"
        if not config_path.exists():
            # Use scan-generated config if available, otherwise default
            config_data = DEFAULT_CONFIG
            if scan_report is not None:
                try:
                    from exo.stdlib.scan import generate_config

                    config_data = generate_config(scan_report)
                except Exception:  # noqa: BLE001
                    pass
            self._write_system_yaml(config_path, config_data)
            created.append(str(config_path.relative_to(self.repo)))

        inbox = self.exo_dir / "scratchpad" / "INBOX.md"
        if not inbox.exists():
            self._write_system_text(inbox, "# INBOX\n\n")
            created.append(str(inbox.relative_to(self.repo)))

        memory_index = self.exo_dir / "memory" / "index.yaml"
        if not memory_index.exists():
            self._write_system_yaml(memory_index, evolution.default_memory_index())
            created.append(str(memory_index.relative_to(self.repo)))

        schema_path = self.exo_dir / "schemas" / "proposal.schema.json"
        if not schema_path.exists():
            self._write_system_text(
                schema_path,
                json.dumps(evolution.proposal_schema_template(), indent=2, ensure_ascii=True) + "\n",
            )
            created.append(str(schema_path.relative_to(self.repo)))

        obs_template = self.exo_dir / "templates" / "OBS.template.md"
        if not obs_template.exists():
            self._write_system_text(obs_template, evolution.OBS_TEMPLATE)
            created.append(str(obs_template.relative_to(self.repo)))

        prop_template = self.exo_dir / "templates" / "PROP.template.yaml"
        if not prop_template.exists():
            self._write_system_text(prop_template, evolution.PROP_TEMPLATE)
            created.append(str(prop_template.relative_to(self.repo)))

        rev_template = self.exo_dir / "templates" / "REV.template.md"
        if not rev_template.exists():
            self._write_system_text(rev_template, evolution.REV_TEMPLATE)
            created.append(str(rev_template.relative_to(self.repo)))

        memory_template = self.exo_dir / "templates" / "memory.index.template.yaml"
        if not memory_template.exists():
            self._write_system_text(
                memory_template,
                json.dumps(evolution.default_memory_index(), indent=2, ensure_ascii=True) + "\n",
            )
            created.append(str(memory_template.relative_to(self.repo)))

        for script_name, script_body in OVERLAY_TEMPLATES.items():
            path = self.exo_dir / "scripts" / script_name
            if not path.exists():
                self._write_system_text(path, script_body)
                created.append(str(path.relative_to(self.repo)))

        seed_spec_id = gen_timestamp_id("SPEC")
        spec_path = self.exo_dir / "specs" / f"{seed_spec_id}.md"
        if seed and not spec_path.exists():
            spec_body = (
                f"# {seed_spec_id}\n\n"
                "Example specification created by `exo init`.\n\n"
                "Replace this with your project's first spec.\n"
            )
            self._write_system_text(spec_path, spec_body)
            created.append(str(spec_path.relative_to(self.repo)))

        compile_out = governance.compile_constitution(self.repo)
        self._audit("build_governance", "ok", details={"source_hash": compile_out.get("source_hash")})

        seeded: list[str] = []
        if seed:
            for ticket in seed_kernel_tickets(now_iso()):
                path = tickets.ticket_path(self.repo, str(ticket["id"]))
                if path.exists():
                    continue
                tickets.save_ticket(self.repo, ticket)
                self._audit("create_ticket", "ok", ticket=str(ticket["id"]), path=path)
                seeded.append(str(path.relative_to(self.repo)))

        # Auto-generate adapters (advisory — never blocks init)
        adapters_generated: list[str] = []
        if scan:
            try:
                from exo.stdlib.adapters import generate_adapters

                adapter_result = generate_adapters(self.repo)
                adapters_generated = adapter_result.get("written", [])
            except Exception:  # noqa: BLE001
                pass

        # Manage .gitignore based on privacy config
        gitignore_updated = _manage_gitignore(self.repo, config_path)

        result: dict[str, Any] = {
            "repo": str(self.repo),
            "created": created,
            "seeded_tickets": seeded,
            "governance_source_hash": compile_out.get("source_hash"),
        }
        if scan_dict is not None:
            result["scan"] = scan_dict
        if adapters_generated:
            result["adapters_generated"] = adapters_generated
        else:
            result["adapters_generated"] = []
        if gitignore_updated:
            result["gitignore_updated"] = True

        return self._response(result)

    def build_governance(self) -> dict[str, Any]:
        self._begin()
        self._require_scaffold()

        script_result = self._run_script("compile_constitution", {"repo": str(self.repo)})
        lock_data = script_result.get("lock_data")
        if not isinstance(lock_data, dict):
            lock_data = governance.compile_constitution(self.repo)

        self._audit("build_governance", "ok", details={"source_hash": lock_data.get("source_hash")})
        return self._response({"lock": lock_data, "script": script_result.get("_script_source")})

    def audit(self) -> dict[str, Any]:
        self._begin()
        self._require_scaffold()

        issues: list[str] = []
        git_report: dict[str, Any] | None = None
        lock_data: dict[str, Any] | None = None
        integrity_ok = True
        try:
            lock_data = governance.verify_integrity(self.repo)
        except ExoError as err:
            integrity_ok = False
            issues.append(f"{err.code}: {err.message}")
            try:
                lock_data = governance.load_governance_lock(self.repo)
            except ExoError as lock_err:
                issues.append(f"{lock_err.code}: {lock_err.message}")

        if lock_data:
            issues.extend(governance.sanity_check_rules(lock_data))

        lock = tickets.load_lock(self.repo)
        lock_ok = True
        if lock:
            ticket_id = str(lock.get("ticket_id", ""))
            if not ticket_id:
                lock_ok = False
                issues.append("ticket lock missing ticket_id")
            else:
                ticket_path = tickets.ticket_path(self.repo, ticket_id)
                if not ticket_path.exists():
                    lock_ok = False
                    issues.append(f"ticket lock references missing ticket: {ticket_id}")
                else:
                    git_report = self._audit_lock_branch_policy(lock, issues)
        else:
            git_controls = self._git_controls()
            if bool(git_controls.get("enabled", True)):
                git_report = {"enabled": True, "git_repo": self._is_git_repo()}

        overall_ok = integrity_ok and lock_ok and not issues
        self._audit("audit", "ok" if overall_ok else "failed", details={"issue_count": len(issues)})

        return self._response(
            {
                "integrity_ok": integrity_ok,
                "lock_ok": lock_ok,
                "issues": issues,
                "lock": lock,
                "git": git_report,
            },
            blocked=not overall_ok,
        )

    def status(self) -> dict[str, Any]:
        self._begin()
        if not self.exo_dir.exists():
            return self._response(
                {
                    "initialized": False,
                    "repo": str(self.repo),
                }
            )

        config = self._config()
        scheduler = config.get("scheduler") if isinstance(config.get("scheduler"), dict) else None

        integrity_ok = True
        integrity_error: str | None = None
        try:
            _ = governance.verify_integrity(self.repo)
        except ExoError as err:
            integrity_ok = False
            integrity_error = f"{err.code}: {err.message}"

        all_tickets = tickets.load_all_tickets(self.repo)
        counts: dict[str, int] = {}
        for ticket in all_tickets:
            status = str(ticket.get("status", "todo"))
            counts[status] = counts.get(status, 0) + 1

        lock = tickets.load_lock(self.repo)
        result = dispatch.choose_next_ticket(all_tickets, scheduler=scheduler, active_lock=lock)

        self._audit("status", "ok", details={"tickets": len(all_tickets)})

        return self._response(
            {
                "initialized": True,
                "integrity_ok": integrity_ok,
                "integrity_error": integrity_error,
                "ticket_counts": counts,
                "active_lock": lock,
                "next_candidate": result.get("ticket", {}).get("id") if result.get("ticket") else None,
                "dispatch_reasoning": result.get("reasoning", {}),
            }
        )

    def plan(self, input_value: str) -> dict[str, Any]:
        self._begin()
        self._verify_integrity()

        source_path = Path(input_value)
        if source_path.exists():
            input_text = source_path.read_text(encoding="utf-8")
            input_origin = str(source_path)
        else:
            input_text = input_value
            input_origin = "inline"

        spec_path = self._next_spec_path()
        script_out = self._run_script(
            "plan",
            {
                "repo": str(self.repo),
                "input": input_text,
                "input_origin": input_origin,
                "no_llm": self.no_llm,
            },
        )

        spec_markdown = script_out.get("spec_markdown")
        if not isinstance(spec_markdown, str) or not spec_markdown.strip():
            spec_markdown = (
                f"# {spec_path.stem}\n\n"
                f"Generated at {now_iso()} from {input_origin}.\n\n"
                "## Input\n\n"
                f"{input_text.strip()}\n"
            )

        self._write_system_text(spec_path, spec_markdown)

        created_tickets: list[str] = []
        raw_tickets = script_out.get("tickets", [])
        if isinstance(raw_tickets, list):
            for raw_ticket in raw_tickets:
                if not isinstance(raw_ticket, dict):
                    continue
                ticket = dict(raw_ticket)
                kind = str(ticket.get("kind", "task")).strip().lower()
                if not ticket.get("id"):
                    if kind == "intent":
                        ticket["id"] = tickets.allocate_intent_id(self.repo)
                    else:
                        ticket["id"] = tickets.allocate_ticket_id(self.repo, kind=kind)
                ticket.setdefault("spec_ref", str(spec_path.relative_to(self.repo)))
                ticket.setdefault("created_at", now_iso())
                tickets.save_ticket(self.repo, ticket)
                created_tickets.append(str(ticket["id"]))
                self._audit("create_ticket", "ok", ticket=str(ticket["id"]))

        self._audit("plan", "ok", details={"tickets_created": len(created_tickets)})

        # Advisory sidecar commit for newly created tickets
        if created_tickets:
            try:
                from exo.stdlib.sidecar import commit_sidecar

                commit_sidecar(
                    self.repo,
                    message=f"chore(exo): plan — {len(created_tickets)} ticket(s) created",
                )
            except Exception:
                pass

        return self._response(
            {
                "spec": str(spec_path.relative_to(self.repo)),
                "tickets_created": created_tickets,
                "script": script_out.get("_script_source"),
            }
        )

    def next(
        self,
        *,
        owner: str = "human",
        role: str = "developer",
        distributed: bool = False,
        remote: str = "origin",
        duration_hours: int = 2,
    ) -> dict[str, Any]:
        self._begin()
        self._verify_integrity()

        config = self._config()
        scheduler = config.get("scheduler") if isinstance(config.get("scheduler"), dict) else None
        all_tickets = tickets.load_all_tickets(self.repo)
        active_lock = tickets.load_lock(self.repo)
        script_out = self._run_script(
            "dispatch",
            {
                "tickets": all_tickets,
                "repo": str(self.repo),
                "scheduler": scheduler,
                "active_lock": active_lock,
            },
        )

        chosen_id = script_out.get("ticket_id")
        reasoning = script_out.get("reasoning") or {}

        if chosen_id:
            chosen = tickets.load_ticket(self.repo, str(chosen_id))
        else:
            fallback = dispatch.choose_next_ticket(all_tickets, scheduler=scheduler, active_lock=active_lock)
            chosen = fallback.get("ticket")
            if not reasoning:
                reasoning = fallback.get("reasoning") or {}

        if not chosen:
            raise ExoError(code="NO_TICKET", message="No dispatchable todo ticket found")

        distributed_result: dict[str, Any] | None = None
        if distributed:
            manager = distributed_leases_mod.GitDistributedLeaseManager(self.repo)
            distributed_result = manager.claim(
                str(chosen["id"]),
                owner=owner,
                role=role,
                duration_hours=duration_hours,
                remote=remote,
            )
            lock = dict(distributed_result.get("lock", {}))
        else:
            lock = tickets.acquire_lock(
                self.repo, str(chosen["id"]), owner=owner, role=role, duration_hours=duration_hours
            )
        if chosen.get("status") == "todo":
            chosen["status"] = "active"
            tickets.save_ticket(self.repo, chosen)
            self._audit("update_ticket", "ok", ticket=str(chosen["id"]), details={"status": "active"})

        self._audit(
            "acquire_lock",
            "ok",
            ticket=str(chosen["id"]),
            details={"owner": owner, "distributed": distributed, "remote": (remote if distributed else None)},
        )
        return self._response(
            {
                "ticket_id": chosen["id"],
                "lock": lock,
                "distributed": (distributed_result.get("distributed") if distributed_result else None),
                "reasoning": reasoning,
                "script": script_out.get("_script_source"),
            }
        )

    def lease_renew(
        self,
        ticket_id: str | None = None,
        *,
        owner: str | None = None,
        role: str | None = None,
        duration_hours: int = 2,
        distributed: bool = False,
        remote: str = "origin",
    ) -> dict[str, Any]:
        self._begin()
        lock = tickets.ensure_lock(self.repo, ticket_id=ticket_id)
        target_ticket_id = str(lock.get("ticket_id", "")).strip()
        if not target_ticket_id:
            raise ExoError(code="LOCK_TICKET_INVALID", message="Active lock missing ticket_id", blocked=True)

        effective_owner = (
            owner.strip() if isinstance(owner, str) and owner.strip() else str(lock.get("owner", "")).strip()
        )
        if not effective_owner:
            effective_owner = self.actor
        effective_role = role.strip() if isinstance(role, str) and role.strip() else str(lock.get("role", "developer"))
        distributed_result: dict[str, Any] | None = None
        if distributed:
            manager = distributed_leases_mod.GitDistributedLeaseManager(self.repo)
            distributed_result = manager.renew(
                target_ticket_id,
                owner=effective_owner,
                role=effective_role,
                duration_hours=duration_hours,
                remote=remote,
            )
            renewed = dict(distributed_result.get("lock", {}))
        else:
            workspace = lock.get("workspace") if isinstance(lock.get("workspace"), dict) else {}
            base_branch = str(workspace.get("base") or "main")
            renewed = tickets.acquire_lock(
                self.repo,
                target_ticket_id,
                owner=effective_owner,
                role=effective_role,
                duration_hours=duration_hours,
                base=base_branch,
            )
        self._audit(
            "renew_lock",
            "ok",
            ticket=target_ticket_id,
            details={
                "owner": effective_owner,
                "fencing_token": renewed.get("fencing_token"),
                "distributed": distributed,
                "remote": (remote if distributed else None),
            },
        )
        return self._response(
            {
                "ticket_id": target_ticket_id,
                "lock": renewed,
                "distributed": (distributed_result.get("distributed") if distributed_result else None),
            }
        )

    def lease_heartbeat(
        self,
        ticket_id: str | None = None,
        *,
        owner: str | None = None,
        duration_hours: int = 2,
        distributed: bool = False,
        remote: str = "origin",
    ) -> dict[str, Any]:
        self._begin()
        distributed_result: dict[str, Any] | None = None
        if distributed:
            effective_owner = owner.strip() if isinstance(owner, str) and owner.strip() else str(self.actor)
            target_ticket = ticket_id
            if not target_ticket:
                lock = tickets.ensure_lock(self.repo)
                target_ticket = str(lock.get("ticket_id", "")).strip()
            if not target_ticket:
                raise ExoError(code="LOCK_TICKET_INVALID", message="Active lock missing ticket_id", blocked=True)
            manager = distributed_leases_mod.GitDistributedLeaseManager(self.repo)
            distributed_result = manager.heartbeat(
                target_ticket,
                owner=effective_owner,
                duration_hours=duration_hours,
                remote=remote,
            )
            refreshed = dict(distributed_result.get("lock", {}))
        else:
            refreshed = tickets.heartbeat_lock(
                self.repo, ticket_id=ticket_id, owner=owner, duration_hours=duration_hours
            )
        target_ticket_id = str(refreshed.get("ticket_id", "")).strip() or ticket_id
        self._audit(
            "heartbeat_lock",
            "ok",
            ticket=(target_ticket_id or None),
            details={
                "owner": refreshed.get("owner"),
                "expires_at": refreshed.get("expires_at"),
                "distributed": distributed,
                "remote": (remote if distributed else None),
            },
        )
        return self._response(
            {
                "ticket_id": target_ticket_id,
                "lock": refreshed,
                "distributed": (distributed_result.get("distributed") if distributed_result else None),
            }
        )

    def lease_release(
        self,
        ticket_id: str | None = None,
        *,
        owner: str | None = None,
        distributed: bool = False,
        remote: str = "origin",
        ignore_missing: bool = False,
    ) -> dict[str, Any]:
        self._begin()
        target_ticket = ticket_id
        if not target_ticket:
            lock = tickets.load_lock(self.repo)
            if lock:
                target_ticket = str(lock.get("ticket_id", "")).strip()
        if not target_ticket:
            if ignore_missing:
                return self._response({"ticket_id": None, "released": False, "distributed": None})
            raise ExoError(code="LOCK_REQUIRED", message="No active ticket lock found", blocked=True)

        if distributed:
            manager = distributed_leases_mod.GitDistributedLeaseManager(self.repo)
            out = manager.release(
                target_ticket,
                owner=owner,
                remote=remote,
                ignore_missing=ignore_missing,
            )
            self._audit(
                "release_lock",
                "ok",
                ticket=target_ticket,
                details={"distributed": True, "remote": remote, "released": out.get("released")},
            )
            return self._response(out)

        current = tickets.load_lock(self.repo)
        if not current:
            if ignore_missing:
                return self._response({"ticket_id": target_ticket, "released": False, "distributed": None})
            raise ExoError(code="LOCK_REQUIRED", message="No active ticket lock found", blocked=True)
        if str(current.get("ticket_id", "")).strip() != target_ticket:
            raise ExoError(
                code="LOCK_MISMATCH",
                message=f"Active lock is for {current.get('ticket_id')}, not {target_ticket}",
                details=current,
                blocked=True,
            )
        owner_value = owner.strip() if isinstance(owner, str) and owner.strip() else ""
        current_owner = str(current.get("owner", "")).strip()
        if owner_value and current_owner and owner_value != current_owner:
            raise ExoError(
                code="LOCK_OWNER_MISMATCH",
                message=f"Active lock owner is {current_owner}; release owner {owner_value} is not allowed",
                details=current,
                blocked=True,
            )

        released = tickets.release_lock(self.repo, ticket_id=target_ticket)
        self._audit("release_lock", "ok", ticket=target_ticket, details={"distributed": False, "released": released})
        return self._response({"ticket_id": target_ticket, "released": released, "distributed": None})

    def _load_patch_spec(self, patch_file: str) -> dict[str, Any]:
        path = self._resolve_repo_path(patch_file)
        if not path.exists():
            raise ExoError(code="PATCH_NOT_FOUND", message=f"Patch file not found: {patch_file}")

        data = json.loads(path.read_text(encoding="utf-8")) if path.suffix.lower() == ".json" else load_yaml(path)

        if not isinstance(data, dict):
            raise ExoError(code="PATCH_INVALID", message="Patch file must contain a mapping object")
        patches = data.get("patches")
        if not isinstance(patches, list):
            raise ExoError(code="PATCH_INVALID", message="Patch file must include patches list")
        return data

    def _apply_patches(self, ticket: dict[str, Any], patches_payload: list[dict[str, Any]]) -> list[str]:
        operations: list[tuple[Path, str]] = []
        seen_paths: set[str] = set()
        for patch in patches_payload:
            if not isinstance(patch, dict):
                continue
            raw_path = patch.get("path")
            content = patch.get("content")
            if not isinstance(raw_path, str) or not isinstance(content, str):
                raise ExoError(
                    code="PATCH_INVALID",
                    message="Each patch must include string path and content",
                )
            target = self._resolve_repo_path(raw_path)
            rel = relative_posix(target, self.repo)
            if rel in seen_paths:
                raise ExoError(
                    code="PATCH_INVALID",
                    message=f"Duplicate patch target path: {rel}",
                )
            seen_paths.add(rel)
            operations.append((target, content))

        preview_usage: dict[str, dict[str, Any]] = {}
        for key, value in self._budget_usage.items():
            preview_usage[key] = {
                "files": set(value.get("files", set())),
                "loc": int(value.get("loc", 0)),
            }

        for target, content in operations:
            old = target.read_text(encoding="utf-8") if target.exists() else ""
            is_new_file, loc_delta = self._authorize_write(
                ticket,
                target,
                "write",
                old,
                content,
                usage_state=preview_usage,
            )
            self._commit_budget(
                ticket,
                target,
                is_new_file,
                loc_delta,
                usage_state=preview_usage,
            )

        changed_files: list[str] = []
        for target, content in operations:
            self._write_ticket_file(ticket, target, content)
            changed_files.append(relative_posix(target, self.repo))
        return changed_files

    def do(
        self, ticket_id: str | None = None, *, patch_file: str | None = None, mark_done: bool = True
    ) -> dict[str, Any]:
        self._begin()
        self._verify_integrity()

        ticket, lock = self._current_ticket(ticket_id)
        git_controls = self._git_controls()
        git_enabled = bool(git_controls.get("enabled", True))
        git_ignore_patterns = list(git_controls.get("ignore_paths", []))
        git_branch: dict[str, Any] | None = None
        baseline_snapshot: dict[str, Any] = {"loc_by_path": {}, "actions": {}}

        if git_enabled:
            self._require_git_repo()
            if bool(git_controls.get("enforce_lock_branch", True)):
                git_branch = self._enforce_lock_branch(lock, git_controls)
            else:
                git_branch = {
                    "expected_branch": None,
                    "base_branch": None,
                    "current_branch": self._git_current_branch(),
                    "actions": [],
                }

            baseline_snapshot = self._git_change_snapshot(git_ignore_patterns)
            baseline_actions = baseline_snapshot.get("actions", {})
            baseline_paths = sorted(baseline_actions.keys()) if isinstance(baseline_actions, dict) else []
            if bool(git_controls.get("require_clean_worktree_before_do", True)) and baseline_paths:
                raise ExoError(
                    code="WORKTREE_DIRTY",
                    message="Strict mode requires a clean worktree before exo do",
                    details={"changed_paths": baseline_paths[:100]},
                    blocked=True,
                )

        preflight = {
            "ticket": ticket["id"],
            "scope": ticket.get("scope", {}),
            "budgets": ticket.get("budgets", {}),
            "checks": ticket.get("checks", []),
        }
        if git_enabled:
            base_loc = baseline_snapshot.get("loc_by_path", {})
            base_actions = baseline_snapshot.get("actions", {})
            baseline_loc = (
                sum(int(base_loc.get(path, 0)) for path in base_actions)
                if isinstance(base_loc, dict) and isinstance(base_actions, dict)
                else 0
            )
            preflight["git"] = {
                "enabled": True,
                "branch": git_branch,
                "strict_diff_budgets": bool(git_controls.get("strict_diff_budgets", True)),
                "baseline_files_changed": len(base_actions) if isinstance(base_actions, dict) else 0,
                "baseline_loc_changed": baseline_loc,
                "ignore_paths": git_ignore_patterns,
            }
        self._audit("do_preflight", "ok", ticket=str(ticket["id"]), details=preflight)

        patches: list[dict[str, Any]] = []
        if patch_file:
            patch_spec = self._load_patch_spec(patch_file)
            patches.extend(patch_spec.get("patches", []))

        script_out = self._run_script(
            "do",
            {
                "repo": str(self.repo),
                "ticket": ticket,
                "preflight": preflight,
                "patches": patches,
                "no_llm": self.no_llm,
            },
        )

        script_patches = script_out.get("patches", [])
        if script_patches and not isinstance(script_patches, list):
            raise ExoError(code="DO_SCRIPT_INVALID", message="do script patches must be a list")

        all_patches = list(patches)
        if isinstance(script_patches, list):
            all_patches.extend(script_patches)

        patch_changed_files = self._apply_patches(ticket, all_patches)

        config = self._config()
        commands = script_out.get("commands", [])
        if commands and not isinstance(commands, list):
            raise ExoError(code="DO_SCRIPT_INVALID", message="do script commands must be a list")

        run_results: list[dict[str, Any]] = []
        for command in commands or []:
            run_results.append(
                self._run_allowlisted_command(str(command), list(config.get("do_allowlist", [])), "run_do_command")
            )

        check_out = self._execute_checks(ticket)
        if not check_out.get("passed"):
            raise ExoError(
                code="CHECKS_FAILED",
                message="Ticket checks failed during exo do",
                details={"check_results": check_out.get("results")},
                blocked=True,
            )

        git_budget: dict[str, Any] | None = None
        changed_files = list(patch_changed_files)
        if git_enabled:
            expected_branch = (git_branch or {}).get("expected_branch")
            if expected_branch:
                current_branch = self._git_current_branch()
                if current_branch != expected_branch:
                    raise ExoError(
                        code="BRANCH_CHANGED",
                        message=f"Branch changed during exo do: expected {expected_branch}, got {current_branch}",
                        details={
                            "expected_branch": expected_branch,
                            "current_branch": current_branch,
                        },
                        blocked=True,
                    )

            if bool(git_controls.get("strict_diff_budgets", True)):
                after_snapshot = self._git_change_snapshot(git_ignore_patterns)
                delta_snapshot = self._git_snapshot_delta(baseline_snapshot, after_snapshot)
                self._enforce_git_change_scope(ticket, delta_snapshot)
                git_budget = self._enforce_git_budget(ticket, delta_snapshot)
                changed_files = list(git_budget.get("paths", []))
                self._audit("git_budget", "ok", ticket=str(ticket["id"]), details=git_budget)

        should_mark_done = bool(script_out.get("mark_done", True)) and mark_done
        if should_mark_done:
            lock_data = governance.load_governance_lock(self.repo)
            if governance.has_rule(lock_data, "require_checks") and not check_out.get("passed"):
                raise ExoError(
                    code="CHECK_REQUIRED",
                    message="Governance requires checks before marking done",
                    blocked=True,
                )
            self._mark_ticket_status(ticket, "done")

        distill_path = self.exo_dir / "cache" / "distill" / f"{ticket['id']}.md"
        distill_body = (
            f"# Distill {ticket['id']}\n\n"
            f"Generated: {now_iso()}\n\n"
            "## Summary\n\n"
            f"{script_out.get('summary', 'No summary provided.')}\n\n"
            "## Changed Files\n\n"
            + ("\n".join(f"- {item}" for item in changed_files) if changed_files else "- (none)")
            + "\n\n"
            "## Checks\n\n" + ("- passed\n" if check_out.get("passed") else "- failed\n")
        )
        self._write_system_text(distill_path, distill_body)

        self._audit(
            "do",
            "ok",
            ticket=str(ticket["id"]),
            details={
                "changed_files": changed_files,
                "patch_changed_files": patch_changed_files,
                "git_budget": git_budget,
                "script": script_out.get("_script_source"),
            },
        )

        return self._response(
            {
                "ticket_id": ticket["id"],
                "preflight": preflight,
                "changed_files": changed_files,
                "patch_changed_files": patch_changed_files,
                "commands": run_results,
                "checks": check_out,
                "git_budget": git_budget,
                "distill": str(distill_path.relative_to(self.repo)),
                "marked_done": should_mark_done,
                "script": script_out.get("_script_source"),
            }
        )

    def check(self, ticket_id: str | None = None) -> dict[str, Any]:
        self._begin()
        self._verify_integrity()

        ticket: dict[str, Any] | None
        if ticket_id:
            ticket = tickets.load_ticket(self.repo, ticket_id)
        else:
            lock = tickets.load_lock(self.repo)
            ticket = tickets.load_ticket(self.repo, str(lock["ticket_id"])) if lock else None

        results = self._execute_checks(ticket)
        self._audit("check", "ok" if results.get("passed") else "failed", ticket=(ticket or {}).get("id"))
        return self._response(results, blocked=not bool(results.get("passed")))

    def jot(self, line: str) -> dict[str, Any]:
        self._begin()
        self._require_scaffold()
        result = scratchpad.append_jot(self.repo, line)
        self._audit("jot", "ok", details={"line": line})
        return self._response(result)

    def thread(self, topic: str) -> dict[str, Any]:
        self._begin()
        self._require_scaffold()
        result = scratchpad.create_thread(self.repo, topic)
        self._audit("thread", "ok", details={"topic": topic, "thread_id": result.get("thread_id")})
        return self._response(result)

    def promote(self, thread_id: str, *, to: str = "ticket") -> dict[str, Any]:
        self._begin()
        self._require_scaffold()
        if to != "ticket":
            raise ExoError(code="PROMOTE_TARGET_INVALID", message="Only --to ticket is supported in v0.1")

        try:
            thread_path, body = scratchpad.load_thread(self.repo, thread_id)
        except FileNotFoundError:
            raise ExoError(code="THREAD_NOT_FOUND", message=f"Thread not found: {thread_id}") from None

        title = "Promoted thread"
        for line in body.splitlines():
            if line.startswith("# Thread:"):
                title = line.replace("# Thread:", "").strip()
                break

        ticket_id = tickets.allocate_ticket_id(self.repo)
        ticket = {
            "id": ticket_id,
            "type": "docs",
            "title": f"Promote: {title}",
            "status": "todo",
            "priority": 3,
            "parent_id": None,
            "spec_ref": None,
            "scope": {
                "allow": ["docs/**", ".exo/**", "README.md"],
                "deny": [".env*", "**/.ssh/**", "**/.aws/**", ".git/**"],
            },
            "budgets": {
                "max_files_changed": 6,
                "max_loc_changed": 200,
            },
            "checks": [],
            "notes": [f"Promoted from {thread_id}"],
            "blockers": [],
            "labels": ["promoted", "thread"],
            "created_at": now_iso(),
        }
        path = tickets.save_ticket(self.repo, ticket)

        marker = f"\n\nPromoted to {ticket_id} at {now_iso()}\n"
        thread_path.write_text(body + marker, encoding="utf-8")

        self._audit("promote", "ok", ticket=ticket_id, path=path)

        # Advisory sidecar commit
        try:
            from exo.stdlib.sidecar import commit_sidecar

            commit_sidecar(self.repo, message=f"chore(exo): promote {thread_id} → {ticket_id}")
        except Exception:
            pass

        return self._response(
            {
                "thread_id": thread_id,
                "ticket_id": ticket_id,
                "ticket_path": str(path.relative_to(self.repo)),
            }
        )

    def observe(
        self,
        ticket_id: str,
        tag: str,
        msg: str,
        *,
        triggers: list[str] | None = None,
        confidence: str = "high",
    ) -> dict[str, Any]:
        self._begin()
        self._require_scaffold()
        self._ensure_evolution_layout()
        ticket = tickets.load_ticket(self.repo, ticket_id)

        obs_id = evolution.next_observation_id(self.repo)
        path = evolution.observation_path(self.repo, obs_id)
        trigger_items = [item.strip() for item in (triggers or []) if str(item).strip()]
        if not trigger_items:
            trigger_items = ["manual"]
        tags = [tag.strip()] if tag.strip() else []
        confidence_norm = confidence.strip().lower()
        if confidence_norm not in evolution.CONFIDENCE_VALUES:
            confidence_norm = "high"

        trigger_block = "\n".join(f"  - {item}" for item in trigger_items)
        tag_block = "\n".join(f"  - {item}" for item in tags) if tags else "  - untagged"
        body = (
            "---\n"
            f"id: {obs_id}\n"
            f"timestamp: {now_iso()}\n"
            f"ticket: {ticket.get('id')}\n"
            f"actor: {self.actor}\n"
            "trigger:\n"
            f"{trigger_block}\n"
            "tags:\n"
            f"{tag_block}\n"
            f"confidence: {confidence_norm}\n"
            "---\n\n"
            "## What happened\n"
            f"{msg.strip()}\n\n"
            "## Evidence\n"
            "- audit log lines: pending\n"
            "- command: pending\n\n"
            "## Immediate outcome\n"
            "Pending classification.\n\n"
            "## Notes\n"
            "Pure observation. No interpretation or fix proposed here.\n"
        )
        self._write_system_text(path, body)
        self._audit(
            "observe",
            "ok",
            ticket=str(ticket.get("id")),
            details={"observation_id": obs_id, "tag": tag, "triggers": trigger_items},
        )
        return self._response(
            {
                "observation_id": obs_id,
                "path": str(path.relative_to(self.repo)),
                "ticket": ticket.get("id"),
                "tag": tag,
                "triggers": trigger_items,
                "confidence": confidence_norm,
            }
        )

    def propose(
        self,
        ticket_id: str,
        kind: str,
        symptom: str | list[str],
        root_cause: str,
        *,
        summary: str | None = None,
        expected_effect: str | list[str] | None = None,
        risk_level: str = "medium",
        blast_radius: list[str] | None = None,
        rollback_type: str = "delete_file",
        rollback_path: str | None = None,
        proposed_change_type: str = "patch_file",
        proposed_change_path: str | None = None,
        evidence_observations: list[str] | None = None,
        evidence_audit_ranges: list[str] | None = None,
        notes: list[str] | None = None,
        requires_approvals: int = 1,
        human_required: bool | None = None,
        patch_file: str | None = None,
    ) -> dict[str, Any]:
        self._begin()
        self._verify_integrity()
        self._ensure_evolution_layout()
        ticket = tickets.load_ticket(self.repo, ticket_id)

        normalized_kind = kind.strip().lower()
        if normalized_kind not in evolution.KIND_VALUES:
            raise ExoError(
                code="PROPOSAL_KIND_INVALID",
                message=f"Invalid proposal kind: {kind}",
            )

        proposal_id = evolution.next_proposal_id(self.repo)
        proposal_path = evolution.proposal_path(self.repo, proposal_id)

        patch_path: Path
        should_create_patch = False
        if patch_file:
            patch_path = self._resolve_repo_path(patch_file)
            rel_patch = relative_posix(patch_path, self.repo)
            if not rel_patch.startswith(".exo/patches/"):
                raise ExoError(
                    code="PATCH_PATH_INVALID",
                    message="Proposal patch file must live under .exo/patches/",
                    blocked=True,
                )
            should_create_patch = not patch_path.exists()
        else:
            patch_id = evolution.patch_id_for_proposal(proposal_id)
            patch_path = evolution.patch_path(self.repo, patch_id)
            should_create_patch = not patch_path.exists()
        patch_ref = str(patch_path.relative_to(self.repo))

        symptom_list: list[str]
        if isinstance(symptom, list):
            symptom_list = [str(item).strip() for item in symptom if str(item).strip()]
        else:
            symptom_list = [str(symptom).strip()] if str(symptom).strip() else []
        if not symptom_list:
            raise ExoError(code="PROPOSAL_INVALID", message="Proposal requires at least one symptom")

        if isinstance(expected_effect, list):
            expected_effect_list = [str(item).strip() for item in expected_effect if str(item).strip()]
        elif isinstance(expected_effect, str) and expected_effect.strip():
            expected_effect_list = [expected_effect.strip()]
        else:
            expected_effect_list = []

        evidence_obs = [str(item).strip() for item in (evidence_observations or []) if str(item).strip()]
        evidence_audit = [str(item).strip() for item in (evidence_audit_ranges or []) if str(item).strip()]
        notes_list = [str(item).strip() for item in (notes or []) if str(item).strip()]
        blast = [str(item).strip() for item in (blast_radius or []) if str(item).strip()]
        if not blast:
            blast = [f"{normalized_kind}_only"]

        proposal_summary = (summary or "").strip()
        if not proposal_summary:
            proposal_summary = f"{normalized_kind}: {symptom_list[0]}"
        risk_norm = risk_level.strip().lower() if risk_level else "medium"
        if risk_norm not in evolution.RISK_VALUES:
            risk_norm = "medium"

        requires_human = (
            bool(human_required) if human_required is not None else (normalized_kind == "governance_change")
        )
        change_path = (
            proposed_change_path.strip()
            if isinstance(proposed_change_path, str) and proposed_change_path.strip()
            else patch_ref
        )
        rollback_path_value = (
            rollback_path.strip() if isinstance(rollback_path, str) and rollback_path.strip() else change_path
        )

        proposal = {
            "id": proposal_id,
            "created_at": now_iso(),
            "author": self.actor,
            "ticket": str(ticket.get("id")),
            "kind": normalized_kind,
            "status": "draft",
            "summary": proposal_summary,
            "symptom": symptom_list,
            "root_cause": root_cause.strip(),
            "proposed_change": {
                "type": proposed_change_type.strip() if proposed_change_type.strip() else "patch_file",
                "path": change_path,
            },
            "expected_effect": expected_effect_list,
            "risk_level": risk_norm,
            "blast_radius": blast,
            "rollback": {
                "type": rollback_type.strip() if rollback_type.strip() else "delete_file",
                "path": rollback_path_value,
            },
            "evidence": {
                "observations": evidence_obs,
                "audit_log_ranges": evidence_audit,
            },
            "requires": {
                "approvals": max(int(requires_approvals), 1),
                "human_required": requires_human,
            },
            "notes": notes_list,
            "approvals": [],
        }
        self._validate_proposal_schema(proposal)
        if should_create_patch:
            self._write_system_yaml(patch_path, evolution.patch_placeholder())
        self._write_system_yaml(proposal_path, proposal)

        gate = self._proposal_gate_summary(proposal)
        self._audit(
            "propose",
            "ok",
            ticket=str(ticket.get("id")),
            details={
                "proposal_id": proposal_id,
                "kind": normalized_kind,
                "patch_ref": patch_ref,
                "summary": proposal_summary,
            },
        )
        return self._response(
            {
                "proposal_id": proposal_id,
                "proposal_path": str(proposal_path.relative_to(self.repo)),
                "patch_ref": patch_ref,
                "ticket": ticket.get("id"),
                "gate": gate,
            }
        )

    def approve(self, proposal_id: str, *, decision: str = "approved", note: str = "") -> dict[str, Any]:
        self._begin()
        self._verify_integrity()
        self._ensure_evolution_layout()

        proposal, proposal_path = self._load_proposal(proposal_id)
        normalized_decision = decision.strip().lower()
        if normalized_decision not in {"approved", "rejected"}:
            raise ExoError(code="REVIEW_DECISION_INVALID", message="Decision must be approved or rejected")

        review_id = evolution.next_review_id(self.repo, str(proposal.get("id")))
        review_path = evolution.review_path(self.repo, review_id)
        reviewer_type = evolution.actor_type(self.actor)

        review = {
            "review_id": review_id,
            "decision": normalized_decision,
            "reviewer": self.actor,
            "reviewer_type": reviewer_type,
            "note": note.strip(),
            "created_at": now_iso(),
        }

        review_body = (
            "---\n"
            f"review_id: {review_id}\n"
            f"proposal: {proposal.get('id')}\n"
            f"reviewer: {self.actor}\n"
            f"timestamp: {review.get('created_at')}\n"
            f"decision: {'approve' if normalized_decision == 'approved' else 'reject'}\n"
            "confidence: high\n"
            "---\n\n"
            "## Rationale\n"
            f"{note.strip() or 'No rationale provided.'}\n\n"
            "## Conditions\n"
            "None.\n\n"
            "## Signature\n"
            f"{self.actor}\n"
        )
        self._write_system_text(review_path, review_body)

        approvals = proposal.get("approvals")
        if not isinstance(approvals, list):
            approvals = []
        approvals.append(review)
        proposal["approvals"] = approvals
        proposal["updated_at"] = now_iso()

        gate = self._proposal_gate_summary(proposal)
        if normalized_decision == "rejected":
            proposal["status"] = "rejected"
        elif gate.get("ready"):
            proposal["status"] = "approved"
        else:
            proposal["status"] = "proposed"

        self._validate_proposal_schema(proposal)
        self._write_system_yaml(proposal_path, proposal)
        self._audit(
            "approve_proposal",
            "ok",
            ticket=str(proposal.get("ticket")),
            details={
                "proposal_id": proposal.get("id"),
                "review_id": review_id,
                "decision": normalized_decision,
                "gate_ready": gate.get("ready"),
            },
        )
        return self._response(
            {
                "proposal_id": proposal.get("id"),
                "proposal_status": proposal.get("status"),
                "review_id": review_id,
                "review_path": str(review_path.relative_to(self.repo)),
                "gate": gate,
            }
        )

    def apply_proposal(self, proposal_id: str) -> dict[str, Any]:
        self._begin()
        self._verify_integrity()
        self._ensure_evolution_layout()

        proposal, proposal_path = self._load_proposal(proposal_id)
        kind = str(proposal.get("kind", "")).strip().lower()
        if kind not in evolution.KIND_VALUES:
            raise ExoError(code="PROPOSAL_KIND_INVALID", message=f"Invalid proposal kind: {kind}")

        gate = self._proposal_gate_summary(proposal)
        if not gate.get("ready"):
            raise ExoError(
                code="PROPOSAL_NOT_APPROVED",
                message="Proposal does not satisfy approval gate",
                details={"gate": gate},
                blocked=True,
            )

        ticket_id = str(proposal.get("ticket", "")).strip()
        if not ticket_id:
            raise ExoError(code="PROPOSAL_INVALID", message="Proposal missing ticket field")
        ticket = tickets.load_ticket(self.repo, ticket_id)
        _ = tickets.ensure_lock(self.repo, ticket_id)
        self._validate_evolution_ticket(ticket, kind)

        proposed_change = proposal.get("proposed_change")
        if not isinstance(proposed_change, dict):
            raise ExoError(code="PROPOSAL_INVALID", message="Proposal missing proposed_change object")

        patch_ref = evolution.default_patch_ref_for_proposal(str(proposal.get("id")))
        change_type = str(proposed_change.get("type", "")).strip()
        change_path = str(proposed_change.get("path", "")).strip()
        if change_type == "patch_file" and change_path.startswith(".exo/patches/"):
            patch_ref = change_path
        if not patch_ref:
            raise ExoError(code="PROPOSAL_INVALID", message="Proposal patch artifact reference missing")

        patch_spec = self._load_patch_spec(patch_ref)
        patches_payload = patch_spec.get("patches", [])
        if not isinstance(patches_payload, list) or not patches_payload:
            raise ExoError(code="PATCH_EMPTY", message="Patch artifact contains no patches", blocked=True)

        patch_paths: list[str] = []
        for item in patches_payload:
            if not isinstance(item, dict):
                continue
            path_value = item.get("path")
            if isinstance(path_value, str):
                normalized = path_value.replace("\\\\", "/")
                if normalized.startswith("./"):
                    normalized = normalized[2:]
                patch_paths.append(normalized)

        if kind == "governance_change":
            if ".exo/CONSTITUTION.md" not in patch_paths:
                raise ExoError(
                    code="GOVERNANCE_PATCH_INVALID",
                    message="governance_change must patch .exo/CONSTITUTION.md",
                    blocked=True,
                )
        elif kind == "practice_change":
            if not any(path.startswith(".exo/practices/") or path.startswith(".exo/roles/") for path in patch_paths):
                raise ExoError(
                    code="PRACTICE_PATCH_INVALID",
                    message="practice_change must patch .exo/practices/* or .exo/roles/*",
                    blocked=True,
                )
        elif kind == "tooling_change" and not any(path.startswith(".exo/scripts/") for path in patch_paths):
            raise ExoError(
                code="TOOLING_PATCH_INVALID",
                message="tooling_change must patch .exo/scripts/*",
                blocked=True,
            )

        if kind == "governance_change":
            if int(gate.get("human_approved_count", 0)) < 1:
                raise ExoError(
                    code="HUMAN_APPROVAL_REQUIRED",
                    message="governance_change requires at least one human approval",
                    blocked=True,
                )
            self._enforce_governance_cooldown()

        changed_files = self._apply_patches(ticket, patches_payload)
        check_out = self._execute_checks(ticket)
        if not check_out.get("passed"):
            raise ExoError(
                code="CHECKS_FAILED",
                message="Proposal checks failed",
                details={"check_results": check_out.get("results")},
                blocked=True,
            )

        governance_recompiled = False
        if kind == "governance_change" or ".exo/CONSTITUTION.md" in changed_files:
            lock_data = governance.compile_constitution(self.repo)
            governance_recompiled = True
            self._audit(
                "build_governance",
                "ok",
                details={"source_hash": lock_data.get("source_hash"), "source": "apply_proposal"},
            )
            _ = governance.verify_integrity(self.repo)

        proposal["status"] = "applied"
        proposal["applied_at"] = now_iso()
        proposal["applied_by"] = self.actor
        proposal["applied_files"] = changed_files
        proposal["updated_at"] = now_iso()
        self._validate_proposal_schema(proposal)
        self._write_system_yaml(proposal_path, proposal)

        self._audit(
            "apply_proposal",
            "ok",
            ticket=ticket_id,
            details={
                "proposal_id": proposal.get("id"),
                "kind": kind,
                "patch_ref": patch_ref,
                "changed_files": changed_files,
                "governance_recompiled": governance_recompiled,
            },
        )
        return self._response(
            {
                "proposal_id": proposal.get("id"),
                "ticket": ticket_id,
                "kind": kind,
                "changed_files": changed_files,
                "checks": check_out,
                "governance_recompiled": governance_recompiled,
            }
        )

    def distill(self, proposal_id: str, *, statement: str | None = None, confidence: float = 0.7) -> dict[str, Any]:
        self._begin()
        self._require_scaffold()
        self._ensure_evolution_layout()

        proposal, proposal_path = self._load_proposal(proposal_id)
        if proposal.get("status") != "applied":
            raise ExoError(
                code="PROPOSAL_NOT_APPLIED",
                message="Proposal must be applied before distill",
                blocked=True,
            )

        proposal_id_norm = str(proposal.get("id"))
        kind = str(proposal.get("kind", ""))
        lesson_path: Path
        if kind == "practice_change":
            lesson_path = self.repo / ".exo/practices" / f"LESSON-{proposal_id_norm}.md"
        else:
            lesson_path = self.repo / ".exo/scratchpad/threads" / f"lesson-{proposal_id_norm.lower()}.md"

        lesson_body = (
            f"# Lesson {proposal_id_norm}\n\n"
            f"- proposal: {proposal_id_norm}\n"
            f"- kind: {kind}\n"
            f"- distilled_at: {now_iso()}\n\n"
            "## Symptom\n\n" + "\\n".join(f"- {item}" for item in (proposal.get("symptom") or [])) + "\n\n"
            "## Root Cause\n\n"
            f"{proposal.get('root_cause', '')}\n\n"
            "## Expected Effect\n\n"
            + "\\n".join(f"- {item}" for item in (proposal.get("expected_effect") or []))
            + "\n"
        )
        self._write_system_text(lesson_path, lesson_body)

        index = evolution.load_memory_index(self.repo)
        mem_id = evolution.next_memory_id(index, "MEM")
        fm_id = evolution.next_memory_id(index, "FM")
        effect_list = proposal.get("expected_effect") if isinstance(proposal.get("expected_effect"), list) else []
        symptom_list = proposal.get("symptom") if isinstance(proposal.get("symptom"), list) else []
        default_statement = (
            str(effect_list[0]).strip() if effect_list else (str(symptom_list[0]).strip() if symptom_list else "")
        )
        statement_value = (statement or default_statement).strip()
        if not statement_value:
            statement_value = f"Apply lesson from {proposal_id_norm}"

        rules = index.get("rules_of_thumb")
        if not isinstance(rules, list):
            rules = []
        evidence = proposal.get("evidence") if isinstance(proposal.get("evidence"), dict) else {}
        observations = evidence.get("observations") if isinstance(evidence.get("observations"), list) else []
        source_observation = str(observations[0]) if observations else None
        rules.append(
            {
                "id": mem_id,
                "statement": statement_value,
                "source": {
                    "proposal": proposal_id_norm,
                    "observation": source_observation,
                },
                "applies_to": {"repos": ["*"]},
                "confidence": round(float(confidence), 2),
                "status": "active",
                "last_verified": datetime.now().astimezone().date().isoformat(),
            }
        )
        index["rules_of_thumb"] = rules

        failure_modes = index.get("failure_modes")
        if not isinstance(failure_modes, list):
            failure_modes = []
        change_path = ""
        proposed_change = proposal.get("proposed_change")
        if isinstance(proposed_change, dict):
            change_path = str(proposed_change.get("path", ""))
        failure_modes.append(
            {
                "id": fm_id,
                "name": str(symptom_list[0] if symptom_list else "Unnamed failure mode")[:120],
                "detection": {
                    "signal": str(symptom_list[0] if symptom_list else ""),
                    "source": "audit.log.jsonl",
                },
                "mitigation": {
                    "practice": change_path or statement_value,
                },
                "severity": "medium",
                "frequency": "recurring",
            }
        )
        index["failure_modes"] = failure_modes
        index["version"] = str(index.get("version", "0.1"))
        index["last_updated"] = datetime.now().astimezone().date().isoformat()

        memory_path = self.repo / evolution.MEMORY_INDEX_PATH
        self._write_system_yaml(memory_path, index)

        proposal["distilled_at"] = now_iso()
        proposal["distilled_by"] = self.actor
        proposal["updated_at"] = now_iso()
        self._validate_proposal_schema(proposal)
        self._write_system_yaml(proposal_path, proposal)

        self._audit(
            "distill_proposal",
            "ok",
            ticket=str(proposal.get("ticket")),
            details={
                "proposal_id": proposal_id_norm,
                "memory_id": mem_id,
                "failure_mode_id": fm_id,
            },
        )
        return self._response(
            {
                "proposal_id": proposal_id_norm,
                "lesson_path": str(lesson_path.relative_to(self.repo)),
                "memory_index_path": str(memory_path.relative_to(self.repo)),
                "memory_id": mem_id,
                "failure_mode_id": fm_id,
            }
        )

    def recall(self, query: str) -> dict[str, Any]:
        self._begin()
        self._require_scaffold()
        config = self._config()
        paths = config.get("recall_paths", [".exo", "docs"])
        if not isinstance(paths, list):
            paths = [".exo", "docs"]

        data = recall_mod.recall(self.repo, query, [str(p) for p in paths])
        self._audit("recall", "ok", details={"query": query, "hits": data.get("counts", {})})
        return self._response(data)

    def subscribe(
        self,
        topic_id: str | None = None,
        *,
        since_cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        self._begin()
        self._require_scaffold()
        topic = topic_id.strip() if isinstance(topic_id, str) and topic_id.strip() else None
        try:
            limit_value = max(int(limit), 1)
        except (TypeError, ValueError):
            raise ExoError(
                code="SUBSCRIBE_LIMIT_INVALID", message="limit must be a positive integer", blocked=True
            ) from None
        data = ledger.subscribe(
            self.repo,
            topic_id=topic,
            since_cursor=since_cursor,
            limit=limit_value,
        )
        self._audit(
            "subscribe",
            "ok",
            details={"topic_id": topic, "count": data.get("count", 0), "since_cursor": since_cursor},
        )
        return self._response(data)

    def ack(self, ref_id: str, *, required: int = 1) -> dict[str, Any]:
        self._begin()
        self._require_scaffold()
        ref = ref_id.strip()
        if not ref:
            raise ExoError(code="ACK_REF_INVALID", message="ref_id is required", blocked=True)

        ack_ref = ledger.acked(self.repo, actor_id=self.actor, ref_id=ref)
        quorum = ledger.ack_status(self.repo, ref_id=ref, required=required)
        self._audit(
            "ack",
            "ok",
            details={"ref_id": ref, "required": int(required), "satisfied": quorum.get("satisfied")},
        )
        return self._response(
            {
                "ack_record": {
                    "log_path": ack_ref.log_path,
                    "line": ack_ref.line,
                    "record_hash": ack_ref.record_hash,
                    "record_type": ack_ref.record_type,
                    "ref_id": ack_ref.ref_id,
                },
                "quorum": quorum,
            }
        )

    def quorum(self, ref_id: str, *, required: int = 1) -> dict[str, Any]:
        self._begin()
        self._require_scaffold()
        ref = ref_id.strip()
        if not ref:
            raise ExoError(code="ACK_REF_INVALID", message="ref_id is required", blocked=True)

        status = ledger.ack_status(self.repo, ref_id=ref, required=required)
        self._audit(
            "quorum",
            "ok",
            details={"ref_id": ref, "required": int(required), "satisfied": status.get("satisfied")},
        )
        return self._response({"quorum": status})

    def head(self, topic_id: str) -> dict[str, Any]:
        self._begin()
        self._require_scaffold()
        topic = topic_id.strip()
        if not topic:
            raise ExoError(code="TOPIC_ID_INVALID", message="topic_id is required", blocked=True)

        current = ledger.head(self.repo, topic)
        self._audit("head", "ok", details={"topic_id": topic, "head": current})
        return self._response({"topic_id": topic, "head": current})

    def cas_head(
        self,
        topic_id: str,
        *,
        expected_ref: str | None,
        new_ref: str | None,
        max_attempts: int = 1,
        control_cap: str,
    ) -> dict[str, Any]:
        self._begin()
        self._require_scaffold()

        topic = topic_id.strip()
        if not topic:
            raise ExoError(code="TOPIC_ID_INVALID", message="topic_id is required", blocked=True)
        granted_cap = self._require_control_cap("cas_head", control_cap)
        try:
            attempts = max(int(max_attempts), 1)
        except (TypeError, ValueError):
            raise ExoError(
                code="CAS_ATTEMPTS_INVALID", message="max_attempts must be a positive integer", blocked=True
            ) from None
        result = ledger.cas_head_retry(
            self.repo,
            topic,
            expected_ref,
            new_ref,
            max_attempts=attempts,
        )

        if not bool(result.get("ok")):
            self._audit(
                "cas_head",
                "failed",
                details={
                    "topic_id": topic,
                    "attempts": result.get("attempts"),
                    "expected_ref": expected_ref,
                    "actual_ref": result.get("head"),
                    "capability": granted_cap,
                },
            )
            raise ExoError(
                code="CAS_HEAD_CONFLICT",
                message=f"CAS head conflict for topic {topic}",
                details=result,
                blocked=True,
            )

        audit_ref = self._audit(
            "cas_head",
            "ok",
            details={
                "topic_id": topic,
                "attempts": result.get("attempts"),
                "expected_ref": expected_ref,
                "new_ref": new_ref,
                "applied_head": result.get("head"),
                "capability": granted_cap,
            },
        )
        receipt = self._build_control_receipt(
            action={
                "kind": "cas_head",
                "target": topic,
                "params": {
                    "expected_ref": expected_ref,
                    "new_ref": new_ref,
                    "max_attempts": attempts,
                    "control_cap": granted_cap,
                },
                "mode": "execute",
            },
            result={
                "decision": {"status": "ALLOW"},
                "plan": {"steps": [{"op": "cas_head_retry"}, {"op": "append_audit"}]},
            },
            audit_refs=[audit_ref],
            ticket_id=f"HEAD-{topic}",
        )
        return self._response(
            {
                "topic_id": topic,
                "head": result.get("head"),
                "attempts": result.get("attempts"),
                "history": result.get("history", []),
                "receipt": receipt,
            }
        )

    def decide_override(
        self,
        intent_id: str,
        *,
        override_cap: str,
        rationale_ref: str,
        outcome: str = "ALLOW",
    ) -> dict[str, Any]:
        self._begin()
        self._require_scaffold()
        lock_data = self._verify_integrity()

        intent = intent_id.strip()
        if not intent:
            raise ExoError(code="INTENT_ID_INVALID", message="intent_id is required", blocked=True)
        if not ledger.read_records(self.repo, record_type="IntentSubmitted", intent_id=intent, limit=1):
            raise ExoError(
                code="INTENT_NOT_FOUND",
                message=f"Intent not found in ledger: {intent}",
                details={"intent_id": intent},
                blocked=True,
            )

        granted_cap = self._require_control_cap("decide_override", override_cap)
        rationale = self._resolve_reference(rationale_ref)
        normalized_outcome = outcome.strip().upper()
        allowed_outcomes = {"ALLOW", "DENY", "ESCALATE", "SANDBOX"}
        if normalized_outcome not in allowed_outcomes:
            raise ExoError(
                code="OVERRIDE_OUTCOME_INVALID",
                message=f"override outcome must be one of {sorted(allowed_outcomes)}",
                blocked=True,
            )

        decision_id = f"DEC-OVR-{datetime.now().astimezone().strftime('%Y%m%d%H%M%S%f')}"
        decision_id = f"{decision_id}-{intent[-6:].upper()}"
        decision_reasons = [
            f"override decision issued by {self.actor}",
            f"override capability validated: {granted_cap}",
            f"rationale reference: {rationale['kind']}:{rationale['value']}",
        ]
        decision_ref = ledger.decision_recorded(
            self.repo,
            decision_id=decision_id,
            intent_id=intent,
            policy_version=str(lock_data.get("version", "0.1")),
            outcome=normalized_outcome,
            reasons_hash=ledger.payload_hash(decision_reasons),
            reasons=decision_reasons,
            constraints={
                "override": {
                    "actor": self.actor,
                    "capability": granted_cap,
                    "rationale": rationale,
                    "issued_at": now_iso(),
                }
            },
        )

        audit_ref = self._audit(
            "decide_override",
            "ok",
            ticket=intent,
            details={
                "decision_id": decision_id,
                "outcome": normalized_outcome,
                "capability": granted_cap,
                "rationale": rationale,
            },
        )
        receipt = self._build_control_receipt(
            action={
                "kind": "decide_override",
                "target": intent,
                "params": {
                    "decision_id": decision_id,
                    "override_cap": granted_cap,
                    "rationale_ref": rationale,
                    "outcome": normalized_outcome,
                },
                "mode": "execute",
            },
            result={
                "decision": {"status": normalized_outcome},
                "plan": {"steps": [{"op": "record_override_decision"}, {"op": "append_audit"}]},
            },
            audit_refs=[audit_ref],
            ticket_id=intent,
        )

        return self._response(
            {
                "intent_id": intent,
                "decision_id": decision_id,
                "decision_record": {
                    "log_path": decision_ref.log_path,
                    "line": decision_ref.line,
                    "record_hash": decision_ref.record_hash,
                    "record_type": decision_ref.record_type,
                },
                "receipt": receipt,
            }
        )

    def policy_set(
        self,
        *,
        policy_cap: str,
        policy_bundle: str | None = None,
        version: str | None = None,
    ) -> dict[str, Any]:
        self._begin()
        self._require_scaffold()
        _ = self._verify_integrity()

        granted_cap = self._require_control_cap("policy_set", policy_cap)
        lock = tickets.ensure_lock(self.repo)
        ticket_id = str(lock.get("ticket_id", "")).strip()
        if not ticket_id:
            raise ExoError(code="LOCK_TICKET_INVALID", message="Active lock missing ticket_id", blocked=True)
        ticket = tickets.load_ticket(self.repo, ticket_id)
        ticket_type = str(ticket.get("type", "")).strip().lower()
        if ticket_type != "governance" and not ticket_id.startswith("GOV-"):
            raise ExoError(
                code="POLICY_TICKET_REQUIRED",
                message="policy_set requires a governance ticket lock (type=governance or GOV-*)",
                details={"ticket_id": ticket_id, "ticket_type": ticket_type},
                blocked=True,
            )

        constitution_path = self.exo_dir / "CONSTITUTION.md"
        bundle_ref = str(constitution_path.relative_to(self.repo))
        if isinstance(policy_bundle, str) and policy_bundle.strip():
            bundle_path = self._resolve_repo_path(policy_bundle.strip())
            if not bundle_path.exists():
                raise ExoError(
                    code="POLICY_BUNDLE_NOT_FOUND",
                    message=f"Policy bundle not found: {policy_bundle}",
                    blocked=True,
                )
            content = bundle_path.read_text(encoding="utf-8")
            self._write_system_text(constitution_path, content)
            bundle_ref = relative_posix(bundle_path, self.repo)

        current_lock = governance.load_governance_lock(self.repo)
        target_version = (
            version.strip() if isinstance(version, str) and version.strip() else str(current_lock.get("version", "0.1"))
        )
        lock_data = governance.compile_constitution(self.repo, version=target_version)

        audit_ref = self._audit(
            "policy_set",
            "ok",
            ticket=ticket_id,
            details={
                "policy_version": target_version,
                "source_hash": lock_data.get("source_hash"),
                "bundle": bundle_ref,
                "capability": granted_cap,
            },
        )
        receipt = self._build_control_receipt(
            action={
                "kind": "policy_set",
                "target": str((self.exo_dir / "governance.lock.json").relative_to(self.repo)),
                "params": {
                    "policy_cap": granted_cap,
                    "bundle": bundle_ref,
                    "version": target_version,
                },
                "mode": "execute",
            },
            result={
                "decision": {"status": "ALLOW"},
                "plan": {"steps": [{"op": "compile_constitution"}, {"op": "append_audit"}]},
            },
            audit_refs=[audit_ref],
            ticket_id=ticket_id,
        )

        return self._response(
            {
                "policy_version": str(lock_data.get("version")),
                "source_hash": str(lock_data.get("source_hash")),
                "ticket_id": ticket_id,
                "bundle": bundle_ref,
                "receipt": receipt,
            }
        )
