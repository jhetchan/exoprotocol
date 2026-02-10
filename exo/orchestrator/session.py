from __future__ import annotations

import contextlib
import json
import re
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

    def get_active(self) -> dict[str, Any] | None:
        return self._load_active()
