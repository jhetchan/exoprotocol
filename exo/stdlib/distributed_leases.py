from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from exo.kernel import tickets
from exo.kernel.errors import ExoError


_LEASE_VERSION = "exo-distributed-lease-v1"
_LEASE_REF_PREFIX = "refs/exoprotocol/locks"
_GIT_AUTHOR_ENV = {
    "GIT_AUTHOR_NAME": "ExoProtocol",
    "GIT_AUTHOR_EMAIL": "exo@local.invalid",
    "GIT_COMMITTER_NAME": "ExoProtocol",
    "GIT_COMMITTER_EMAIL": "exo@local.invalid",
}


def _normalize_duration_hours(duration_hours: int) -> int:
    try:
        value = int(duration_hours)
    except (TypeError, ValueError):
        raise ExoError(
            code="LOCK_DURATION_INVALID",
            message="duration_hours must be an integer >= 1",
            blocked=True,
        ) from None
    if value < 1:
        raise ExoError(
            code="LOCK_DURATION_INVALID",
            message="duration_hours must be an integer >= 1",
            blocked=True,
        )
    return value


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _sanitize_ref_segment(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ExoError(code="TICKET_INVALID", message="ticket id is required", blocked=True)
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-")
    if not sanitized:
        raise ExoError(code="TICKET_INVALID", message=f"ticket id invalid for ref namespace: {value}", blocked=True)
    return sanitized


class GitDistributedLeaseManager:
    def __init__(self, repo: Path | str) -> None:
        self.repo = Path(repo).resolve()

    def _run_git(
        self,
        args: list[str],
        *,
        check: bool = True,
        stdin: str | None = None,
        error_code: str = "GIT_COMMAND_FAILED",
        message: str = "Git command failed",
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["git", *args]
        run_env = os.environ.copy()
        if env:
            run_env.update(env)
        proc = subprocess.run(
            cmd,
            cwd=self.repo,
            capture_output=True,
            text=True,
            input=stdin,
            env=run_env,
        )
        if check and proc.returncode != 0:
            raise ExoError(
                code=error_code,
                message=message,
                details={
                    "command": " ".join(cmd),
                    "returncode": proc.returncode,
                    "stdout": (proc.stdout or "")[-1200:],
                    "stderr": (proc.stderr or "")[-1200:],
                },
                blocked=True,
            )
        return proc

    def _require_remote(self, remote: str) -> str:
        value = remote.strip()
        if not value:
            raise ExoError(code="DISTRIBUTED_REMOTE_REQUIRED", message="remote is required", blocked=True)
        probe = self._run_git(["remote", "get-url", value], check=False)
        if probe.returncode != 0:
            raise ExoError(
                code="DISTRIBUTED_REMOTE_NOT_FOUND",
                message=f"Git remote not found: {value}",
                blocked=True,
            )
        return value

    def _lease_ref(self, ticket_id: str) -> str:
        return f"{_LEASE_REF_PREFIX}/{_sanitize_ref_segment(ticket_id)}"

    def _ls_remote_sha(self, remote: str, ref: str) -> str | None:
        proc = self._run_git(["ls-remote", remote, ref], check=False)
        if proc.returncode != 0:
            raise ExoError(
                code="DISTRIBUTED_REMOTE_READ_FAILED",
                message=f"Failed to query remote ref {ref} on {remote}",
                details={
                    "remote": remote,
                    "ref": ref,
                    "stdout": (proc.stdout or "")[-1200:],
                    "stderr": (proc.stderr or "")[-1200:],
                },
                blocked=True,
            )
        line = (proc.stdout or "").strip()
        if not line:
            return None
        return line.split()[0]

    def _fetch_ref(self, remote: str, ref: str) -> str:
        local_ref = f"refs/exoprotocol/tmp/{uuid4().hex[:12]}"
        proc = self._run_git(["fetch", remote, f"{ref}:{local_ref}"], check=False)
        if proc.returncode != 0:
            raise ExoError(
                code="DISTRIBUTED_REMOTE_FETCH_FAILED",
                message=f"Failed to fetch remote ref {ref} from {remote}",
                details={
                    "remote": remote,
                    "ref": ref,
                    "stdout": (proc.stdout or "")[-1200:],
                    "stderr": (proc.stderr or "")[-1200:],
                },
                blocked=True,
            )
        sha = self._run_git(
            ["rev-parse", local_ref],
            error_code="DISTRIBUTED_REMOTE_FETCH_FAILED",
            message=f"Failed to resolve fetched ref {local_ref}",
        ).stdout.strip()
        self._run_git(["update-ref", "-d", local_ref], check=False)
        if not sha:
            raise ExoError(
                code="DISTRIBUTED_REMOTE_FETCH_FAILED",
                message=f"Fetched ref {ref} has no SHA",
                blocked=True,
            )
        return sha

    def _read_commit_message(self, sha: str) -> str:
        proc = self._run_git(
            ["show", "-s", "--format=%B", sha],
            error_code="DISTRIBUTED_LOCK_PARSE_FAILED",
            message=f"Failed to read lock commit message: {sha}",
        )
        return proc.stdout

    def _parse_lease_payload(self, message: str) -> dict[str, Any]:
        lines = [line.strip() for line in message.splitlines() if line.strip()]
        if not lines or lines[0] != _LEASE_VERSION:
            raise ExoError(
                code="DISTRIBUTED_LOCK_PARSE_FAILED",
                message="Remote lock commit format is invalid",
                blocked=True,
            )

        payload: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            payload[key.strip()] = value.strip()

        required_keys = {
            "ticket_id",
            "owner",
            "role",
            "created_at",
            "updated_at",
            "heartbeat_at",
            "expires_at",
            "lease_expires_at",
            "fencing_token",
            "base_branch",
            "nonce",
        }
        missing = sorted(required_keys - set(payload.keys()))
        if missing:
            raise ExoError(
                code="DISTRIBUTED_LOCK_PARSE_FAILED",
                message=f"Remote lock payload missing keys: {', '.join(missing)}",
                blocked=True,
            )

        try:
            fencing = int(payload["fencing_token"])
        except ValueError:
            raise ExoError(
                code="DISTRIBUTED_LOCK_PARSE_FAILED",
                message="Remote lock fencing_token is not an integer",
                blocked=True,
            ) from None

        data: dict[str, Any] = dict(payload)
        data["fencing_token"] = fencing
        return data

    def _read_remote_lock(self, remote: str, ticket_id: str) -> dict[str, Any] | None:
        ref = self._lease_ref(ticket_id)
        sha = self._ls_remote_sha(remote, ref)
        if sha is None:
            return None
        fetched_sha = self._fetch_ref(remote, ref)
        payload = self._parse_lease_payload(self._read_commit_message(fetched_sha))
        expires = _parse_iso(str(payload["expires_at"]))
        payload["expired"] = _now() >= expires
        payload["commit_sha"] = sha
        payload["remote"] = remote
        payload["ref"] = ref
        return payload

    def _empty_tree(self) -> str:
        tree = self._run_git(["hash-object", "-t", "tree", "/dev/null"]).stdout.strip()
        if not tree:
            raise ExoError(
                code="DISTRIBUTED_LOCK_COMMIT_FAILED",
                message="Failed to create empty git tree object",
                blocked=True,
            )
        return tree

    def _build_commit_message(self, payload: dict[str, Any]) -> str:
        ordered_keys = [
            "ticket_id",
            "owner",
            "role",
            "created_at",
            "updated_at",
            "heartbeat_at",
            "expires_at",
            "lease_expires_at",
            "fencing_token",
            "base_branch",
            "nonce",
        ]
        lines = [_LEASE_VERSION]
        for key in ordered_keys:
            lines.append(f"{key}: {payload[key]}")
        return "\n".join(lines) + "\n"

    def _create_lock_commit(self, payload: dict[str, Any], *, parent_sha: str | None = None) -> str:
        args = ["commit-tree", self._empty_tree()]
        if isinstance(parent_sha, str) and parent_sha.strip():
            args.extend(["-p", parent_sha.strip()])
        proc = self._run_git(
            args,
            stdin=self._build_commit_message(payload),
            env=_GIT_AUTHOR_ENV,
            error_code="DISTRIBUTED_LOCK_COMMIT_FAILED",
            message="Failed to create distributed lock commit",
        )
        sha = proc.stdout.strip()
        if not sha:
            raise ExoError(
                code="DISTRIBUTED_LOCK_COMMIT_FAILED",
                message="Distributed lock commit returned empty SHA",
                blocked=True,
            )
        return sha

    def _push_create(self, remote: str, commit_sha: str, ref: str) -> None:
        proc = self._run_git(["push", "--porcelain", remote, f"{commit_sha}:{ref}"], check=False)
        if proc.returncode != 0:
            raise ExoError(
                code="DISTRIBUTED_LOCK_CONFLICT",
                message=f"Distributed claim failed due to competing writer on {ref}",
                details={
                    "remote": remote,
                    "ref": ref,
                    "stdout": (proc.stdout or "")[-1200:],
                    "stderr": (proc.stderr or "")[-1200:],
                },
                blocked=True,
            )

    def _push_update(self, remote: str, commit_sha: str, ref: str, expected_sha: str) -> None:
        proc = self._run_git(
            ["push", "--porcelain", f"--force-with-lease={ref}:{expected_sha}", remote, f"{commit_sha}:{ref}"],
            check=False,
        )
        if proc.returncode != 0:
            raise ExoError(
                code="DISTRIBUTED_LOCK_CONFLICT",
                message=f"Distributed CAS update failed for {ref}",
                details={
                    "remote": remote,
                    "ref": ref,
                    "expected_sha": expected_sha,
                    "stdout": (proc.stdout or "")[-1200:],
                    "stderr": (proc.stderr or "")[-1200:],
                },
                blocked=True,
            )

    def _push_delete(self, remote: str, ref: str, expected_sha: str) -> None:
        proc = self._run_git(
            ["push", "--porcelain", f"--force-with-lease={ref}:{expected_sha}", remote, f":{ref}"],
            check=False,
        )
        if proc.returncode != 0:
            raise ExoError(
                code="DISTRIBUTED_LOCK_CONFLICT",
                message=f"Distributed lock release failed for {ref}",
                details={
                    "remote": remote,
                    "ref": ref,
                    "expected_sha": expected_sha,
                    "stdout": (proc.stdout or "")[-1200:],
                    "stderr": (proc.stderr or "")[-1200:],
                },
                blocked=True,
            )

    def _sync_local_lock(self, payload: dict[str, Any], *, remote: str, ref: str, commit_sha: str) -> dict[str, Any]:
        ticket_id = str(payload["ticket_id"])
        owner = str(payload["owner"])
        role = str(payload["role"])
        base_branch = str(payload.get("base_branch", "main"))
        existing = self._precheck_local_lock(ticket_id, owner)
        existing_owner = str(existing.get("owner", "")).strip() if isinstance(existing, dict) else ""
        if existing and existing_owner and existing_owner != owner:
            raise ExoError(
                code="LOCK_COLLISION",
                message=f"Ticket lock collision on {ticket_id}: held by {existing_owner}",
                details=existing,
                blocked=True,
            )

        lock = {
            "ticket_id": ticket_id,
            "owner": owner,
            "role": role,
            "created_at": str(payload["created_at"]),
            "updated_at": str(payload["updated_at"]),
            "heartbeat_at": str(payload["heartbeat_at"]),
            "expires_at": str(payload["expires_at"]),
            "lease_expires_at": str(payload["lease_expires_at"]),
            "fencing_token": int(payload["fencing_token"]),
            "workspace": {
                "branch": f"codex/{ticket_id}",
                "base": base_branch,
            },
            "distributed": {
                "remote": remote,
                "ref": ref,
                "commit_sha": commit_sha,
                "nonce": str(payload["nonce"]),
            },
        }
        return tickets.write_lock(self.repo, lock)

    def _precheck_local_lock(self, ticket_id: str, owner: str) -> dict[str, Any] | None:
        existing = tickets.load_lock(self.repo)
        if existing and str(existing.get("ticket_id", "")).strip() != ticket_id:
            raise ExoError(
                code="LOCK_HELD",
                message=f"Ticket lock already held by {existing.get('owner')}",
                details=existing,
                blocked=True,
            )
        existing_owner = str(existing.get("owner", "")).strip() if isinstance(existing, dict) else ""
        if existing and existing_owner and existing_owner != owner:
            raise ExoError(
                code="LOCK_COLLISION",
                message=f"Ticket lock collision on {ticket_id}: held by {existing_owner}",
                details=existing,
                blocked=True,
            )
        return existing

    def claim(
        self,
        ticket_id: str,
        *,
        owner: str,
        role: str = "developer",
        duration_hours: int = 2,
        remote: str = "origin",
        base_branch: str = "main",
    ) -> dict[str, Any]:
        owner_value = owner.strip()
        if not owner_value:
            raise ExoError(code="LOCK_OWNER_INVALID", message="owner is required", blocked=True)
        _ = self._precheck_local_lock(ticket_id, owner_value)
        role_value = role.strip() or "developer"
        remote_name = self._require_remote(remote)
        ref = self._lease_ref(ticket_id)
        duration = _normalize_duration_hours(duration_hours)
        now = _now()
        now_iso = _iso(now)
        expiry_iso = _iso(now + timedelta(hours=duration))
        remote_lock = self._read_remote_lock(remote_name, ticket_id)

        mode = "claim"
        parent_sha: str | None = None
        expected_sha: str | None = None
        fencing = 1
        created_at = now_iso
        if remote_lock:
            remote_owner = str(remote_lock["owner"])
            remote_expired = bool(remote_lock.get("expired"))
            if remote_owner != owner_value and not remote_expired:
                raise ExoError(
                    code="DISTRIBUTED_LOCK_COLLISION",
                    message=f"Distributed lock collision on {ticket_id}: held by {remote_owner}",
                    details=remote_lock,
                    blocked=True,
                )
            expected_sha = str(remote_lock["commit_sha"])
            parent_sha = expected_sha
            fencing = int(remote_lock["fencing_token"]) + 1
            created_at = str(remote_lock["created_at"]) if remote_owner == owner_value else now_iso
            mode = "renew" if remote_owner == owner_value else "takeover"

        payload: dict[str, Any] = {
            "ticket_id": ticket_id,
            "owner": owner_value,
            "role": role_value,
            "created_at": created_at,
            "updated_at": now_iso,
            "heartbeat_at": now_iso,
            "expires_at": expiry_iso,
            "lease_expires_at": expiry_iso,
            "fencing_token": fencing,
            "base_branch": base_branch.strip() or "main",
            "nonce": uuid4().hex,
        }

        commit_sha = self._create_lock_commit(payload, parent_sha=parent_sha)
        if expected_sha:
            self._push_update(remote_name, commit_sha, ref, expected_sha)
        else:
            self._push_create(remote_name, commit_sha, ref)

        lock = self._sync_local_lock(payload, remote=remote_name, ref=ref, commit_sha=commit_sha)
        return {
            "ticket_id": ticket_id,
            "lock": lock,
            "distributed": {
                "mode": mode,
                "remote": remote_name,
                "ref": ref,
                "commit_sha": commit_sha,
                "fencing_token": fencing,
            },
        }

    def renew(
        self,
        ticket_id: str,
        *,
        owner: str,
        role: str = "developer",
        duration_hours: int = 2,
        remote: str = "origin",
    ) -> dict[str, Any]:
        return self.claim(
            ticket_id,
            owner=owner,
            role=role,
            duration_hours=duration_hours,
            remote=remote,
        )

    def heartbeat(
        self,
        ticket_id: str,
        *,
        owner: str,
        duration_hours: int = 2,
        remote: str = "origin",
    ) -> dict[str, Any]:
        owner_value = owner.strip()
        if not owner_value:
            raise ExoError(code="LOCK_OWNER_INVALID", message="owner is required", blocked=True)
        _ = self._precheck_local_lock(ticket_id, owner_value)

        remote_name = self._require_remote(remote)
        ref = self._lease_ref(ticket_id)
        duration = _normalize_duration_hours(duration_hours)
        remote_lock = self._read_remote_lock(remote_name, ticket_id)
        if not remote_lock:
            raise ExoError(
                code="DISTRIBUTED_LOCK_NOT_FOUND",
                message=f"No distributed lock exists for ticket {ticket_id}",
                blocked=True,
            )

        remote_owner = str(remote_lock["owner"])
        if remote_owner != owner_value:
            raise ExoError(
                code="DISTRIBUTED_LOCK_OWNER_MISMATCH",
                message=f"Distributed lock owner is {remote_owner}; heartbeat owner {owner_value} is not allowed",
                details=remote_lock,
                blocked=True,
            )

        now = _now()
        now_iso = _iso(now)
        candidate_expiry = now + timedelta(hours=duration)
        current_expiry = _parse_iso(str(remote_lock["expires_at"]))
        expiry = candidate_expiry if candidate_expiry >= current_expiry else current_expiry
        expiry_iso = _iso(expiry)

        payload: dict[str, Any] = {
            "ticket_id": str(remote_lock["ticket_id"]),
            "owner": remote_owner,
            "role": str(remote_lock["role"]),
            "created_at": str(remote_lock["created_at"]),
            "updated_at": now_iso,
            "heartbeat_at": now_iso,
            "expires_at": expiry_iso,
            "lease_expires_at": expiry_iso,
            "fencing_token": int(remote_lock["fencing_token"]),
            "base_branch": str(remote_lock.get("base_branch", "main")),
            "nonce": str(remote_lock["nonce"]),
        }
        expected_sha = str(remote_lock["commit_sha"])
        commit_sha = self._create_lock_commit(payload, parent_sha=expected_sha)
        self._push_update(remote_name, commit_sha, ref, expected_sha)
        lock = self._sync_local_lock(payload, remote=remote_name, ref=ref, commit_sha=commit_sha)
        return {
            "ticket_id": ticket_id,
            "lock": lock,
            "distributed": {
                "mode": "heartbeat",
                "remote": remote_name,
                "ref": ref,
                "commit_sha": commit_sha,
                "fencing_token": int(payload["fencing_token"]),
            },
        }

    def list_locks(
        self,
        *,
        remote: str = "origin",
    ) -> list[dict[str, Any]]:
        """List all distributed lock refs on the remote.

        Returns a list of lock info dicts with ticket_id, owner, ref,
        commit_sha, expired status, and parsed payload fields.
        """
        remote_name = self._require_remote(remote)
        prefix = f"{_LEASE_REF_PREFIX}/"
        proc = self._run_git(["ls-remote", remote_name, f"{prefix}*"], check=False)
        if proc.returncode != 0:
            return []

        locks: list[dict[str, Any]] = []
        for line in (proc.stdout or "").strip().splitlines():
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            sha = parts[0]
            ref = parts[1]
            # Extract ticket_id from ref path
            ticket_segment = ref[len(prefix) :] if ref.startswith(prefix) else ref
            try:
                fetched_sha = self._fetch_ref(remote_name, ref)
                payload = self._parse_lease_payload(self._read_commit_message(fetched_sha))
                expires = _parse_iso(str(payload["expires_at"]))
                payload["expired"] = _now() >= expires
                payload["commit_sha"] = sha
                payload["remote"] = remote_name
                payload["ref"] = ref
                locks.append(payload)
            except (ExoError, Exception):
                # If we can't parse a lock, include minimal info
                locks.append(
                    {
                        "ticket_id": ticket_segment,
                        "ref": ref,
                        "commit_sha": sha,
                        "remote": remote_name,
                        "expired": None,
                        "parse_error": True,
                    }
                )
        return locks

    def cleanup_locks(
        self,
        *,
        remote: str = "origin",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Remove expired distributed locks from the remote.

        Scans all refs under refs/exoprotocol/locks/* on the remote,
        identifies expired leases, and deletes their refs.

        Args:
            remote: Git remote name.
            dry_run: Preview what would be cleaned up without deleting.

        Returns:
            Dict with lists of expired/active/cleaned locks and counts.
        """
        locks = self.list_locks(remote=remote)

        expired: list[dict[str, Any]] = []
        active: list[dict[str, Any]] = []
        cleaned: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for lock in locks:
            if lock.get("parse_error"):
                errors.append(lock)
                continue
            if lock.get("expired"):
                expired.append(lock)
            else:
                active.append(lock)

        if not dry_run:
            for lock in expired:
                ref = str(lock["ref"])
                expected_sha = str(lock["commit_sha"])
                try:
                    self._push_delete(remote, ref, expected_sha)
                    # Also release local lock if it matches
                    ticket_id = str(lock.get("ticket_id", "")).strip()
                    if ticket_id:
                        tickets.release_lock(self.repo, ticket_id=ticket_id)
                    cleaned.append(lock)
                except ExoError:
                    errors.append(lock)

        return {
            "remote": remote,
            "total_locks": len(locks),
            "expired_count": len(expired),
            "active_count": len(active),
            "cleaned_count": len(cleaned) if not dry_run else 0,
            "error_count": len(errors),
            "expired": [
                {
                    "ticket_id": l.get("ticket_id", ""),
                    "owner": l.get("owner", ""),
                    "ref": l.get("ref", ""),
                    "expires_at": l.get("expires_at", ""),
                }
                for l in expired
            ],
            "active": [
                {
                    "ticket_id": l.get("ticket_id", ""),
                    "owner": l.get("owner", ""),
                    "ref": l.get("ref", ""),
                    "expires_at": l.get("expires_at", ""),
                }
                for l in active
            ],
            "cleaned": [{"ticket_id": l.get("ticket_id", ""), "ref": l.get("ref", "")} for l in cleaned],
            "errors": [{"ticket_id": l.get("ticket_id", ""), "ref": l.get("ref", "")} for l in errors],
            "dry_run": dry_run,
        }

    def release(
        self,
        ticket_id: str,
        *,
        owner: str | None = None,
        remote: str = "origin",
        ignore_missing: bool = False,
    ) -> dict[str, Any]:
        remote_name = self._require_remote(remote)
        remote_lock = self._read_remote_lock(remote_name, ticket_id)
        if not remote_lock:
            released_local = tickets.release_lock(self.repo, ticket_id=ticket_id)
            if ignore_missing:
                return {
                    "ticket_id": ticket_id,
                    "released": released_local,
                    "distributed": {
                        "remote": remote_name,
                        "ref": self._lease_ref(ticket_id),
                        "remote_released": False,
                    },
                }
            raise ExoError(
                code="DISTRIBUTED_LOCK_NOT_FOUND",
                message=f"No distributed lock exists for ticket {ticket_id}",
                blocked=True,
            )

        owner_value = owner.strip() if isinstance(owner, str) else ""
        remote_owner = str(remote_lock["owner"])
        if owner_value and owner_value != remote_owner:
            raise ExoError(
                code="DISTRIBUTED_LOCK_OWNER_MISMATCH",
                message=f"Distributed lock owner is {remote_owner}; release owner {owner_value} is not allowed",
                details=remote_lock,
                blocked=True,
            )

        ref = str(remote_lock["ref"])
        expected_sha = str(remote_lock["commit_sha"])
        self._push_delete(remote_name, ref, expected_sha)
        released_local = tickets.release_lock(self.repo, ticket_id=ticket_id)
        return {
            "ticket_id": ticket_id,
            "released": released_local,
            "distributed": {
                "remote": remote_name,
                "ref": ref,
                "remote_released": True,
                "previous_commit_sha": expected_sha,
            },
        }
