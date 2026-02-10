from __future__ import annotations

from pathlib import Path
from typing import Any

from exo.kernel import governance


def run(payload: dict[str, Any]) -> dict[str, Any]:
    repo = Path(payload.get("repo", ".")).resolve()
    lock_data = governance.compile_constitution(repo)
    return {"lock_data": lock_data}
