"""Runtime adapters. get_runtime() is the only way activities reach a runtime."""
from __future__ import annotations

from .base import Handle, Role, Runtime, RuntimeError_

_cache: dict[str, Runtime] = {}


def get_runtime(name: str) -> Runtime:
    if name not in _cache:
        if name == "openshell":
            from .openshell import OpenShellRuntime as cls
        elif name == "substrate":
            from .substrate import SubstrateRuntime as cls
        elif name == "local":
            from .local import LocalRuntime as cls
        else:
            raise ValueError(f"unknown runtime {name!r}; expected openshell | substrate | local")
        _cache[name] = cls()
    return _cache[name]


__all__ = ["Handle", "Role", "Runtime", "RuntimeError_", "get_runtime"]
