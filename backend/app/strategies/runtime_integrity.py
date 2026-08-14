"""Legacy compatibility hook.

Production 007 no longer installs runtime monkey-patches.  The live path is
explicitly wired through the production strategy registry and engine.
"""
from __future__ import annotations

from typing import Any

_CURRENT_MOVER_CONTEXT: dict[str, dict[str, Any]] = {}


def install_runtime_integrity() -> None:
    """Deprecated no-op retained only for API compatibility."""
    return None


def get_dynamic_mover_context(symbol: str) -> dict[str, Any]:
    """Return legacy mover context when supplied by an external caller."""
    return dict(_CURRENT_MOVER_CONTEXT.get(symbol.upper(), {}))
