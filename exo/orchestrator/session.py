from __future__ import annotations

import contextlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from exo.kernel import tickets
from exo.kernel.audit import append_audit, event_template
from exo.kernel.errors import ExoError
from exo.kernel.utils import ensure_dir, now_iso, relative_posix
from exo.stdlib import distributed_leases
from exo.stdlib.engine import KernelEngine

try:
    import fcntl as _fcntl
except Exception:  # pragma: no cover - platform-specific
    _fcntl = None


SESSION_CACHE_DIR = Path(".exo/cache/sessions")
SESSION_MEMORY_DIR = Path(".exo/memory/sessions")
SESSION_SUSPENDED_DIR = Path(".exo/memory/suspended")
SESSION_INDEX_PATH = SESSION_MEMORY_DIR / "index.jsonl"
SESSION_LOG_PATH = Path(".exo/logs/session.log.jsonl")
SESSION_LOG_LOCK = Path(".exo/logs/session.log.lock")


def _safe_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    return token or "agent"


def _session_id() -> str:
    now = datetime.now().astimezone()
    return f"SES-{now.strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8].upper()}"


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    line = json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")


@contextlib.contextmanager
def _session_log_lock(repo: Path):
    lock_path = repo / SESSION_LOG_LOCK
    ensure_dir(lock_path.parent)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        yield
    finally:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        handle.close()


def _last_memento_summary(repo: Path, ticket_id: str) -> dict[str, Any] | None:
    path = repo / SESSION_INDEX_PATH
    if not path.exists():
        return None
    latest: dict[str, Any] | None = None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            if str(item.get("ticket_id", "")).strip() != ticket_id:
                continue
            latest = item
    return latest


class AgentSessionManager:
    """Agent bootstrap/closeout lifecycle for bounded-context multi-vendor sessions."""

    def __init__(self, root: Path | str, *, actor: str) -> None:
        self.root = Path(root).resolve()
        self.actor = actor
        self.actor_token = _safe_token(actor)

    @property
    def active_session_path(self) -> Path:
        return self.root / SESSION_CACHE_DIR / f"{self.actor_token}.active.json"

    @property
    def bootstrap_path(self) -> Path:
        return self.root / SESSION_CACHE_DIR / f"{self.actor_token}.bootstrap.md"

    def _log_event(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        payload.setdefault("ts", now_iso())
        payload.setdefault("actor", self.actor)
        with _session_log_lock(self.root):
            _append_jsonl(self.root / SESSION_LOG_PATH, payload)

    def _load_active(self) -> dict[str, Any] | None:
        if not self.active_session_path.exists():
            return None
        raw = json.loads(self.active_session_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return raw

    def _write_active(self, payload: dict[str, Any]) -> None:
        ensure_dir(self.active_session_path.parent)
        self.active_session_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")

    def start(
        self,
        *,
        ticket_id: str | None = None,
        vendor: str = "unknown",
        model: str = "unknown",
        context_window_tokens: int | None = None,
        role: str | None = None,
        task: str | None = None,
        topic_id: str | None = None,
        acquire_lock: bool = False,
        distributed: bool = False,
        remote: str = "origin",
        duration_hours: int = 2,
    ) -> dict[str, Any]:
        existing = self._load_active()
        if existing:
            age = _session_age_hours(existing)
            if age >= 48.0:
                self._log_event(
                    {
                        "event": "session_stale_evict",
                        "session_id": existing.get("session_id", ""),
                        "ticket_id": existing.get("ticket_id", ""),
                        "age_hours": round(age, 2),
                    }
                )
                append_audit(
                    self.root,
                    event_template(
                        actor=self.actor,
                        action="session_stale_evict",
                        result="ok",
                        ticket=str(existing.get("ticket_id", "")).strip() or None,
                        details={
                            "session_id": existing.get("session_id", ""),
                            "age_hours": round(age, 2),
                        },
                    ),
                )
                self.active_session_path.unlink(missing_ok=True)
            else:
                existing_ticket = str(existing.get("ticket_id", "")).strip()
                target_ticket = str(ticket_id).strip() if isinstance(ticket_id, str) and ticket_id.strip() else existing_ticket
                if existing_ticket and target_ticket and existing_ticket != target_ticket:
                    raise ExoError(
                        code="SESSION_ALREADY_ACTIVE",
                        message=f"Actor {self.actor} already has active session for {existing_ticket}",
                        details={"active_session": existing},
                        blocked=True,
                    )
                return {
                    "session": existing,
                    "reused": True,
                    "bootstrap_path": str(relative_posix(self.bootstrap_path, self.root)),
                }

        chosen_ticket = str(ticket_id).strip() if isinstance(ticket_id, str) and ticket_id.strip() else ""
        lock = tickets.load_lock(self.root)
        if not chosen_ticket and lock:
            chosen_ticket = str(lock.get("ticket_id", "")).strip()
        if not chosen_ticket:
            raise ExoError(
                code="SESSION_TICKET_REQUIRED",
                message="session-start requires --ticket-id or an active lock",
                blocked=True,
            )

        ticket = tickets.load_ticket(self.root, chosen_ticket)
        if acquire_lock and not lock:
            if distributed:
                manager = distributed_leases.GitDistributedLeaseManager(self.root)
                out = manager.claim(chosen_ticket, owner=self.actor, role=(role or "developer"), duration_hours=duration_hours, remote=remote)
                lock = dict(out.get("lock", {}))
            else:
                lock = tickets.acquire_lock(
                    self.root,
                    chosen_ticket,
                    owner=self.actor,
                    role=(role or "developer"),
                    duration_hours=duration_hours,
                )

        lock = tickets.ensure_lock(self.root, ticket_id=chosen_ticket)
        lock_owner = str(lock.get("owner", "")).strip()
        if lock_owner and lock_owner != self.actor:
            raise ExoError(
                code="SESSION_LOCK_OWNER_MISMATCH",
                message=f"Active lock owner is {lock_owner}; session actor is {self.actor}",
                details={"ticket_id": chosen_ticket, "lock_owner": lock_owner, "actor": self.actor},
                blocked=True,
            )

        started_at = now_iso()
        session_id = _session_id()
        topic = topic_id.strip() if isinstance(topic_id, str) and topic_id.strip() else f"repo:{self.root.as_posix()}"
        context_tokens: int | None
        if context_window_tokens is None:
            context_tokens = None
        else:
            try:
                context_tokens = max(int(context_window_tokens), 1)
            except (TypeError, ValueError):
                raise ExoError(
                    code="SESSION_CONTEXT_WINDOW_INVALID",
                    message="context_window_tokens must be a positive integer",
                    blocked=True,
                ) from None

        prior = _last_memento_summary(self.root, chosen_ticket)
        prior_summary = str(prior.get("summary", "")).strip() if isinstance(prior, dict) else ""

        lock_branch = ""
        workspace = lock.get("workspace")
        if isinstance(workspace, dict):
            lock_branch = str(workspace.get("branch", "")).strip()

        bootstrap_lines = [
            "# Exo Agent Session Bootstrap",
            "",
            f"session_id: {session_id}",
            f"actor: {self.actor}",
            f"vendor: {vendor}",
            f"model: {model}",
            f"context_window_tokens: {context_tokens if context_tokens is not None else 'unknown'}",
            f"ticket_id: {chosen_ticket}",
            f"ticket_title: {ticket.get('title') or ticket.get('intent') or ''}",
            f"ticket_status: {ticket.get('status') or ''}",
            f"ticket_priority: {ticket.get('priority')}",
            f"topic_id: {topic}",
            f"lock_owner: {lock_owner}",
            f"lock_branch: {lock_branch}",
            f"lock_expires_at: {lock.get('expires_at')}",
            "",
            "## Scope",
            f"- allow: {json.dumps((ticket.get('scope') or {}).get('allow', ['**']), ensure_ascii=True)}",
            f"- deny: {json.dumps((ticket.get('scope') or {}).get('deny', []), ensure_ascii=True)}",
            "",
            "## Checks",
            f"- {json.dumps(ticket.get('checks') or [], ensure_ascii=True)}",
            "",
            "## Prior Session Memento",
            (prior_summary if prior_summary else "(none)"),
            "",
            "## Current Task",
            (task.strip() if isinstance(task, str) and task.strip() else "(not provided)"),
            "",
            "## Lifecycle Commands",
            f"- heartbeat: EXO_ACTOR={self.actor} python3 -m exo.cli lease-heartbeat --ticket-id {chosen_ticket} --owner {self.actor}",
            f"- run worker once: EXO_ACTOR={self.actor} python3 -m exo.cli worker-poll --require-session --limit 50",
            f"- suspend: EXO_ACTOR={self.actor} python3 -m exo.cli session-suspend --reason \"<why pausing>\"",
            (
                f"- finish: EXO_ACTOR={self.actor} python3 -m exo.cli session-finish --summary \"<what changed>\" "
                f"--set-status review --ticket-id {chosen_ticket}"
            ),
        ]
        bootstrap_text = "\n".join(bootstrap_lines).rstrip() + "\n"
        ensure_dir(self.bootstrap_path.parent)
        self.bootstrap_path.write_text(bootstrap_text, encoding="utf-8")

        session_payload = {
            "session_id": session_id,
            "status": "active",
            "actor": self.actor,
            "vendor": vendor.strip() or "unknown",
            "model": model.strip() or "unknown",
            "context_window_tokens": context_tokens,
            "role": role.strip() if isinstance(role, str) and role.strip() else None,
            "task": task.strip() if isinstance(task, str) and task.strip() else None,
            "ticket_id": chosen_ticket,
            "topic_id": topic,
            "started_at": started_at,
            "lock": lock,
            "bootstrap_path": relative_posix(self.bootstrap_path, self.root),
        }
        self._write_active(session_payload)
        self._log_event(
            {
                "event": "session_start",
                "session_id": session_id,
                "ticket_id": chosen_ticket,
                "topic_id": topic,
                "vendor": session_payload["vendor"],
                "model": session_payload["model"],
                "context_window_tokens": context_tokens,
            }
        )
        append_audit(
            self.root,
            event_template(
                actor=self.actor,
                action="session_start",
                result="ok",
                ticket=chosen_ticket,
                details={
                    "session_id": session_id,
                    "vendor": session_payload["vendor"],
                    "model": session_payload["model"],
                    "context_window_tokens": context_tokens,
                },
            ),
        )
        return {
            "session": session_payload,
            "bootstrap_path": str(relative_posix(self.bootstrap_path, self.root)),
            "bootstrap_prompt": bootstrap_text,
            "reused": False,
        }

    def finish(
        self,
        *,
        summary: str,
        ticket_id: str | None = None,
        set_status: str = "review",
        skip_check: bool = False,
        break_glass_reason: str | None = None,
        artifacts: list[str] | None = None,
        blockers: list[str] | None = None,
        next_step: str | None = None,
        release_lock: bool | None = None,
    ) -> dict[str, Any]:
        session = self._load_active()
        if not session:
            raise ExoError(
                code="SESSION_NOT_ACTIVE",
                message=f"No active session for actor {self.actor}",
                blocked=True,
            )

        if set_status not in {"keep", "review", "done"}:
            raise ExoError(
                code="SESSION_STATUS_INVALID",
                message="set_status must be one of keep|review|done",
                blocked=True,
            )

        text_summary = summary.strip()
        if not text_summary:
            raise ExoError(
                code="SESSION_SUMMARY_REQUIRED",
                message="summary is required for session closeout",
                blocked=True,
            )

        if skip_check and not (isinstance(break_glass_reason, str) and break_glass_reason.strip()):
            raise ExoError(
                code="SESSION_BREAK_GLASS_REQUIRED",
                message="break_glass_reason is required when skip_check=true",
                blocked=True,
            )

        session_ticket = str(session.get("ticket_id", "")).strip()
        target_ticket = str(ticket_id).strip() if isinstance(ticket_id, str) and ticket_id.strip() else session_ticket
        if not target_ticket:
            raise ExoError(code="SESSION_TICKET_REQUIRED", message="Active session missing ticket_id", blocked=True)
        if target_ticket != session_ticket:
            raise ExoError(
                code="SESSION_TICKET_MISMATCH",
                message=f"Active session ticket is {session_ticket}; requested {target_ticket}",
                blocked=True,
            )

        verify = "bypassed" if skip_check else "passed"
        check_results: dict[str, Any] | None = None
        if not skip_check:
            check_out = KernelEngine(self.root, actor=self.actor).check(target_ticket)
            check_results = check_out.get("data") if isinstance(check_out.get("data"), dict) else {}
            passed = bool(check_results.get("passed"))
            verify = "passed" if passed else "failed"
            if not passed:
                raise ExoError(
                    code="SESSION_VERIFY_FAILED",
                    message=f"session-finish verify gate failed for ticket {target_ticket}",
                    details={"check_results": check_results},
                    blocked=True,
                )

        ticket_status = None
        if set_status != "keep":
            ticket_data = tickets.load_ticket(self.root, target_ticket)
            ticket_data["status"] = set_status
            tickets.save_ticket(self.root, ticket_data)
            ticket_status = set_status

        effective_release = bool(release_lock) if release_lock is not None else set_status != "keep"
        released_lock = False
        release_details: dict[str, Any] | None = None
        if effective_release:
            active_lock = tickets.load_lock(self.root)
            if active_lock and str(active_lock.get("ticket_id", "")).strip() == target_ticket and isinstance(active_lock.get("distributed"), dict):
                distributed_meta = active_lock.get("distributed", {})
                remote = str(distributed_meta.get("remote", "origin")).strip() or "origin"
                manager = distributed_leases.GitDistributedLeaseManager(self.root)
                out = manager.release(target_ticket, owner=self.actor, remote=remote, ignore_missing=True)
                released_lock = bool(out.get("released")) or bool(out.get("distributed", {}).get("remote_released"))
                release_details = out
            else:
                released_lock = tickets.release_lock(self.root, ticket_id=target_ticket)
                release_details = {"released": released_lock, "distributed": None}

        finished_at = now_iso()
        artifacts_list = [str(item) for item in (artifacts or []) if isinstance(item, str) and item.strip()]
        blockers_list = [str(item) for item in (blockers or []) if isinstance(item, str) and item.strip()]
        next_step_value = next_step.strip() if isinstance(next_step, str) and next_step.strip() else ""

        session_id = str(session.get("session_id", _session_id()))
        memento_dir = self.root / SESSION_MEMORY_DIR / target_ticket
        ensure_dir(memento_dir)
        memento_path = memento_dir / f"{session_id}.md"
        memento_text = "\n".join(
            [
                f"# Session {session_id}",
                "",
                "## Metadata",
                f"- actor: {self.actor}",
                f"- vendor: {session.get('vendor')}",
                f"- model: {session.get('model')}",
                f"- context_window_tokens: {session.get('context_window_tokens')}",
                f"- ticket_id: {target_ticket}",
                f"- started_at: {session.get('started_at')}",
                f"- finished_at: {finished_at}",
                f"- verify: {verify}",
                f"- break_glass_reason: {break_glass_reason.strip() if isinstance(break_glass_reason, str) else ''}",
                "",
                "## Summary",
                text_summary,
                "",
                "## Artifacts",
                ("\n".join(f"- {item}" for item in artifacts_list) if artifacts_list else "- (none)"),
                "",
                "## Blockers",
                ("\n".join(f"- {item}" for item in blockers_list) if blockers_list else "- (none)"),
                "",
                "## Next Step",
                (next_step_value if next_step_value else "(none)"),
                "",
            ]
        )
        memento_path.write_text(memento_text, encoding="utf-8")

        memento_row = {
            "session_id": session_id,
            "actor": self.actor,
            "ticket_id": target_ticket,
            "vendor": session.get("vendor"),
            "model": session.get("model"),
            "context_window_tokens": session.get("context_window_tokens"),
            "started_at": session.get("started_at"),
            "finished_at": finished_at,
            "verify": verify,
            "break_glass_reason": break_glass_reason.strip() if isinstance(break_glass_reason, str) else "",
            "set_status": set_status,
            "ticket_status": ticket_status,
            "released_lock": released_lock,
            "summary": text_summary,
            "artifact_count": len(artifacts_list),
            "blocker_count": len(blockers_list),
            "next_step": next_step_value,
            "memento_path": relative_posix(memento_path, self.root),
        }
        with _session_log_lock(self.root):
            _append_jsonl(self.root / SESSION_INDEX_PATH, memento_row)

        self._log_event(
            {
                "event": "session_finish",
                "session_id": session_id,
                "ticket_id": target_ticket,
                "verify": verify,
                "set_status": set_status,
                "released_lock": released_lock,
                "memento_path": relative_posix(memento_path, self.root),
            }
        )
        append_audit(
            self.root,
            event_template(
                actor=self.actor,
                action="session_finish",
                result="ok",
                ticket=target_ticket,
                details={
                    "session_id": session_id,
                    "verify": verify,
                    "set_status": set_status,
                    "released_lock": released_lock,
                    "memento_path": relative_posix(memento_path, self.root),
                },
            ),
        )

        self.active_session_path.unlink(missing_ok=True)

        return {
            "session_id": session_id,
            "ticket_id": target_ticket,
            "set_status": set_status,
            "ticket_status": ticket_status,
            "verify": verify,
            "check_results": check_results,
            "released_lock": released_lock,
            "release_details": release_details,
            "memento_path": str(relative_posix(memento_path, self.root)),
            "session_index_path": str(relative_posix(self.root / SESSION_INDEX_PATH, self.root)),
        }

    def suspend(
        self,
        *,
        reason: str,
        ticket_id: str | None = None,
        release_lock: bool = True,
        stash_changes: bool = False,
    ) -> dict[str, Any]:
        session = self._load_active()
        if not session:
            raise ExoError(
                code="SESSION_NOT_ACTIVE",
                message=f"No active session for actor {self.actor}",
                blocked=True,
            )

        text_reason = reason.strip()
        if not text_reason:
            raise ExoError(
                code="SESSION_SUSPEND_REASON_REQUIRED",
                message="reason is required for session suspend",
                blocked=True,
            )

        session_ticket = str(session.get("ticket_id", "")).strip()
        target_ticket = str(ticket_id).strip() if isinstance(ticket_id, str) and ticket_id.strip() else session_ticket
        if not target_ticket:
            raise ExoError(code="SESSION_TICKET_REQUIRED", message="Active session missing ticket_id", blocked=True)
        if target_ticket != session_ticket:
            raise ExoError(
                code="SESSION_TICKET_MISMATCH",
                message=f"Active session ticket is {session_ticket}; requested {target_ticket}",
                blocked=True,
            )

        stash_ref: str | None = None
        if stash_changes:
            try:
                result = subprocess.run(
                    ["git", "stash", "push", "-m", f"exo-suspend:{session.get('session_id', '')}"],
                    cwd=str(self.root),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0 and "No local changes" not in result.stdout:
                    stash_ref = result.stdout.strip().split("\n")[-1] if result.stdout.strip() else None
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

        ticket_data = tickets.load_ticket(self.root, target_ticket)
        previous_status = str(ticket_data.get("status", ""))
        ticket_data["status"] = "paused"
        tickets.save_ticket(self.root, ticket_data)

        released_lock = False
        release_details: dict[str, Any] | None = None
        if release_lock:
            active_lock = tickets.load_lock(self.root)
            if active_lock and str(active_lock.get("ticket_id", "")).strip() == target_ticket:
                if isinstance(active_lock.get("distributed"), dict):
                    distributed_meta = active_lock.get("distributed", {})
                    remote = str(distributed_meta.get("remote", "origin")).strip() or "origin"
                    manager = distributed_leases.GitDistributedLeaseManager(self.root)
                    out = manager.release(target_ticket, owner=self.actor, remote=remote, ignore_missing=True)
                    released_lock = bool(out.get("released")) or bool(out.get("distributed", {}).get("remote_released"))
                    release_details = out
                else:
                    released_lock = tickets.release_lock(self.root, ticket_id=target_ticket)
                    release_details = {"released": released_lock, "distributed": None}

        suspended_at = now_iso()
        session_id = str(session.get("session_id", _session_id()))

        suspended_payload = {
            **session,
            "status": "suspended",
            "suspended_at": suspended_at,
            "suspend_reason": text_reason,
            "previous_ticket_status": previous_status,
            "released_lock": released_lock,
            "stash_ref": stash_ref,
        }

        suspended_dir = self.root / SESSION_SUSPENDED_DIR
        ensure_dir(suspended_dir)
        suspended_path = suspended_dir / f"{self.actor_token}.suspended.json"
        suspended_path.write_text(
            json.dumps(suspended_payload, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        self._log_event(
            {
                "event": "session_suspend",
                "session_id": session_id,
                "ticket_id": target_ticket,
                "reason": text_reason,
                "released_lock": released_lock,
                "stash_ref": stash_ref,
            }
        )
        append_audit(
            self.root,
            event_template(
                actor=self.actor,
                action="session_suspend",
                result="ok",
                ticket=target_ticket,
                details={
                    "session_id": session_id,
                    "reason": text_reason,
                    "released_lock": released_lock,
                    "stash_ref": stash_ref,
                    "previous_ticket_status": previous_status,
                },
            ),
        )

        self.active_session_path.unlink(missing_ok=True)

        return {
            "session_id": session_id,
            "ticket_id": target_ticket,
            "suspended_at": suspended_at,
            "reason": text_reason,
            "released_lock": released_lock,
            "release_details": release_details,
            "stash_ref": stash_ref,
            "previous_ticket_status": previous_status,
            "suspended_path": str(relative_posix(suspended_path, self.root)),
        }

    def resume(
        self,
        *,
        ticket_id: str | None = None,
        acquire_lock: bool = True,
        pop_stash: bool = False,
        distributed: bool = False,
        remote: str = "origin",
        duration_hours: int = 2,
        role: str | None = None,
    ) -> dict[str, Any]:
        existing = self._load_active()
        if existing:
            existing_ticket = str(existing.get("ticket_id", "")).strip()
            raise ExoError(
                code="SESSION_ALREADY_ACTIVE",
                message=f"Actor {self.actor} already has active session for {existing_ticket}",
                details={"active_session": existing},
                blocked=True,
            )

        suspended_path = self.root / SESSION_SUSPENDED_DIR / f"{self.actor_token}.suspended.json"
        if not suspended_path.exists():
            raise ExoError(
                code="SESSION_NOT_SUSPENDED",
                message=f"No suspended session for actor {self.actor}",
                blocked=True,
            )

        suspended = json.loads(suspended_path.read_text(encoding="utf-8"))
        if not isinstance(suspended, dict):
            raise ExoError(
                code="SESSION_SUSPEND_CORRUPT",
                message="Suspended session file is corrupt",
                blocked=True,
            )

        suspended_ticket = str(suspended.get("ticket_id", "")).strip()
        target_ticket = str(ticket_id).strip() if isinstance(ticket_id, str) and ticket_id.strip() else suspended_ticket
        if not target_ticket:
            raise ExoError(code="SESSION_TICKET_REQUIRED", message="Suspended session missing ticket_id", blocked=True)
        if target_ticket != suspended_ticket:
            raise ExoError(
                code="SESSION_TICKET_MISMATCH",
                message=f"Suspended session ticket is {suspended_ticket}; requested {target_ticket}",
                blocked=True,
            )

        lock: dict[str, Any] | None = None
        if acquire_lock:
            if distributed:
                lease_manager = distributed_leases.GitDistributedLeaseManager(self.root)
                out = lease_manager.claim(
                    target_ticket,
                    owner=self.actor,
                    role=(role or "developer"),
                    duration_hours=duration_hours,
                    remote=remote,
                )
                lock = dict(out.get("lock", {}))
            else:
                lock = tickets.acquire_lock(
                    self.root,
                    target_ticket,
                    owner=self.actor,
                    role=(role or "developer"),
                    duration_hours=duration_hours,
                )

        ticket_data = tickets.load_ticket(self.root, target_ticket)
        ticket_data["status"] = "active"
        tickets.save_ticket(self.root, ticket_data)

        stash_popped = False
        stash_ref = suspended.get("stash_ref")
        if pop_stash and stash_ref:
            try:
                result = subprocess.run(
                    ["git", "stash", "pop"],
                    cwd=str(self.root),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                stash_popped = result.returncode == 0
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

        resumed_at = now_iso()
        session_id = str(suspended.get("session_id", _session_id()))

        session_payload = {
            "session_id": session_id,
            "status": "active",
            "actor": self.actor,
            "vendor": suspended.get("vendor", "unknown"),
            "model": suspended.get("model", "unknown"),
            "context_window_tokens": suspended.get("context_window_tokens"),
            "role": suspended.get("role"),
            "task": suspended.get("task"),
            "ticket_id": target_ticket,
            "topic_id": suspended.get("topic_id", f"repo:{self.root.as_posix()}"),
            "started_at": suspended.get("started_at"),
            "resumed_at": resumed_at,
            "lock": lock or tickets.load_lock(self.root),
            "bootstrap_path": relative_posix(self.bootstrap_path, self.root),
            "suspend_history": [
                {
                    "suspended_at": suspended.get("suspended_at"),
                    "resumed_at": resumed_at,
                    "reason": suspended.get("suspend_reason", ""),
                },
            ],
        }
        self._write_active(session_payload)

        prior = _last_memento_summary(self.root, target_ticket)
        prior_summary = str(prior.get("summary", "")).strip() if isinstance(prior, dict) else ""

        active_lock = session_payload.get("lock") or {}
        lock_owner = str(active_lock.get("owner", "")).strip()
        lock_branch = ""
        workspace = active_lock.get("workspace")
        if isinstance(workspace, dict):
            lock_branch = str(workspace.get("branch", "")).strip()

        bootstrap_lines = [
            "# Exo Agent Session Bootstrap (Resumed)",
            "",
            f"session_id: {session_id}",
            f"actor: {self.actor}",
            f"vendor: {session_payload['vendor']}",
            f"model: {session_payload['model']}",
            f"context_window_tokens: {session_payload.get('context_window_tokens') or 'unknown'}",
            f"ticket_id: {target_ticket}",
            f"ticket_status: active (resumed from paused)",
            f"topic_id: {session_payload['topic_id']}",
            f"lock_owner: {lock_owner}",
            f"lock_branch: {lock_branch}",
            f"lock_expires_at: {active_lock.get('expires_at')}",
            "",
            "## Suspend Context",
            f"- reason: {suspended.get('suspend_reason', '')}",
            f"- suspended_at: {suspended.get('suspended_at', '')}",
            f"- stash_ref: {stash_ref or '(none)'}",
            f"- stash_popped: {stash_popped}",
            "",
            "## Prior Session Memento",
            (prior_summary if prior_summary else "(none)"),
            "",
            "## Previous Task",
            (str(suspended.get("task", "")).strip() if suspended.get("task") else "(not provided)"),
            "",
            "## Lifecycle Commands",
            f"- heartbeat: EXO_ACTOR={self.actor} python3 -m exo.cli lease-heartbeat --ticket-id {target_ticket} --owner {self.actor}",
            f"- suspend: EXO_ACTOR={self.actor} python3 -m exo.cli session-suspend --reason \"<why pausing>\"",
            (
                f"- finish: EXO_ACTOR={self.actor} python3 -m exo.cli session-finish --summary \"<what changed>\" "
                f"--set-status review --ticket-id {target_ticket}"
            ),
        ]
        bootstrap_text = "\n".join(bootstrap_lines).rstrip() + "\n"
        ensure_dir(self.bootstrap_path.parent)
        self.bootstrap_path.write_text(bootstrap_text, encoding="utf-8")

        self._log_event(
            {
                "event": "session_resume",
                "session_id": session_id,
                "ticket_id": target_ticket,
                "acquired_lock": lock is not None,
                "stash_popped": stash_popped,
            }
        )
        append_audit(
            self.root,
            event_template(
                actor=self.actor,
                action="session_resume",
                result="ok",
                ticket=target_ticket,
                details={
                    "session_id": session_id,
                    "acquired_lock": lock is not None,
                    "stash_popped": stash_popped,
                    "suspend_reason": suspended.get("suspend_reason", ""),
                },
            ),
        )

        suspended_path.unlink(missing_ok=True)

        return {
            "session": session_payload,
            "bootstrap_path": str(relative_posix(self.bootstrap_path, self.root)),
            "bootstrap_prompt": bootstrap_text,
            "resumed_at": resumed_at,
            "acquired_lock": lock is not None,
            "stash_popped": stash_popped,
            "ticket_id": target_ticket,
        }

    def get_active(self) -> dict[str, Any] | None:
        return self._load_active()


def _session_age_hours(session: dict[str, Any]) -> float:
    started_raw = session.get("started_at") or session.get("suspended_at")
    if not started_raw:
        return 0.0
    try:
        started = datetime.fromisoformat(str(started_raw))
        return max((datetime.now().astimezone() - started).total_seconds() / 3600.0, 0.0)
    except (TypeError, ValueError):
        return 0.0


def scan_sessions(repo: Path, *, stale_hours: float = 48.0) -> dict[str, Any]:
    """Scan for all active and suspended sessions across actors, flagging stale ones."""
    repo = Path(repo).resolve()
    active_dir = repo / SESSION_CACHE_DIR
    suspended_dir = repo / SESSION_SUSPENDED_DIR

    active_sessions: list[dict[str, Any]] = []
    suspended_sessions: list[dict[str, Any]] = []
    stale_sessions: list[dict[str, Any]] = []

    if active_dir.exists():
        for path in sorted(active_dir.glob("*.active.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            age = _session_age_hours(data)
            entry = {
                "path": str(relative_posix(path, repo)),
                "actor": data.get("actor", ""),
                "session_id": data.get("session_id", ""),
                "ticket_id": data.get("ticket_id", ""),
                "status": data.get("status", ""),
                "started_at": data.get("started_at", ""),
                "age_hours": round(age, 2),
                "stale": age >= stale_hours,
            }
            active_sessions.append(entry)
            if age >= stale_hours:
                stale_sessions.append(entry)

    if suspended_dir.exists():
        for path in sorted(suspended_dir.glob("*.suspended.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            age = _session_age_hours(data)
            entry = {
                "path": str(relative_posix(path, repo)),
                "actor": data.get("actor", ""),
                "session_id": data.get("session_id", ""),
                "ticket_id": data.get("ticket_id", ""),
                "status": "suspended",
                "suspended_at": data.get("suspended_at", ""),
                "suspend_reason": data.get("suspend_reason", ""),
                "age_hours": round(age, 2),
                "stale": age >= stale_hours,
            }
            suspended_sessions.append(entry)
            if age >= stale_hours:
                stale_sessions.append(entry)

    lock = tickets.load_lock(repo)
    lock_info: dict[str, Any] | None = None
    if lock:
        lock_age = 0.0
        try:
            created = datetime.fromisoformat(str(lock.get("created_at", "")))
            lock_age = max((datetime.now().astimezone() - created).total_seconds() / 3600.0, 0.0)
        except (TypeError, ValueError):
            pass
        lock_info = {
            "ticket_id": lock.get("ticket_id", ""),
            "owner": lock.get("owner", ""),
            "created_at": lock.get("created_at", ""),
            "expires_at": lock.get("expires_at", ""),
            "age_hours": round(lock_age, 2),
        }

    return {
        "active_sessions": active_sessions,
        "suspended_sessions": suspended_sessions,
        "stale_sessions": stale_sessions,
        "active_lock": lock_info,
        "stale_hours_threshold": stale_hours,
    }


def cleanup_sessions(
    repo: Path,
    *,
    stale_hours: float = 48.0,
    force: bool = False,
    release_lock: bool = False,
    actor: str = "system",
) -> dict[str, Any]:
    """Remove stale active/suspended sessions and optionally release orphaned locks."""
    repo = Path(repo).resolve()
    scan = scan_sessions(repo, stale_hours=stale_hours)

    removed_active: list[dict[str, Any]] = []
    removed_suspended: list[dict[str, Any]] = []
    released_lock = False

    targets = scan["stale_sessions"] if not force else scan["active_sessions"] + scan["suspended_sessions"]

    for entry in targets:
        path = repo / entry["path"]
        if not path.exists():
            continue

        ticket_id = str(entry.get("ticket_id", "")).strip()
        if ticket_id:
            ticket_data = tickets.load_ticket(repo, ticket_id)
            current_status = str(ticket_data.get("status", ""))
            if current_status in {"active", "paused"} and entry.get("stale", False):
                ticket_data["status"] = "todo"
                tickets.save_ticket(repo, ticket_data)

        path.unlink(missing_ok=True)

        if entry.get("status") == "suspended":
            removed_suspended.append(entry)
        else:
            removed_active.append(entry)

        append_audit(
            repo,
            event_template(
                actor=actor,
                action="session_cleanup",
                result="ok",
                ticket=ticket_id or None,
                details={
                    "session_id": entry.get("session_id", ""),
                    "removed_path": entry.get("path", ""),
                    "age_hours": entry.get("age_hours", 0),
                    "force": force,
                },
            ),
        )

    if release_lock:
        lock = tickets.load_lock(repo)
        if lock:
            lock_ticket = str(lock.get("ticket_id", "")).strip()
            released_lock = tickets.release_lock(repo, ticket_id=lock_ticket)
            if released_lock:
                append_audit(
                    repo,
                    event_template(
                        actor=actor,
                        action="session_cleanup_lock_release",
                        result="ok",
                        ticket=lock_ticket or None,
                        details={"reason": "orphan_cleanup"},
                    ),
                )

    return {
        "removed_active": removed_active,
        "removed_suspended": removed_suspended,
        "removed_count": len(removed_active) + len(removed_suspended),
        "released_lock": released_lock,
        "stale_hours_threshold": stale_hours,
        "force": force,
    }
