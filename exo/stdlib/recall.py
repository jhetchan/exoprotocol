from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any


def _run_command(args: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _search_with_rg(repo: Path, query: str, paths: list[str]) -> list[dict[str, Any]]:
    if not shutil.which("rg"):
        return []

    existing_paths = [str(repo / p) for p in paths if (repo / p).exists()]
    if not existing_paths:
        return []

    args = [
        "rg",
        "-n",
        "--column",
        "--hidden",
        "--no-heading",
        query,
        *existing_paths,
    ]
    rc, out, _ = _run_command(args, repo)
    if rc not in (0, 1):
        return []

    hits: list[dict[str, Any]] = []
    for line in out.splitlines()[:200]:
        parts = line.split(":", 3)
        if len(parts) < 3:
            continue
        if len(parts) == 3:
            file_path, line_no, content = parts
            col_no = "1"
        else:
            file_path, line_no, col_no, content = parts
        path_obj = Path(file_path)
        try:
            rel = str(path_obj.resolve().relative_to(repo.resolve()))
        except ValueError:
            rel = str(path_obj)
        hits.append(
            {
                "path": rel,
                "line": int(line_no),
                "column": int(col_no),
                "content": content.strip(),
            }
        )
    return hits


def _search_git_history(repo: Path, query: str) -> list[str]:
    if not (repo / ".git").exists():
        return []
    rc, out, _ = _run_command(["git", "log", "--oneline", "--grep", query, "-n", "20"], repo)
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def recall(repo: Path, query: str, paths: list[str] | None = None) -> dict[str, Any]:
    search_paths = paths or [".exo", "docs"]
    text_hits = _search_with_rg(repo, query, search_paths)
    git_hits = _search_git_history(repo, query)
    return {
        "query": query,
        "paths": search_paths,
        "text_hits": text_hits[:50],
        "git_hits": git_hits,
        "counts": {
            "text": len(text_hits[:50]),
            "git": len(git_hits),
        },
    }
