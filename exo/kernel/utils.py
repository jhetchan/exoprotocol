from __future__ import annotations

import fnmatch
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, data: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_yaml(path: Path) -> dict[str, Any]:
    data = parse_yaml_like(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def dump_yaml(path: Path, data: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        return

    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=False), encoding="utf-8")


def parse_yaml_like(text: str) -> Any:
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:
        return json.loads(text)
    return yaml.safe_load(text)


def relative_posix(path: Path, repo: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def matches_pattern(path: Path, pattern: str, repo: Path) -> bool:
    rel = relative_posix(path, repo)
    abs_path = path.resolve().as_posix()
    expanded = Path(pattern).expanduser().as_posix()

    if pattern.startswith("~/"):
        return abs_path.startswith(expanded)
    if pattern.startswith("/"):
        return fnmatch.fnmatch(abs_path, pattern)

    return fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f"/{rel}", pattern)


def any_pattern_matches(path: Path, patterns: list[str], repo: Path) -> bool:
    return any(matches_pattern(path, pat, repo) for pat in patterns)


def default_topic_id(repo: Path) -> str:
    """Return a portable, privacy-safe default topic ID for a repo.

    Uses ``repo:default`` instead of embedding the absolute local path.
    """
    return "repo:default"
