"""Market-session calendar helpers.

Single source of truth for session boundaries is the XNYS exchange calendar,
exposed via :mod:`app.core.clock`.  This module re-exports the helper so the
historical import path (``app.core.calendar_helper.get_session_close``, used by
the engine and intraday replay) keeps working without a duplicate calendar
implementation.
"""

from __future__ import annotations

from app.core.clock import ET
from app.core.clock import get_xnys_session_close as get_session_close

__all__ = ["ET", "get_session_close"]
