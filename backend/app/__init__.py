"""AlphaDesk -- local-first AI day trading platform."""

__version__ = "1.0.0"

# AlphaDesk 007 installs final production guardrails at package import time so
# every engine/backtest caller receives the same risk, cooldown, and trade-
# telemetry rules and the simplified execution path.
from app.core.alphadesk_007_guard import install as _install_alphadesk_007_guard
from app.core.alphadesk_007_entry_guard import install as _install_alphadesk_007_entry_guard
from app.core.alphadesk_007_refactor import install as _install_alphadesk_007_refactor

_install_alphadesk_007_guard()
_install_alphadesk_007_entry_guard()
_install_alphadesk_007_refactor()
