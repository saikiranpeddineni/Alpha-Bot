"""Apply second-pass runtime fixes from the lifecycle/isolation audit.

Run after apply_20260729_architecture_runtime_fixes.py.  The changes preserve
broker truth as authority, keep partial fills partial, and prevent research
replay from owning the production tick.
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


# Research replay is non-authoritative and may perform historical network I/O.
# It must never be awaited by the market-data/execution clock.
replace_once(
'''        self._scanner_task: asyncio.Task | None = None
        self._trainer_task: asyncio.Task | None = None
''',
'''        self._scanner_task: asyncio.Task | None = None
        self._trainer_task: asyncio.Task | None = None
        self._shadow_replay_task: asyncio.Task | None = None
''',
"shadow replay task state",
)
replace_once(
'''        for attr in ("_scanner_task", "_trainer_task"):
''',
'''        for attr in ("_scanner_task", "_trainer_task", "_shadow_replay_task"):
''',
"shadow replay shutdown ownership",
)
replace_once(
'''        # 5.5 Periodic background replay of pending shadow signals
        if self._tick_count % 10 == 0:
            try:
                await self._replay_pending_signals(now)
            except Exception as e:
                applog.error("engine", f"Background replay error: {e}")
''',
'''        # 5.5 Periodic shadow replay. Historical-data I/O is research-only and
        # cannot own or delay the production market-data/execution tick.
        if self._tick_count % 10 == 0:
            task = self._shadow_replay_task
            if task is None or task.done():
                if task is not None and not task.cancelled():
                    try:
                        task.result()
                    except Exception as exc:
                        applog.error("engine", f"Background replay error: {exc}")
                self._shadow_replay_task = asyncio.create_task(
                    self._replay_pending_signals(now), name="shadow-replay"
                )
''',
"detach shadow replay",
)

# Order snapshots must reflect cumulative execution, not mark an order FILLED
# on the first partial execution fragment.
replace_once(
'''            if order is not None and order.status != OrderStatus.FILLED:
                # Paper orders share objects and are already updated; Alpaca
                # orders are snapshots that need their fill state applied.
                order.status = OrderStatus.FILLED
                order.filled_qty = fill.qty
                order.filled_avg_price = fill.price
                order.filled_at = fill.ts
            
            symbol = fill.symbol.upper()
            # Release pending-close guard when sell fill arrives
            if fill.side == OrderSide.SELL:
                self._pending_close_symbols.discard(symbol)
''',
'''            if order is not None and order.status != OrderStatus.FILLED:
                # Alpaca fill events are incremental fragments with a cumulative
                # watermark. Preserve PARTIALLY_FILLED until cumulative execution
                # reaches the broker order quantity.
                cumulative = float(getattr(fill, "cumulative_qty", 0.0) or 0.0)
                if cumulative <= 0:
                    cumulative = float(getattr(order, "filled_qty", 0.0) or 0.0) + float(fill.qty)
                order.filled_qty = min(float(order.qty), cumulative)
                order.status = (
                    OrderStatus.FILLED
                    if order.filled_qty + 1e-9 >= float(order.qty)
                    else OrderStatus.PARTIALLY_FILLED
                )
                order.filled_avg_price = fill.price
                order.filled_at = fill.ts if order.status == OrderStatus.FILLED else None

            symbol = fill.symbol.upper()
''',
"partial fill order state",
)

# Release a close/re-entry guard only when durable local position state proves
# the symbol flat. This is side-neutral (long exits and short covers) and does
# not release on a partial fill.
replace_once(
'''            ack_fill = getattr(self.broker, "ack_fill_processed", None)
            if ack_fill is not None:
                ack_fill(fill)
            if trade is not None:
''',
'''            ack_fill = getattr(self.broker, "ack_fill_processed", None)
            if ack_fill is not None:
                ack_fill(fill)
            local_lot = getattr(self.portfolio, "_lots", {}).get(symbol)
            if local_lot is None or abs(float(getattr(local_lot, "qty", 0.0))) < 1e-9:
                self._pending_close_symbols.discard(symbol)
            if trade is not None:
''',
"close guard durable release",
)

# Heartbeat-triggered emergency flatten must share the single retained task
# owner used by the engine error path; otherwise repeated DB-write failures can
# spawn concurrent liquidation loops.
replace_once(
'''            asyncio.create_task(
                self._emergency_flatten_loop(),
                name="trading-engine-emergency-flatten"
            )
''',
'''            if self._emergency_flatten_task is None or self._emergency_flatten_task.done():
                self._emergency_flatten_task = asyncio.create_task(
                    self._emergency_flatten_loop(),
                    name="trading-engine-emergency-flatten"
                )
''',
"heartbeat emergency flatten ownership",
)

ENGINE.write_text(text, encoding="utf-8")
compile(text, str(ENGINE), "exec")
print("Applied fill-lifecycle/research-isolation fixes; engine.py syntax compile passed.")
