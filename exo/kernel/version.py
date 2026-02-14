from __future__ import annotations

KERNEL_NAME = "exo-kernel"
KERNEL_VERSION = "0.1.0"


def major_of(version: str) -> int | None:
    parts = str(version).strip().split(".")
    if len(parts) != 3:
        return None
    if not all(part.isdigit() for part in parts):
        return None
    return int(parts[0])


def is_supported_kernel_version(version: str) -> bool:
    expected = major_of(KERNEL_VERSION)
    actual = major_of(version)
    if expected is None or actual is None:
        return False
    return expected == actual
