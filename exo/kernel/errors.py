from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ExoError(Exception):
    code: str
    message: str
    details: dict[str, Any] | None = None
    blocked: bool = False

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details or {},
            "blocked": self.blocked,
        }
