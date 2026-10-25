"""Built-in paper trading simulator.

Realistic local fill engine used by demo/paper modes and the backtester:

- market orders: immediate fill at last price (live paper) or next-bar open
  (backtests, to avoid look-ahead), both with configurable slippage
- limit orders: filled when price trades through, gap-aware (fills at the
  open when the bar gaps past the limit)
- bracket exits per position (stop-loss / take-profit); when a single bar
  touches both, the stop is assumed to fill first (conservative)
- cash accounting with margin-style checks for shorts
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from app.brokers.base import Broker, Fill
from app.core.applog import applog
from app.core.domain import (
    AccountSnapshot,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    utcnow,
)


class PaperBroker(Broker):
    name = "paper_sim"
    is_paper = True

    # =============================================================================
    # [MODIFIED 2026-10-25]: Add allow_margin with FINRA 4x DTBP Support to PaperBroker
    # ORIGINAL INTENT: Initialized without allow_margin; long buys were cash-only.
    # REASON FOR CHANGE: Enable FINRA 4x Day Trading Buying Power (DTBP) for PDT accounts
    # (equity >= $25k) matching BetaDesk peak benchmark execution.
    # SAFE ROLLBACK: Previous __init__ preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # def __init__(
    #     self,
    #     starting_cash: float = 100_000.0,
    #     slippage_bps: float = 2.0,
    #     commission_per_share: float = 0.0,
    #     fill_market_on: str = "next_open",
    #     execution_model: str = "deterministic_bar_fill_v1",
    #     max_volume_participation_pct: float | None = None,
    # ) -> None:
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    def __init__(
        self,
        starting_cash: float = 100_000.0,
        slippage_bps: float = 2.0,
        commission_per_share: float = 0.0,
        fill_market_on: str = "next_open",  # immediate | next_open
        execution_model: str = "deterministic_bar_fill_v1",
        max_volume_participation_pct: float | None = None,
        allow_margin: bool = True,
    ) -> None:
        if fill_market_on not in ("immediate", "next_open"):
            raise ValueError("fill_market_on must be 'immediate' or 'next_open'")
        self.starting_cash = float(starting_cash)
        self.cash = float(starting_cash)
        self.slippage = slippage_bps / 10_000.0
        self.commission_per_share = commission_per_share
        self.fill_market_on = fill_market_on
        self.execution_model = execution_model
        self.max_volume_participation_pct = max_volume_participation_pct
        self.allow_margin = allow_margin

        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}
        self._resting: list[str] = []  # unfilled limit + queued market order ids
        self._fills: list[Fill] = []
        self._last_price: dict[str, float] = {}
        self._now: datetime = utcnow()
    # =============================================================================

    # ---- time & prices ----

    def set_time(self, ts: datetime) -> None:
        """Backtester injects simulated time so fills carry market timestamps."""
        self._now = ts

    def _slip(self, price: float, side: OrderSide) -> float:
        return price * (1.0 + self.slippage) if side == OrderSide.BUY else price * (1.0 - self.slippage)

    # ---- accounting ----

    def equity(self) -> float:
        return self.cash + sum(p.qty * p.market_price for p in self._positions.values())

    def _short_notional(self) -> float:
        return sum(-p.qty * p.market_price for p in self._positions.values() if p.qty < 0)

    # =============================================================================
    # [MODIFIED 2026-10-25]: Align Reported Buying Power to Enforced 4x DTBP
    # ORIGINAL INTENT: Reported eq * 2 (2x overnight margin) while enforcing 1x cash.
    # REASON FOR CHANGE: Report active intraday DTBP (4x equity for margin accounts >= $25k).
    # SAFE ROLLBACK: Previous return AccountSnapshot line preserved below as comment.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # async def account(self) -> AccountSnapshot:
    #     eq = self.equity()
    #     return AccountSnapshot(equity=round(eq, 2), cash=round(self.cash, 2), buying_power=round(eq * 2, 2), ts=self._now)
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    async def account(self) -> AccountSnapshot:
        eq = self.equity()
        bp = (eq * 4.0) if (getattr(self, "allow_margin", True) and eq >= 25_000.0) else max(self.cash, 0.0)
        return AccountSnapshot(equity=round(eq, 2), cash=round(self.cash, 2), buying_power=round(bp, 2), ts=self._now)
    # =============================================================================

    async def positions(self) -> list[Position]:
        return [p for p in self._positions.values() if abs(p.qty) > 1e-9]

    async def open_orders(self) -> list[Order]:
        return [self._orders[oid] for oid in self._resting]

    def drain_fills(self) -> list[Fill]:
        fills, self._fills = self._fills, []
        return fills

    async def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        for order in self._orders.values():
            if order.client_order_id == client_order_id:
                return order
        return None

    async def connect(self) -> None:
        applog.info("broker.paper", f"Paper simulator ready (cash ${self.cash:,.0f})")

    # ---- order lifecycle ----

    async def submit(self, req: OrderRequest) -> Order:
        order = Order(
            client_order_id=req.client_order_id,
            symbol=req.symbol.upper(),
            side=req.side,
            qty=float(req.qty),
            order_type=req.order_type,
            limit_price=req.limit_price,
            stop_price=req.stop_price,
            stop_loss=req.stop_loss,
            take_profit=req.take_profit,
            scale_out_target=getattr(req, "scale_out_target", None),
            trail_stop_dist=getattr(req, "trail_stop_dist", None),
            strategy=req.strategy,
            reason=req.reason,
            status=OrderStatus.SUBMITTED,
            submitted_at=self._now,
            signal_timestamp_utc=req.signal_timestamp_utc,
            decision_bar_timestamp_utc=req.decision_bar_timestamp_utc,
        )
        self._orders[order.id] = order

        price = self._last_price.get(order.symbol)
        if price is None:
            order.status = OrderStatus.REJECTED
            order.reason = f"{order.reason} | rejected: no market price for {order.symbol}"
            applog.warning("broker.paper", f"Rejected {order.symbol} {order.side}: no market price")
            return order

        if not self._check_buying_power(order, price):
            order.status = OrderStatus.REJECTED
            order.reason = f"{order.reason} | rejected: insufficient buying power"
            applog.warning("broker.paper", f"Rejected {order.symbol} {order.side} {order.qty}: buying power")
            return order

        if order.order_type == OrderType.MARKET:
            if self.fill_market_on == "immediate" or order.strategy == "manual":
                self._fill(order, self._slip(price, order.side))
            else:
                self._resting.append(order.id)  # fills at next bar open
        elif order.order_type == OrderType.STOP:
            stop_px = order.stop_price
            if stop_px is None:
                raise ValueError("STOP orders require stop_price")
            marketable = (order.side == OrderSide.BUY and price >= stop_px) or (order.side == OrderSide.SELL and price <= stop_px)
            if marketable and self.fill_market_on == "immediate":
                fill_price = self._slip(price, order.side)
                self._fill(order, fill_price)
            else:
                self._resting.append(order.id)
        elif order.order_type == OrderType.STOP_LIMIT:
            stop_px = order.stop_price
            limit_px = order.limit_price
            if stop_px is None or limit_px is None:
                raise ValueError("STOP_LIMIT requires stop_price and limit_price")

            # Phase 1: detect stop trigger.
            if not order.stop_triggered:
                if (
                    order.side == OrderSide.BUY and price >= stop_px
                ) or (
                    order.side == OrderSide.SELL and price <= stop_px
                ):
                    order.stop_triggered = True

            # Phase 2: once triggered, behave exactly like a LIMIT.
            if order.stop_triggered:
                limit_marketable = (
                    order.side == OrderSide.BUY and price <= limit_px
                ) or (
                    order.side == OrderSide.SELL and price >= limit_px
                )

                if limit_marketable and self.fill_market_on == "immediate":
                    fill_price = self._slip(price, order.side)
                    fill_price = (
                        min(fill_price, limit_px)
                        if order.side == OrderSide.BUY
                        else max(fill_price, limit_px)
                    )
                    self._fill(order, fill_price)
                else:
                    self._resting.append(order.id)
            else:
                self._resting.append(order.id)
        else:  # limit
            marketable = order.limit_price is not None and (
                (order.side == OrderSide.BUY and price <= order.limit_price) or (
                    order.side == OrderSide.SELL and price >= order.limit_price
                )
            )
            if marketable and self.fill_market_on == "immediate":
                fill_price = self._slip(price, order.side)
                fill_price = min(fill_price, order.limit_price) if order.side == OrderSide.BUY else max(
                    fill_price, order.limit_price
                )
                self._fill(order, fill_price)
            else:
                self._resting.append(order.id)
        return order

    # =============================================================================
    # [MODIFIED 2026-10-25]: Enforce FINRA 4x Combined Gross Day Trading Buying Power
    # ORIGINAL INTENT: Required notional <= self.cash for long buys, rejecting 78/137 trades.
    # REASON FOR CHANGE: Day trading accounts (equity >= $25k) have 4x DTBP for intraday positions.
    # Checks total combined gross notional (longs + shorts + order) <= 4x equity.
    # SAFE ROLLBACK: Previous _check_buying_power preserved below as comment.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # def _check_buying_power(self, order: Order, price: float) -> bool:
    #     notional = order.qty * price
    #     pos = self._positions.get(order.symbol)
    #     pos_qty = pos.qty if pos else 0.0
    #     if order.side == OrderSide.BUY:
    #         closing = pos_qty < 0  # covering a short frees cash
    #         return closing or notional <= self.cash + 1e-6
    #     opening_short_qty = max(0.0, order.qty - max(pos_qty, 0.0))
    #     if opening_short_qty <= 0:
    #         return True
    #     return self._short_notional() + opening_short_qty * price <= self.equity() + 1e-6
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    def _check_buying_power(self, order: Order, price: float) -> bool:
        notional = order.qty * price
        pos = self._positions.get(order.symbol)
        pos_qty = pos.qty if pos else 0.0
        current_eq = self.equity()
        if order.side == OrderSide.BUY:
            closing = pos_qty < 0  # covering a short frees cash
            if closing:
                return True
            # For margin-enabled PDT accounts (equity >= $25k), standard 4x intraday buying power applies
            if getattr(self, "allow_margin", True):
                max_bp = (4.0 * current_eq) if current_eq >= 25_000.0 else max(self.cash, 0.0)
                total_long_notional = sum(p.qty * self._last_price.get(sym, p.avg_entry_price) for sym, p in self._positions.items() if p.qty > 0)
                total_gross = total_long_notional + self._short_notional() + notional
                return total_gross <= (max_bp + 1e-6)
            return notional <= self.cash + 1e-6

        # sell: reducing a long is always allowed; opening/adding short is margin-checked
        opening_short_qty = max(0.0, order.qty - max(pos_qty, 0.0))
        if opening_short_qty <= 0:
            return True
        max_short_bp = (4.0 * current_eq) if (getattr(self, "allow_margin", True) and current_eq >= 25_000.0) else current_eq
        total_long_notional = sum(p.qty * self._last_price.get(sym, p.avg_entry_price) for sym, p in self._positions.items() if p.qty > 0)
        total_gross = total_long_notional + self._short_notional() + opening_short_qty * price
        return total_gross <= (max_short_bp + 1e-6)
    # =============================================================================

    def _fill(self, order: Order, price: float, fill_qty: float | None = None) -> None:
        qty_to_fill = fill_qty if fill_qty is not None else (order.qty - order.filled_qty)
        if qty_to_fill <= 0:
            return

        commission = self.commission_per_share * qty_to_fill
        signed_qty = qty_to_fill if order.side == OrderSide.BUY else -qty_to_fill
        self.cash -= signed_qty * price
        self.cash -= commission
        self._apply_to_position(order, signed_qty, price)

        order.filled_qty = round(order.filled_qty + qty_to_fill, 4)
        if order.filled_qty >= order.qty - 1e-6:
            order.status = OrderStatus.FILLED
        else:
            order.status = OrderStatus.PARTIALLY_FILLED

        order.filled_avg_price = round(price, 4)
        order.filled_at = self._now
        self._fills.append(
            Fill(
                order_id=order.id,
                symbol=order.symbol,
                side=order.side,
                qty=qty_to_fill,
                price=round(price, 4),
                commission=commission,
                strategy=order.strategy,
                reason=order.reason,
                ts=self._now,
                signal_timestamp_utc=order.signal_timestamp_utc,
                decision_bar_timestamp_utc=order.decision_bar_timestamp_utc,
            )
        )
        applog.debug(
            "broker.paper",
            f"FILL {order.side.upper()} {order.qty:g} {order.symbol} @ {price:.2f} ({order.strategy}: {order.reason})",
        )

    def _apply_to_position(self, order: Order, signed_qty: float, price: float) -> None:
        symbol = order.symbol
        pos = self._positions.get(symbol)
        if pos is None or abs(pos.qty) < 1e-9:
            initial_stop = order.stop_loss
            init_risk = round(abs(price - initial_stop), 4) if initial_stop is not None else order.trail_stop_dist
            self._positions[symbol] = Position(
                symbol=symbol,
                qty=signed_qty,
                avg_entry_price=price,
                market_price=price,
                initial_qty=signed_qty,
                stop_loss=initial_stop,
                take_profit=order.take_profit,
                scale_out_target=order.scale_out_target,
                trail_stop_dist=order.trail_stop_dist,
                trail_high=price,
                initial_risk=init_risk,
                strategy=order.strategy,
                opened_at=self._now,
            )
            return

        same_direction = (pos.qty > 0) == (signed_qty > 0)
        if same_direction:
            total = pos.qty + signed_qty
            pos.avg_entry_price = (pos.avg_entry_price * pos.qty + price * signed_qty) / total
            pos.qty = total
            if pos.initial_qty is None:
                pos.initial_qty = total
            if order.stop_loss is not None:
                if pos.stop_loss is None:
                    pos.stop_loss = order.stop_loss
                elif total > 0:
                    pos.stop_loss = max(pos.stop_loss, order.stop_loss)
                else:
                    pos.stop_loss = min(pos.stop_loss, order.stop_loss)
            if order.take_profit is not None:
                pos.take_profit = order.take_profit
            if order.scale_out_target is not None:
                pos.scale_out_target = order.scale_out_target
            if order.trail_stop_dist is not None:
                pos.trail_stop_dist = order.trail_stop_dist
        else:
            remaining = pos.qty + signed_qty
            if abs(remaining) < 1e-9:
                del self._positions[symbol]
            elif (remaining > 0) == (pos.qty > 0):
                pos.qty = remaining  # partial reduce keeps entry basis
            else:
                # Flip: close the old side entirely, open the remainder fresh
                init_risk = round(abs(price - order.stop_loss), 4) if order.stop_loss is not None else order.trail_stop_dist
                self._positions[symbol] = Position(
                    symbol=symbol,
                    qty=remaining,
                    avg_entry_price=price,
                    market_price=price,
                    initial_qty=remaining,
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                    scale_out_target=order.scale_out_target,
                    trail_stop_dist=order.trail_stop_dist,
                    trail_high=price,
                    initial_risk=init_risk,
                    strategy=order.strategy,
                    opened_at=self._now,
                )
        if symbol in self._positions:
            self._positions[symbol].market_price = price

    async def cancel(self, order_id: str) -> bool:
        if order_id in self._resting:
            self._resting.remove(order_id)
            self._orders[order_id].status = OrderStatus.CANCELED
            return True
        return False

    # =============================================================================
    # [MODIFIED 2026-10-14]: CR-16 & CR-21 - Explicit Close Position Attribution
    # ORIGINAL INTENT: Used bare default reason="manual".
    # REASON FOR CHANGE: Enforce explicit enumerated close reason and avoid bare 'exit' attribution.
    # SAFE ROLLBACK: The original code is removed; rollback via git history.
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    async def close_position(self, symbol: str, reason: str = "manual close", qty: float | None = None) -> Order | None:
        symbol = symbol.upper()
        pos = self._positions.get(symbol)
        if pos is None or abs(pos.qty) < 1e-9:
            return None
        side = OrderSide.SELL if pos.qty > 0 else OrderSide.BUY
        close_qty = min(abs(pos.qty), qty) if qty is not None and qty > 0 else abs(pos.qty)
        clean_reason = (reason or "").strip()
        if not clean_reason or clean_reason == "exit":
            clean_reason = "manual close"
        req = OrderRequest(
            symbol=symbol,
            side=side,
            qty=close_qty,
            order_type=OrderType.MARKET,
            strategy=pos.strategy,
            reason=clean_reason,
        )
    # =============================================================================
        # Position exits always fill immediately at the latest price, even in
        # next_open mode: stops/targets/flatten act on the observed bar.
        order = Order(
            client_order_id=req.client_order_id,
            symbol=symbol,
            side=side,
            qty=close_qty,
            order_type=OrderType.MARKET,
            strategy=pos.strategy,
            reason=reason,
            status=OrderStatus.SUBMITTED,
            submitted_at=self._now,
            leg="exit",
        )
        self._orders[order.id] = order
        price = self._last_price.get(symbol, pos.market_price)
        self._fill(order, self._slip(price, side))
        return order

    async def flatten_all(self, reason: str = "flatten") -> list[Order]:
        orders = []
        for oid in list(self._resting):
            await self.cancel(oid)
        for symbol in list(self._positions):
            order = await self.close_position(symbol, reason=reason)
            if order:
                orders.append(order)
        return orders

    # ---- market simulation ----

    def _advance_trail(self, symbol: str, h: float, low: float) -> None:
        """Ratchet a trailing stop upward (long) or downward (short) each bar."""
        pos = self._positions.get(symbol)
        if pos is None:
            return
        if pos.trail_stop_dist is None:
            return

        if pos.qty > 0:  # long — trail stop rises with new highs
            new_high = max(pos.trail_high or pos.avg_entry_price, h)
            pos.trail_high = new_high
            trail_stop = round(new_high - pos.trail_stop_dist, 2)
            if pos.stop_loss is None or trail_stop > pos.stop_loss:
                pos.stop_loss = trail_stop
        else:  # short — trail stop falls with new lows
            new_low = min(pos.trail_high or pos.avg_entry_price, low)  # reuse field for shorts
            pos.trail_high = new_low
            trail_stop = round(new_low + pos.trail_stop_dist, 2)
            if pos.stop_loss is None or trail_stop < pos.stop_loss:
                pos.stop_loss = trail_stop

    def process_bar(self, symbol: str, bar: pd.Series, ts: datetime) -> None:
        """Advance the simulator with one closed bar (open/high/low/close)."""
        symbol = symbol.upper()
        self._now = ts
        o, h, low, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        vol = float(bar["volume"]) if "volume" in bar else None

        self._fill_resting(symbol, o, h, low, vol=vol)
        self._advance_trail(symbol, h, low)
        self._check_brackets(symbol, o, h, low)
        self._last_price[symbol] = c
        if symbol in self._positions:
            self._positions[symbol].market_price = c

    def update_price(self, symbol: str, price: float, ts: datetime) -> None:
        """Advance with a single trade price (live quote tick)."""
        symbol = symbol.upper()
        self._now = ts
        self._last_price[symbol] = price
        self._fill_resting(symbol, price, price, price)
        self._check_brackets(symbol, price, price, price)
        if symbol in self._positions:
            self._positions[symbol].market_price = price

    def _fill_resting(self, symbol: str, o: float, h: float, low: float, vol: float | None = None) -> None:
        import math
        for oid in list(self._resting):
            order = self._orders[oid]
            if order.symbol != symbol:
                continue

            rem_qty = order.qty - order.filled_qty
            fill_qty = rem_qty
            if self.max_volume_participation_pct is not None and vol is not None and vol > 0:
                max_vol_qty = max(1.0, float(math.floor(vol * self.max_volume_participation_pct)))
                fill_qty = min(rem_qty, max_vol_qty)

            if order.order_type == OrderType.MARKET:
                if not self._check_buying_power(order, o):
                    if oid in self._resting:
                        self._resting.remove(oid)
                    order.status = OrderStatus.REJECTED
                    order.reason += " | rejected: insufficient buying power at fill time"
                    continue
                self._fill(order, self._slip(o, order.side), fill_qty=fill_qty)
                if order.filled_qty >= order.qty - 1e-6:
                    if oid in self._resting:
                        self._resting.remove(oid)
            elif order.order_type == OrderType.STOP:
                stop_px = order.stop_price
                if stop_px is not None:
                    if order.side == OrderSide.BUY and h >= stop_px:
                        self._fill(order, self._slip(max(o, stop_px), order.side), fill_qty=fill_qty)
                        if order.filled_qty >= order.qty - 1e-6:
                            if oid in self._resting:
                                self._resting.remove(oid)
                    elif order.side == OrderSide.SELL and low <= stop_px:
                        self._fill(order, self._slip(min(o, stop_px), order.side), fill_qty=fill_qty)
                        if order.filled_qty >= order.qty - 1e-6:
                            if oid in self._resting:
                                self._resting.remove(oid)
            elif order.order_type == OrderType.STOP_LIMIT:
                stop_px = order.stop_price
                limit_px = order.limit_price

                if stop_px is None or limit_px is None:
                    continue

                # --------------------------------------------------
                # Phase 1 — trigger the STOP.
                # --------------------------------------------------
                if not order.stop_triggered:
                    if (
                        order.side == OrderSide.BUY
                        and h >= stop_px
                    ) or (
                        order.side == OrderSide.SELL
                        and low <= stop_px
                    ):
                        order.stop_triggered = True

                # Stop not triggered yet → remain resting.
                if not order.stop_triggered:
                    continue

                # --------------------------------------------------
                # Phase 2 — now this is effectively a LIMIT order.
                # --------------------------------------------------
                if (
                    order.side == OrderSide.BUY
                    and low <= limit_px
                ):
                    fill_p = self._slip(
                        min(o, limit_px),
                        order.side,
                    )
                    # Never fill BUY above its limit.
                    fill_p = min(fill_p, limit_px)
                    self._fill(
                        order,
                        fill_p,
                        fill_qty=fill_qty,
                    )
                elif (
                    order.side == OrderSide.SELL
                    and h >= limit_px
                ):
                    fill_p = self._slip(
                        max(o, limit_px),
                        order.side,
                    )
                    # Never fill SELL below its limit.
                    fill_p = max(fill_p, limit_px)
                    self._fill(
                        order,
                        fill_p,
                        fill_qty=fill_qty,
                    )

                if order.filled_qty >= order.qty - 1e-6:
                    if oid in self._resting:
                        self._resting.remove(oid)
            elif order.side == OrderSide.BUY and order.limit_price is not None and low <= order.limit_price:
                self._fill(order, min(o, order.limit_price), fill_qty=fill_qty)
                if order.filled_qty >= order.qty - 1e-6:
                    if oid in self._resting:
                        self._resting.remove(oid)
            elif order.side == OrderSide.SELL and order.limit_price is not None and h >= order.limit_price:
                self._fill(order, max(o, order.limit_price), fill_qty=fill_qty)
                if order.filled_qty >= order.qty - 1e-6:
                    if oid in self._resting:
                        self._resting.remove(oid)

    # =============================================================================
    # [MODIFIED 2026-10-25]: 50% Scale-Out Split-Fill Support in _check_brackets
    # ORIGINAL INTENT: Evaluated only full 100% stop_loss or full 100% take_profit.
    # REASON FOR CHANGE: Enable 50% scale-out targets, banking half while runner rides with trailing stop.
    # SAFE ROLLBACK: Previous _check_brackets and _exit_position preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # def _check_brackets(self, symbol: str, o: float, h: float, low: float) -> None:
    #     pos = self._positions.get(symbol)
    #     if pos is None or abs(pos.qty) < 1e-9:
    #         return
    #     if pos.qty > 0:
    #         if pos.stop_loss is not None and low <= pos.stop_loss:
    #             self._exit_position(pos, min(o, pos.stop_loss), "stop_loss", slip_side=OrderSide.SELL)
    #         elif pos.take_profit is not None and h >= pos.take_profit:
    #             self._exit_position(pos, max(o, pos.take_profit), "take_profit")
    #     else:
    #         if pos.stop_loss is not None and h >= pos.stop_loss:
    #             self._exit_position(pos, max(o, pos.stop_loss), "stop_loss", slip_side=OrderSide.BUY)
    #         elif pos.take_profit is not None and low <= pos.take_profit:
    #             self._exit_position(pos, min(o, pos.take_profit), "take_profit")
    #
    # def _exit_position(self, pos: Position, price: float, reason: str, slip_side: OrderSide | None = None) -> None:
    #     ...
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    def _check_brackets(self, symbol: str, o: float, h: float, low: float) -> None:
        pos = self._positions.get(symbol)
        if pos is None or abs(pos.qty) < 1e-9:
            return
        if pos.qty > 0:  # long: stop below, target above
            # 1. Evaluate scale-out target first (banking half on upward expansion)
            if pos.scale_out_target is not None and h >= pos.scale_out_target:
                target_px = pos.scale_out_target
                pos.scale_out_target = None
                self._exit_position(pos, max(o, target_px), "scale_out", scale_factor=0.5)
                # Re-fetch remaining lot after partial fill
                pos = self._positions.get(symbol)
                if pos is None or abs(pos.qty) < 1e-9:
                    return

            # 2. Check full stop loss and take profit for (remaining) position
            if pos.stop_loss is not None and low <= pos.stop_loss:
                self._exit_position(pos, min(o, pos.stop_loss), "stop_loss", slip_side=OrderSide.SELL)
            elif pos.take_profit is not None and h >= pos.take_profit:
                self._exit_position(pos, max(o, pos.take_profit), "take_profit")
        else:  # short
            # 1. Evaluate scale-out target first (banking half on downward expansion)
            if pos.scale_out_target is not None and low <= pos.scale_out_target:
                target_px = pos.scale_out_target
                pos.scale_out_target = None
                self._exit_position(pos, min(o, target_px), "scale_out", scale_factor=0.5)
                pos = self._positions.get(symbol)
                if pos is None or abs(pos.qty) < 1e-9:
                    return

            # 2. Check full stop loss and take profit for (remaining) position
            if pos.stop_loss is not None and h >= pos.stop_loss:
                self._exit_position(pos, max(o, pos.stop_loss), "stop_loss", slip_side=OrderSide.BUY)
            elif pos.take_profit is not None and low <= pos.take_profit:
                self._exit_position(pos, min(o, pos.take_profit), "take_profit")

    def _exit_position(self, pos: Position, price: float, reason: str, slip_side: OrderSide | None = None, scale_factor: float = 1.0) -> None:
        side = OrderSide.SELL if pos.qty > 0 else OrderSide.BUY
        if slip_side is not None:  # stops get adverse slippage; targets fill at the limit
            price = self._slip(price, slip_side)

        exit_qty = abs(pos.qty) * scale_factor

        order = Order(
            symbol=pos.symbol,
            side=side,
            qty=exit_qty,
            order_type=OrderType.MARKET,
            strategy=pos.strategy,
            reason=reason,
            status=OrderStatus.SUBMITTED,
            submitted_at=self._now,
            leg="stop" if reason == "stop_loss" else ("scale_out" if reason == "scale_out" else "target"),
        )
        self._orders[order.id] = order
        self._fill(order, price)
    # =============================================================================
