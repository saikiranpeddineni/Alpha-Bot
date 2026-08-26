"""Structured application logging.

Log records go to three places: stdlib logging (console + rotating file), an
in-memory ring buffer served over the REST API, and any subscribed sinks (the
WebSocket hub subscribes so the UI log panel streams in real time).
"""

from __future__ import annotations

import contextlib
import logging
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from pydantic import BaseModel

_std = logging.getLogger("alphadesk")


class LogEntry(BaseModel):
    ts: datetime
    level: str
    source: str
    message: str


class AppLog:
    """Ring-buffered app log with pluggable broadcast sinks."""

    def __init__(self, capacity: int = 5000) -> None:
        self._buffer: deque[LogEntry] = deque(maxlen=capacity)
        self._sinks: list[Callable[[LogEntry], None]] = []
        self._lock = Lock()

    def subscribe(self, sink: Callable[[LogEntry], None]) -> None:
        self._sinks.append(sink)

    def log(self, level: str, source: str, message: str) -> LogEntry:
        entry = LogEntry(ts=datetime.now(UTC), level=level, source=source, message=message)
        with self._lock:
            self._buffer.append(entry)
        console_msg = message
        if source == "portfolio":
            msg_upper = message.upper()
            if any(x in msg_upper for x in ("BOUGHT", "SOLD SHORT", "BOUGHT MORE", "SOLD MORE SHORT")) and "P&L" not in msg_upper:
                console_msg = f"\033[33m{message}\033[0m"  # Yellow (universally compatible \033[33m)
            elif "P&L: +" in msg_upper:
                console_msg = f"\033[92m{message}\033[0m"  # Green
            elif "P&L: -" in msg_upper:
                console_msg = f"\033[91m{message}\033[0m"  # Red

        getattr(_std, level if level != "trade" else "info")(f"[{source}] {console_msg}")
        for sink in list(self._sinks):
            # A broken sink must never kill the engine
            with contextlib.suppress(Exception):
                sink(entry)
        return entry

    def debug(self, source: str, message: str) -> LogEntry:
        return self.log("debug", source, message)

    def info(self, source: str, message: str) -> LogEntry:
        return self.log("info", source, message)

    def warning(self, source: str, message: str) -> LogEntry:
        return self.log("warning", source, message)

    def error(self, source: str, message: str) -> LogEntry:
        return self.log("error", source, message)

    def trade(self, source: str, message: str) -> LogEntry:
        """Trade-level events, highlighted in the UI."""
        return self.log("trade", source, message)

    def recent(self, limit: int = 200) -> list[LogEntry]:
        with self._lock:
            items = list(self._buffer)
        return items[-limit:]


def configure_std_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy third-party loggers
    for noisy in ("httpx", "urllib3", "yfinance", "peewee", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def configure_file_logging(level: int = logging.INFO) -> None:
    """Persist the alphadesk log stream to a rotating file.

    Console output only survives when the server happens to be launched with a
    redirect (start.ps1); a plain `uvicorn app.main:app` session leaves no
    forensic trail at all (e.g. 2026-08-25). This sink is always-on so every
    launch mode writes data/logs/app.log regardless of how stdout is handled.
    Disable with ALPHADESK_FILE_LOG=0; relocate with ALPHADESK_LOG_FILE.
    """
    import os

    if os.environ.get("ALPHADESK_FILE_LOG", "1").strip().lower() in ("0", "false", "off"):
        return
    from logging.handlers import RotatingFileHandler

    from app.config import DATA_DIR

    path = Path(os.environ.get("ALPHADESK_LOG_FILE", str(DATA_DIR / "logs" / "app.log")))
    path.parent.mkdir(parents=True, exist_ok=True)

    std = logging.getLogger("alphadesk")
    for existing in std.handlers:
        if isinstance(existing, RotatingFileHandler) and getattr(existing, "_alphadesk_sink", False):
            return
    handler = RotatingFileHandler(
        path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler._alphadesk_sink = True  # type: ignore[attr-defined]
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    std.addHandler(handler)
    std.setLevel(level)


applog = AppLog()
