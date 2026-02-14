from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .types import AuditRef
from .utils import ensure_dir, now_iso, relative_posix, sha256_text
from .version import KERNEL_NAME, KERNEL_VERSION

AUDIT_LOG_PATH = Path(".exo/logs/audit.log.jsonl")


def append_audit_event(repo: Path, event: dict[str, Any]) -> None:
    append_audit(repo, event)


def append_audit(root: Path | str, event: dict[str, Any]) -> AuditRef:
    repo = Path(root).resolve()
    log_path = repo / AUDIT_LOG_PATH
    ensure_dir(log_path.parent)
    payload = dict(event)
    if not isinstance(payload.get("ts"), str):
        payload["ts"] = now_iso()
    if not isinstance(payload.get("kernel_name"), str):
        payload["kernel_name"] = KERNEL_NAME
    if not isinstance(payload.get("kernel_version"), str):
        payload["kernel_version"] = KERNEL_VERSION

    line = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")

    with log_path.open("r", encoding="utf-8") as handle:
        line_no = sum(1 for _ in handle)

    return AuditRef(
        log_path=relative_posix(log_path, repo),
        line=line_no,
        event_hash=sha256_text(line),
        ts=str(payload.get("ts")),
    )


def event_template(
    actor: str,
    action: str,
    result: str,
    *,
    ticket: str | None = None,
    path: str | None = None,
    rule: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "ts": now_iso(),
        "actor": actor,
        "action": action,
        "result": result,
        "kernel_name": KERNEL_NAME,
        "kernel_version": KERNEL_VERSION,
    }
    if ticket:
        event["ticket"] = ticket
    if path:
        event["path"] = path
    if rule:
        event["rule"] = rule
    if details:
        event["details"] = details
    return event
