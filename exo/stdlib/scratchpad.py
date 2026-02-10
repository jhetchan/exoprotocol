from __future__ import annotations

import re
from pathlib import Path

from exo.kernel.utils import ensure_dir, now_iso


SCRATCHPAD_DIR = Path(".exo/scratchpad")
THREADS_DIR = SCRATCHPAD_DIR / "threads"
INBOX_PATH = SCRATCHPAD_DIR / "INBOX.md"
THREAD_RE = re.compile(r"thread-(\d{4})\.md$")


def append_jot(repo: Path, line: str) -> dict[str, str]:
    inbox = repo / INBOX_PATH
    ensure_dir(inbox.parent)
    if not inbox.exists():
        inbox.write_text("# INBOX\n\n", encoding="utf-8")
    entry = f"- [{now_iso()}] {line}\n"
    with inbox.open("a", encoding="utf-8") as handle:
        handle.write(entry)
    return {"path": str(INBOX_PATH), "entry": entry.strip()}


def _next_thread_id(repo: Path) -> str:
    threads_dir = repo / THREADS_DIR
    ensure_dir(threads_dir)
    seen: list[int] = []
    for path in threads_dir.glob("thread-*.md"):
        match = THREAD_RE.search(path.name)
        if match:
            seen.append(int(match.group(1)))
    nxt = (max(seen) + 1) if seen else 1
    return f"thread-{nxt:04d}"


def create_thread(repo: Path, topic: str) -> dict[str, str]:
    thread_id = _next_thread_id(repo)
    path = repo / THREADS_DIR / f"{thread_id}.md"
    ensure_dir(path.parent)
    body = (
        f"# Thread: {topic}\n\n"
        f"Created: {now_iso()}\n\n"
        "## Notes\n\n"
        "- \n"
    )
    path.write_text(body, encoding="utf-8")
    return {
        "thread_id": thread_id,
        "path": str(path.relative_to(repo)),
    }


def load_thread(repo: Path, thread_id: str) -> tuple[Path, str]:
    path = repo / THREADS_DIR / f"{thread_id}.md"
    if not path.exists():
        raise FileNotFoundError(f"Thread not found: {thread_id}")
    return path, path.read_text(encoding="utf-8")
