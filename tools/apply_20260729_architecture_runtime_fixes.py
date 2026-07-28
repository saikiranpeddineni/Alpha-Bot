"""Apply surgical runtime-ownership fixes discovered by the architecture audit.

Run from repository root, then run tools/apply_20260729_architecture_audit_tests.py
before the final backend test suite.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
text = ENGINE.read_text(encoding="utf-8")


def replace_once(old: str, new: str, label: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    text = text.replace(old, new, 1)


# Singular scanner/task ownership in __init__.
replace_once(
'''        self.scanned_watchlist: list[str] = []
        self.scanned_watchlist_ttl: dict[str, int] = {}
        self.watchlist: list[str] = sorted(list(set(self.user_watchlist + self.scanned_watchlist)))
''',
'''        self.scanned_watchlist: list[str] = []
        self.scanned_watchlist_ttl: dict[str, int] = {}
        self.scanned_watchlist_expires_at: dict[str, datetime] = {}
        self.watchlist: list[str] = sorted(list(set(self.user_watchlist + self.scanned_watchlist)))
''',
"scanner state canonical initialization",
)
replace_once(
'''        # Scanner state initialization
        self.scanned_watchlist: list[str] = []
        self.scanned_watchlist_ttl: dict[str, int] = {}
        self.scanned_watchlist_expires_at: dict[str, datetime] = {}

''',
'''        # Slow scheduled work is explicitly detached from the market-data/execution tick.
        self._scanner_task: asyncio.Task | None = None
        self._trainer_task: asyncio.Task | None = None

''',
"remove duplicate scanner initialization",
)

# Telemetry timestamps represent observed events only, not engine construction.
replace_once(
'''        now_iso = datetime.now(timezone.utc).isoformat()
        self._subsystem_timestamps: dict[str, str] = {
            "last_scan": now_iso,
            "last_bar": now_iso,
            "last_order": now_iso,
            "last_strategy": now_iso,
            "last_replay": now_iso,
            "last_training": now_iso,
            "last_broker_ping": now_iso,
        }
''',
'''        self._subsystem_timestamps: dict[str, str | None] = {
            "last_scan": None,
            "last_bar": None,
            "last_order": None,
            "last_strategy": None,
            "last_replay": None,
            "last_training": None,
            "last_broker_ping": None,
        }
        self._last_broker_latency_ms: float | None = None
''',
"telemetry initialization",
)

# Cancel detached scheduled work during engine shutdown.
replace_once(
'''        self._event_monitor_task = None
        # fix(#56): cancel emergency flatten task when engine is explicitly stopped
''',
'''        self._event_monitor_task = None
        for attr in ("_scanner_task", "_trainer_task"):
            task = getattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            setattr(self, attr, None)
        # fix(#56): cancel emergency flatten task when engine is explicitly stopped
''',
"scheduled task shutdown",
)

# One canonical real-broker fill poll per tick: after data polling and immediately
# before portfolio/account reconciliation. Remove the pre-heartbeat duplicate.
replace_once(
'''        # Poll real fills at the start of the tick to prevent position reconciliation race conditions
        from app.brokers.paper import PaperBroker
        if not isinstance(self.broker, PaperBroker) and hasattr(self.broker, "poll_fills"):
            await self.broker.poll_fills()
            await self._drain_fills(now)

''',
'''        from app.brokers.paper import PaperBroker

''',
"duplicate fill poll",
)

# Scanner/trainer are slow scheduled jobs. Schedule once and let the tick continue.
replace_once(
'''                self._last_scan_date = today_date
                applog.info("engine", "Pre-market scanner trigger time reached (>= 09:00 ET)")
                await self.run_market_scanner(is_intraday=False)
''',
'''                self._last_scan_date = today_date
                applog.info("engine", "Pre-market scanner trigger time reached (>= 09:00 ET)")
                if self._scanner_task is None or self._scanner_task.done():
                    self._scanner_task = asyncio.create_task(
                        self.run_market_scanner(is_intraday=False), name="market-scanner"
                    )
''',
"pre-market scanner detachment",
)
replace_once(
'''                self._last_intraday_scan_min = et_mins
                applog.info("engine", f"Intraday watchlist refresh triggered at {et_mins} ET mins")
                await self.run_market_scanner(is_intraday=True)
''',
'''                self._last_intraday_scan_min = et_mins
                applog.info("engine", f"Intraday watchlist refresh triggered at {et_mins} ET mins")
                if self._scanner_task is None or self._scanner_task.done():
                    self._scanner_task = asyncio.create_task(
                        self.run_market_scanner(is_intraday=True), name="market-scanner-intraday"
                    )
                else:
                    applog.info("engine", "Intraday scanner skipped: previous scanner task still running")
''',
"intraday scanner detachment",
)
replace_once(
'''                self._last_train_date = today_date
                applog.info("engine", "Post-market trainer trigger time reached (>= 16:15 ET)")
                await self.trainer.trigger(self.watchlist)
''',
'''                self._last_train_date = today_date
                applog.info("engine", "Post-market trainer trigger time reached (>= 16:15 ET)")
                if self._trainer_task is None or self._trainer_task.done():
                    self._trainer_task = asyncio.create_task(
                        self.trainer.trigger(list(self.watchlist)), name="post-market-trainer"
                    )
''',
"trainer detachment",
)

# Remove the shadowed/dead first Rule-41 implementation, leaving one authority.
start = text.find('    async def verify_broker_protective_stops(self, positions: list[Position]) -> dict[str, bool]:')
marker = text.find('    def _resolve_recovery_stop(', start)
if start < 0 or marker < 0:
    raise RuntimeError("Rule-41 dead verifier block not found")
text = text[:start] + text[marker:]

# Telemetry rendering is read-only with respect to broker health.
replace_once(
'''        now_iso = datetime.now(timezone.utc).isoformat()
        self._subsystem_timestamps["last_broker_ping"] = now_iso

''',
'''        now_iso = datetime.now(timezone.utc).isoformat()

''',
"telemetry broker timestamp fabrication",
)
replace_once(
'''                "latency_ms": 15,
''',
'''                "latency_ms": self._last_broker_latency_ms,
''',
"telemetry latency fabrication",
)

ENGINE.write_text(text, encoding="utf-8")
compile(text, str(ENGINE), "exec")
print("Applied deterministic tick-ownership fixes; engine.py syntax compile passed.")
