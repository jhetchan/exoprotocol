"""Compatibility shim for control-plane syscalls.

The syscall implementation moved to ``exo.control.syscalls`` to keep
``exo.kernel`` focused on mechanism-only primitives.
"""

from exo.control.syscalls import KernelSyscalls

__all__ = ["KernelSyscalls"]
