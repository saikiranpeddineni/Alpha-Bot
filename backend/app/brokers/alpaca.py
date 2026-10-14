"""Alpaca broker adapter (alpaca-py SDK).

Maps the platform's order model onto Alpaca's REST API. Entries with a stop
and target become native bracket orders so protective exits live server-side
at the broker even if this machine goes offline. Fills are detected by
polling order state each engine tick and surfaced through drain_fills().

All Alpaca SDK calls are wrapped with asyncio.wait_for(asyncio.to_thread(...))
using a bounded wall-clock budget (ALPACA_API_TIMEOUT_SECONDS, default 10 s).
A timeout raises BrokerTimeoutError so callers treat it as an ambiguous state
rather than success or clean failure, preserving engine-tick liveness.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from app.brokers.base import Broker, BrokerTimeoutError, Fill
from app.core.applog import applog
from app.core.domain import (
    AccountSnapshot,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)

if TYPE_CHECKING:
    pass

# Wall-clock budget for every Alpaca HTTP call. Tune via env var if needed.
_API_TIMEOUT: float = float(os.environ.get("ALPACA_API_TIMEOUT_SECONDS", "10"))

_STATUS_MAP = {
    "new": OrderStatus.SUBMITTED,
    "accepted": OrderStatus.SUBMITTED,
    "pending_new": OrderStatus.SUBMITTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
}


async def _call(fn, *args, timeout: float = _API_TIMEOUT, label: str = "", max_rate_retries: int = 3, **kwargs):
    """Run a synchronous Alpaca SDK call in a thread with a bounded timeout and rate-limit resilience.

    Raises BrokerTimeoutError (not asyncio.TimeoutError) so callers can
    distinguish this from other failures without catching the base exception.
    """
    try:
        from app.data.alpaca_data import _get_rate_limiter
        await _get_rate_limiter().acquire()
    except Exception as exc:
        applog.warning("broker.alpaca", f"Failed to acquire global rate limiter: {exc}")

    for attempt in range(max_rate_retries + 1):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fn, *args, **kwargs),
                timeout=timeout,
            )
        except TimeoutError as exc:
            if attempt < max_rate_retries:
                backoff = (2 ** attempt) * 0.5 + 0.1
                applog.warning(
                    "broker.alpaca",
                    f"Alpaca API call timed out [{label}], backing off for {backoff:.2f}s (attempt {attempt + 1}/{max_rate_retries})",
                )
                await asyncio.sleep(backoff)
                continue
            msg = f"Alpaca API call timed out after {timeout:.0f}s"
            if label:
                msg += f" [{label}]"
            applog.error("broker.alpaca", msg)
            raise BrokerTimeoutError(msg) from exc
        except Exception as exc:
            err_str = str(exc).lower()
            is_rate_limit = (
                "429" in err_str
                or "too many requests" in err_str
                or getattr(exc, "status_code", None) == 429
                or getattr(exc, "code", None) == 429
            )
            if is_rate_limit and attempt < max_rate_retries:
                backoff = (2 ** attempt) * 0.5 + 0.1
                applog.warning(
                    "broker.alpaca",
                    f"Alpaca rate limit encountered [{label}], backing off for {backoff:.2f}s (attempt {attempt + 1}/{max_rate_retries})",
                )
                await asyncio.sleep(backoff)
                continue
            raise


async def _paginate_orders(client: object, request: object, label: str = "get_orders") -> list:
    """fix(#50): Paginate Alpaca get_orders until no new orders are returned.

    Handles deduplication across overlapping pages, orders sharing identical timestamps,
    and multi-page results without skipping orders sharing the cursor timestamp.
    """
    from alpaca.trading.requests import GetOrdersRequest
    page_size: int = getattr(request, "limit", 500) or 500
    all_orders: list = []
    seen_ids: set[str] = set()
    current_after = getattr(request, "after", None)

    while True:
        paged_req = GetOrdersRequest(
            status=getattr(request, "status", "all"),
            after=current_after,
            limit=page_size,
        )
        page = await _call(client.get_orders, paged_req, label=f"{label}/page{len(all_orders)//page_size + 1}")
        if not page:
            break

        new_count = 0
        for o in page:
            oid = str(getattr(o, "id", ""))
            if oid and oid not in seen_ids:
                seen_ids.add(oid)
                all_orders.append(o)
                new_count += 1

        if new_count == 0 or len(page) < page_size:
            break

        last = page[-1]
        ts = getattr(last, "filled_at", None) or getattr(last, "updated_at", None) or getattr(last, "submitted_at", None)
        if ts is None:
            break
        current_after = ts

    return all_orders



class AlpacaBroker(Broker):
    name = "alpaca"

    def __init__(self, api_key: str, api_secret: str, paper: bool = True) -> None:
        if not (api_key and api_secret):
            raise RuntimeError("Alpaca broker requires ALPACA_API_KEY and ALPACA_API_SECRET")
        try:
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("alpaca-py is not installed; run the install script") from exc
        self.is_paper = paper
        self._client = TradingClient(api_key, api_secret, paper=paper)
        self._fills: list[Fill] = []
        self._emitted_fill_ids: set[str] = set()
        # Acknowledged watermarks advance only after PortfolioTracker commits.
        self._processed_fill_qty: dict[str, float] = {}
        self._processed_fill_notional: dict[str, float] = {}
        # In-flight watermarks prevent a repeated Alpaca cumulative snapshot from
        # being emitted twice while the previous Fill awaits durable processing.
        self._emitted_fill_qty: dict[str, float] = {}
        self._emitted_fill_notional: dict[str, float] = {}
        self._meta: dict[str, tuple[str, str]] = {}  # order id -> (strategy, reason)
        # Alpaca's nested Order model exposes a child relationship through the
        # parent's ``legs`` list but does not consistently expose a parent ID on
        # the child object.  Keep that broker relationship by child ID so the
        # conversion layer can classify flattened nested children correctly.
        self._order_parent_ids: dict[str, str] = {}

        self._symbol_strategies: dict[str, str] = {}
        self._shortable_cache: dict[str, bool] = {}
        self._shorting_enabled: bool | None = None
        self.account_id: str | None = None

    @property
    def _broker_mode(self) -> str:
        return "paper" if self.is_paper else "live"

    async def connect(self) -> None:
        acct = await _call(self._client.get_account, label="get_account")
        self.account_id = str(acct.account_number)
        self._shorting_enabled = bool(getattr(acct, "shorting_enabled", True))
        applog.info(
            "broker.alpaca",
            f"Connected to Alpaca ({'paper' if self.is_paper else 'LIVE'}) account {self.account_id} -- equity ${float(acct.equity):,.2f} (shorting_enabled={self._shorting_enabled})",
        )
        await self._bootstrap_durable_fill_baseline()

    async def _bootstrap_durable_fill_baseline(self) -> None:
        """One-time migration baseline; existing installations must not replay old history."""
        from app.db.database import Database
        db = Database.active()
        if db is None or db.has_portfolio_checkpoint(self._broker_mode):
            return
        try:
            from alpaca.trading.requests import GetOrdersRequest
            raw_positions = await _call(self._client.get_all_positions, label="get_all_positions")
            # fix(#47): extend lookback to 30 days to survive extended downtime >24h
            after = datetime.now(UTC) - timedelta(days=30)
            # fix(#50): paginate until fewer than page_size records returned (no silent omissions)
            orders = await _paginate_orders(self._client, GetOrdersRequest(status="all", after=after, limit=500), label="get_orders/bootstrap")
            lots = {}
            for p in raw_positions:
                qty = float(p.qty)
                if str(p.side) == "PositionSide.SHORT" or qty < 0:
                    qty = -abs(qty)
                sym = p.symbol.upper()
                avg_price = float(p.avg_entry_price)

                # Reconstruct protective stop loss and initial risk per share
                stop_loss = None
                for o in orders:
                    o_sym = getattr(o, "symbol", "")
                    if o_sym and o_sym.upper() == sym:
                        st_price = getattr(o, "stop_price", None)
                        if st_price is not None:
                            try:
                                stop_loss = float(st_price)
                                break
                            except (ValueError, TypeError):
                                pass

                if stop_loss is None or stop_loss <= 0:
                    # Safe fallback stop loss: 2% from avg entry price
                    stop_pct = 0.02
                    stop_loss = round(avg_price * (1.0 - stop_pct), 4) if qty > 0 else round(avg_price * (1.0 + stop_pct), 4)

                risk_per_share = round(abs(avg_price - stop_loss), 4)
                if risk_per_share <= 0:
                    risk_per_share = round(avg_price * 0.02, 4)

                lots[sym] = {
                    "qty": qty,
                    "avg_price": avg_price,
                    "stop_loss": stop_loss,
                    "initial_risk_per_share": risk_per_share,
                }
            checkpoint = {
                "account_id": self.account_id,
                "realized_pnl": 0.0,
                "day_realized_pnl": 0.0,
                "session_date": None,
                "session_start_equity": None,
                "peak_equity": float(acct.equity) if (acct := await _call(self._client.get_account, label="get_account")) else 0.0,
                "day_trades_today": 0,
                "trades": [],
                "equity_curve": [],
                "lots": lots,
            }
            watermarks = {
                str(o.id): float(o.filled_qty or 0)
                for o in orders if float(o.filled_qty or 0) > 0
            }
            db.bootstrap_broker_checkpoint(mode=self._broker_mode, account_id=self.account_id, checkpoint=checkpoint, fill_watermarks=watermarks)
            applog.info("broker.alpaca", "Initialized durable Alpaca fill baseline from authoritative broker state")
        except BrokerTimeoutError:
            applog.error(
                "broker.alpaca",
                "Bootstrap FAILED: Alpaca API timed out during durable fill baseline initialization. "
                "Engine start aborted to avoid operating without authoritative baseline.",
            )
            raise
        except Exception as exc:
            applog.warning("broker.alpaca", f"failed to initialize durable fill baseline: {exc}")

    async def account(self) -> AccountSnapshot:
        acct = await _call(self._client.get_account, label="get_account")
        return AccountSnapshot(
            equity=float(acct.equity),
            cash=float(acct.cash),
            buying_power=float(acct.buying_power),
        )

    async def positions(self) -> list[Position]:
        """Return authoritative broker positions.

        fix(#42): Positions are ALWAYS derived from the live Alpaca API
        (get_all_positions). DB checkpoints are strictly internal accounting
        state and must never override broker-authoritative qty, side, or
        symbol membership. This prevents ghost lots, hidden real exposure,
        stale qty, and direction inversions after restarts.
        """
        raw = await _call(self._client.get_all_positions, label="get_all_positions")
        out = []
        for p in raw:
            qty = float(p.qty)
            if str(p.side) == "PositionSide.SHORT" or qty < 0:
                qty = -abs(qty)
            out.append(
                Position(
                    symbol=p.symbol,
                    qty=qty,
                    avg_entry_price=float(p.avg_entry_price),
                    market_price=float(p.current_price or p.avg_entry_price),
                    strategy=self._symbol_strategies.get(p.symbol, "alpaca"),
                )
            )
        return out

    async def open_orders(self) -> list[Order]:
        from alpaca.trading.requests import GetOrdersRequest

        raw = await _call(
            self._client.get_orders,
            GetOrdersRequest(status="open", limit=200, nested=True),
            label="get_orders/open",
        )
        normalized: list[Order] = []
        seen_ids: set[str] = set()
        if not hasattr(self, "_order_parent_ids"):
            self._order_parent_ids = {}

        for raw_order in raw:
            parent = self._to_order(raw_order)
            parent_id = str(getattr(raw_order, "id", ""))
            if parent_id and parent_id not in seen_ids:
                seen_ids.add(parent_id)
                normalized.append(parent)

            parent_strategy, parent_reason = self._meta.get(
                parent_id,
                (parent.strategy, parent.reason),
            )

            # Collect child orders from both legs and related_orders
            children = []
            child_seen = set()
            stack = []
            for key in ("legs", "related_orders"):
                for item in (getattr(raw_order, key, None) or []):
                    cid = str(getattr(item, "id", ""))
                    if cid and cid not in child_seen:
                        child_seen.add(cid)
                        # Preserve the parent/child identifier even when the
                        # Alpaca SDK rejects adding a synthetic parent_id
                        # attribute to its Pydantic Order model below.
                        if parent_id:
                            self._order_parent_ids[cid] = parent_id
                        stack.append(item)

            while stack:
                child_raw = stack.pop(0)
                children.append(child_raw)
                for key in ("legs", "related_orders"):
                    for item in (getattr(child_raw, key, None) or []):
                        cid = str(getattr(item, "id", ""))
                        if cid and cid not in child_seen:
                            child_seen.add(cid)
                            if parent_id:
                                self._order_parent_ids[cid] = parent_id
                            stack.append(item)

            for child in children:
                child_id = str(getattr(child, "id", ""))
                if child_id and child_id not in seen_ids:
                    seen_ids.add(child_id)
                    if child_id not in self._meta:
                        self._meta[child_id] = (parent_strategy, parent_reason)
                    if parent_id and not getattr(child, "parent_id", None) and not getattr(child, "parent_order_id", None):
                        try:
                            child.parent_id = parent_id
                        except Exception:
                            pass
                    normalized.append(self._to_order(child))
        return normalized


    def _to_order(self, o) -> Order:  # noqa: ANN001
        type_map = {
            "market": OrderType.MARKET,
            "limit": OrderType.LIMIT,
            "stop": OrderType.STOP,
            "stop_limit": OrderType.STOP_LIMIT,
        }
        status_map = {
            "new": OrderStatus.SUBMITTED,
            "accepted": OrderStatus.SUBMITTED,
            "pending_new": OrderStatus.SUBMITTED,
            "partially_filled": OrderStatus.PARTIALLY_FILLED,
            "filled": OrderStatus.FILLED,
            "canceled": OrderStatus.CANCELED,
            "expired": OrderStatus.CANCELED,
            "rejected": OrderStatus.REJECTED,
        }
        raw_type = (
            str(o.type).split(".")[-1].lower()
            if getattr(o, "type", None)
            else "market"
        )
        order_type = type_map.get(raw_type)
        if order_type is None:
            raise ValueError(f"Unsupported broker order type: {raw_type}")

        raw_status = (
            str(o.status).split(".")[-1].lower()
            if getattr(o, "status", None)
            else "submitted"
        )
        order_status = status_map.get(raw_status, OrderStatus.SUBMITTED)

        symbol_key = str(getattr(o, "symbol", "") or "").upper()
        symbol_strategies = getattr(self, "_symbol_strategies", {})
        strategy, reason = self._meta.get(
            str(o.id),
            (symbol_strategies.get(symbol_key, "alpaca"), ""),
        )
        stop_price = (
            float(o.stop_price)
            if getattr(o, "stop_price", None) is not None
            else None
        )
        limit_price = (
            float(o.limit_price)
            if getattr(o, "limit_price", None) is not None
            else None
        )
        order_class = (
            str(o.order_class).split(".")[-1].lower()
            if getattr(o, "order_class", None)
            else None
        )

        legs = getattr(o, "legs", None) or getattr(o, "related_orders", None)
        if legs:
            for child in legs:
                child_type = (
                    str(getattr(child, "type", "")).split(".")[-1].lower()
                    if getattr(child, "type", None)
                    else ""
                )
                child_st_px = getattr(child, "stop_price", None)
                child_lm_px = getattr(child, "limit_price", None)
                if (child_type in ("stop", "stop_limit") or child_st_px is not None) and stop_price is None:
                    if child_st_px is not None:
                        stop_price = float(child_st_px)
                if (child_type == "limit" or child_lm_px is not None) and limit_price is None:
                    if child_lm_px is not None:
                        limit_price = float(child_lm_px)

        raw_parent_id = getattr(o, "parent_order_id", None) or getattr(o, "parent_id", None)
        parent_id = str(raw_parent_id) if raw_parent_id else None
        if parent_id is None:
            # Nested Alpaca SDK objects can omit parent_order_id/parent_id.  The
            # open_orders() traversal records the authoritative relationship by
            # child ID, so a flattened child remains distinguishable from its
            # bracket/OTO parent during recovery and reconciliation.
            parent_id = getattr(self, "_order_parent_ids", {}).get(str(getattr(o, "id", "")))

        raw_leg = getattr(o, "leg", None)
        broker_leg = (
            str(raw_leg).split(".")[-1].lower()
            if raw_leg is not None and str(raw_leg)
            else None
        )
        is_child_order = parent_id is not None
        is_group_parent = order_class in {"bracket", "oco", "oto"} and not is_child_order

        # --- PREVIOUS IMPLEMENTATION (Preserved for historical intent & audit) ---
        # raw_leg = getattr(o, "leg", None)
        # if raw_leg is not None:
        #     leg = str(raw_leg).split(".")[-1].lower()
        # elif order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
        #     leg = "stop"
        # elif order_class in {"bracket", "oco", "oto"} or getattr(o, "parent_order_id", None) or getattr(o, "parent_id", None):
        #     if stop_price is not None and limit_price is None:
        #         leg = "stop"
        #     elif limit_price is not None and stop_price is None:
        #         leg = "take_profit"
        #     else:
        #         leg = "entry"
        # else:
        #     leg = "entry"
        # --------------------------------------------------------------------------
        # --- NEW IMPLEMENTATION ---
        # A parent order can carry stop/target prices copied from nested broker
        # legs for metadata, so price presence must not identify the parent as a
        # protective leg.  Trust explicit broker leg metadata first; otherwise
        # use a parent identifier (including the nested-ID map above) to classify
        # real children, and treat an advanced order class without a parent as
        # the entry container.  Standalone STOP/STOP_LIMIT orders retain their
        # historical stop classification.
        if broker_leg is not None:
            leg = broker_leg
        elif is_group_parent:
            leg = "entry"
        elif is_child_order:
            if order_type in (OrderType.STOP, OrderType.STOP_LIMIT) or (
                stop_price is not None and limit_price is None
            ):
                leg = "stop"
            elif limit_price is not None and stop_price is None:
                leg = "take_profit"
            else:
                leg = "entry"
        elif order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            leg = "stop"
        else:
            leg = "entry"

        return Order(
            id=str(o.id),
            client_order_id=str(o.client_order_id or ""),
            symbol=o.symbol,
            side=(
                OrderSide.BUY
                if str(o.side).lower().endswith("buy")
                else OrderSide.SELL
            ),
            qty=float(o.qty or 0),
            filled_qty=float(o.filled_qty or 0),
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            # Do not copy standalone stop trigger here.
            stop_loss=None,
            status=order_status,
            order_class=order_class,
            # --- PREVIOUS IMPLEMENTATION (Preserved for historical intent & audit) ---
            # parent_id=(
            #     str(o.parent_order_id)
            #     if getattr(o, "parent_order_id", None)
            #     else (str(o.parent_id) if getattr(o, "parent_id", None) else None)
            # ),
            # --------------------------------------------------------------------------
            # Use the resolved broker relationship so nested children retain
            # their parent identity even when the SDK only supplied ``legs``.
            parent_id=parent_id,
            leg=leg,
            strategy=strategy,
            reason=reason,
            submitted_at=getattr(o, "submitted_at", None) or datetime.now(UTC),
            filled_at=getattr(o, "filled_at", None),
        )


    async def submit(self, req: OrderRequest) -> Order:
        from alpaca.trading.enums import OrderSide as ASide
        from alpaca.trading.enums import TimeInForce
        from alpaca.trading.requests import (
            LimitOrderRequest,
            MarketOrderRequest,
            StopLimitOrderRequest,
            StopLossRequest,
            StopOrderRequest,
            TakeProfitRequest,
        )

        side = ASide.BUY if req.side == OrderSide.BUY else ASide.SELL
        qty = max(1, int(req.qty))  # brackets require whole shares
        kwargs: dict = {
            "symbol": req.symbol.upper(),
            "qty": qty,
            "side": side,
            "time_in_force": TimeInForce.DAY,
            "client_order_id": req.client_order_id,
        }
        if req.stop_loss is not None or req.take_profit is not None:
            from alpaca.trading.enums import OrderClass

        # --- PREVIOUS IMPLEMENTATION (Preserved for historical intent & audit) ---
        # if req.stop_loss is not None and req.take_profit is not None:
        #     kwargs["order_class"] = OrderClass.BRACKET
        #     kwargs["stop_loss"] = StopLossRequest(stop_price=round(req.stop_loss, 2))
        #     kwargs["take_profit"] = TakeProfitRequest(limit_price=round(req.take_profit, 2))
        # elif req.stop_loss is not None:
        #     kwargs["order_class"] = OrderClass.OTO
        #     kwargs["stop_loss"] = StopLossRequest(stop_price=round(req.stop_loss, 2))
        # elif req.take_profit is not None:
        #     kwargs["order_class"] = OrderClass.OTO
        #     kwargs["take_profit"] = TakeProfitRequest(limit_price=round(req.take_profit, 2))
        #
        # if req.order_type == OrderType.MARKET:
        #     request = MarketOrderRequest(**kwargs)
        # elif req.order_type == OrderType.LIMIT:
        #     if req.limit_price is None:
        #         raise ValueError("LIMIT orders require limit_price")
        #     request = LimitOrderRequest(limit_price=round(req.limit_price, 2), **kwargs)
        # elif req.order_type == OrderType.STOP:
        #     if req.stop_price is None:
        #         raise ValueError("STOP orders require stop_price")
        #     request = StopOrderRequest(stop_price=round(req.stop_price, 2), **kwargs)
        # elif req.order_type == OrderType.STOP_LIMIT:
        #     if req.stop_price is None or req.limit_price is None:
        #         raise ValueError("STOP_LIMIT orders require stop_price and limit_price")
        #     request = StopLimitOrderRequest(stop_price=round(req.stop_price, 2), limit_price=round(req.limit_price, 2), **kwargs)
        # --------------------------------------------------------------------------
        # --- NEW IMPLEMENTATION ---
        # Sub-penny precision support: Apply normalize_stock_price (2dp >= $1.00, 4dp < $1.00)
        # per SEC Rule 612 and Alpaca API requirements for penny stock orders.
        from app.brokers.alpaca_hardening import normalize_stock_price

        if req.stop_loss is not None and req.take_profit is not None:
            kwargs["order_class"] = OrderClass.BRACKET
            kwargs["stop_loss"] = StopLossRequest(stop_price=normalize_stock_price(req.stop_loss))
            kwargs["take_profit"] = TakeProfitRequest(limit_price=normalize_stock_price(req.take_profit))
        elif req.stop_loss is not None:
            kwargs["order_class"] = OrderClass.OTO
            kwargs["stop_loss"] = StopLossRequest(stop_price=normalize_stock_price(req.stop_loss))
        elif req.take_profit is not None:
            kwargs["order_class"] = OrderClass.OTO
            kwargs["take_profit"] = TakeProfitRequest(limit_price=normalize_stock_price(req.take_profit))

        if req.order_type == OrderType.MARKET:
            request = MarketOrderRequest(**kwargs)
        elif req.order_type == OrderType.LIMIT:
            if req.limit_price is None:
                raise ValueError("LIMIT orders require limit_price")
            request = LimitOrderRequest(limit_price=normalize_stock_price(req.limit_price), **kwargs)
        elif req.order_type == OrderType.STOP:
            if req.stop_price is None:
                raise ValueError("STOP orders require stop_price")
            request = StopOrderRequest(stop_price=normalize_stock_price(req.stop_price), **kwargs)
        elif req.order_type == OrderType.STOP_LIMIT:
            if req.stop_price is None or req.limit_price is None:
                raise ValueError("STOP_LIMIT orders require stop_price and limit_price")
            request = StopLimitOrderRequest(
                stop_price=normalize_stock_price(req.stop_price),
                limit_price=normalize_stock_price(req.limit_price),
                **kwargs,
            )
        else:
            raise ValueError(f"Unsupported Alpaca order type: {req.order_type}")

        try:
            raw = await _call(self._client.submit_order, request, label=f"submit_order/{req.symbol}")
        except BrokerTimeoutError:
            # Propagate: caller must treat as ambiguous order state, not rejected.
            raise
        except Exception as exc:  # noqa: BLE001
            # Duplicate-id rescue: a timeout-retry that hits Alpaca's
            # "client order id must be unique" 422 means the FIRST submission is
            # (probably) alive at the broker. Treating this as a definitive
            # REJECT orphans a live order. Query broker truth instead.
            err_str_l = str(exc).lower()
            if (
                getattr(exc, "status_code", None) == 422
                and req.client_order_id
                and ("client_order_id" in err_str_l or "client order id" in err_str_l)
            ):
                applog.warning(
                    "broker.alpaca",
                    f"Duplicate client_order_id on submit for {req.symbol} ({req.client_order_id}) — rescuing live order from broker",
                )
                rescued = await self.get_order_by_client_id(req.client_order_id)
                if rescued is not None:
                    self._meta[str(rescued.id)] = (req.strategy, req.reason)
                    if req.strategy != "emergency_protection":
                        self._symbol_strategies[req.symbol.upper()] = req.strategy
                    rescued.stop_loss = req.stop_loss
                    rescued.take_profit = req.take_profit
                    return rescued
            if self._is_definitive_rejection(exc):
                err_msg = str(exc).lower()
                # --- PREVIOUS IMPLEMENTATION (Preserved for historical intent & audit) ---
                # short_permission_denied = (
                #     "not allowed to short" in err_msg
                #     or "shorting is not permitted" in err_msg
                #     or "asset is not shortable" in err_msg
                #     or "not shortable" in err_msg
                # )
                # if short_permission_denied:
                #     self._shorting_enabled = False
                #     self._shortable_cache[req.symbol.upper()] = False
                # --------------------------------------------------------------------------
                # --- NEW IMPLEMENTATION ---
                # Decouple account-level margin permission denial from single-asset borrow restrictions.
                # If a single ticker (e.g. ONDS) is not shortable, cache that specific asset as unshortable,
                # but DO NOT disable self._shorting_enabled globally for the entire trading account.
                account_short_denied = (
                    "not allowed to short" in err_msg
                    or "shorting is not permitted" in err_msg
                )
                asset_not_shortable = (
                    "asset is not shortable" in err_msg
                    or "not shortable" in err_msg
                )
                # =============================================================================
                # [MODIFIED 2026-10-14]: CR-22 - SHORT_BORROW_UNAVAILABLE Distinct Logging
                # ORIGINAL INTENT: Marked rejected with generic f"{req.reason} | rejected: {exc}".
                # REASON FOR CHANGE: Distinctly log and tag SHORT_BORROW_UNAVAILABLE so borrow rejections
                # are not lumped into generic execution failures or bare exits.
                # SAFE ROLLBACK: The original code is preserved below as comments.
                # -----------------------------------------------------------------------------
                # [PREVIOUS CODE - PRESERVED]:
                # if account_short_denied:
                #     self._shorting_enabled = False
                #     self._shortable_cache[req.symbol.upper()] = False
                # elif asset_not_shortable:
                #     self._shortable_cache[req.symbol.upper()] = False
                # applog.error("broker.alpaca", f"Order rejected {req.symbol} {req.side} {qty}: {exc}")
                # return Order(
                #     client_order_id=req.client_order_id,
                #     symbol=req.symbol.upper(),
                #     side=req.side,
                #     qty=qty,
                #     order_type=req.order_type,
                #     limit_price=req.limit_price,
                #     status=OrderStatus.REJECTED,
                #     strategy=req.strategy,
                #     reason=f"{req.reason} | rejected: {exc}",
                # )
                # -----------------------------------------------------------------------------
                # [NEW CODE]:
                rejection_reason = f"{req.reason} | rejected: {exc}"
                if account_short_denied:
                    self._shorting_enabled = False
                    self._shortable_cache[req.symbol.upper()] = False
                    rejection_reason = f"SHORT_BORROW_UNAVAILABLE (account shorting denied) | {exc}"
                    applog.warning("broker.alpaca", f"SHORT_BORROW_UNAVAILABLE: account shorting disabled on {req.symbol}: {exc}")
                elif asset_not_shortable:
                    self._shortable_cache[req.symbol.upper()] = False
                    rejection_reason = f"SHORT_BORROW_UNAVAILABLE ({req.symbol} not shortable/borrow unavailable) | {exc}"
                    applog.warning("broker.alpaca", f"SHORT_BORROW_UNAVAILABLE: {req.symbol} borrow unavailable: {exc}")
                else:
                    applog.error("broker.alpaca", f"Order rejected {req.symbol} {req.side} {qty}: {exc}")

                return Order(
                    client_order_id=req.client_order_id,
                    symbol=req.symbol.upper(),
                    side=req.side,
                    qty=qty,
                    order_type=req.order_type,
                    limit_price=req.limit_price,
                    status=OrderStatus.REJECTED,
                    strategy=req.strategy,
                    reason=rejection_reason,
                )
                # =============================================================================
            else:
                msg = f"Ambiguous submission result for {req.symbol} ({req.client_order_id}): {exc}"
                applog.error("broker.alpaca", msg)
                raise BrokerTimeoutError(msg) from exc
        self._meta[str(raw.id)] = (req.strategy, req.reason)
        if req.strategy != "emergency_protection":
            self._symbol_strategies[req.symbol.upper()] = req.strategy
        order = self._to_order(raw)
        order.stop_loss = req.stop_loss
        order.take_profit = req.take_profit
        applog.trade("broker.alpaca", f"Submitted {req.side.upper()} {qty} {req.symbol} ({req.strategy})")
        return order

    def _is_definitive_rejection(self, exc: Exception) -> bool:
        import asyncio
        try:
            import requests
            if isinstance(exc, (requests.exceptions.RequestException, ConnectionError, TimeoutError, asyncio.TimeoutError)):
                return False
        except ImportError:
            if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError)):
                return False

        try:
            from alpaca.common.exceptions import APIError
            if isinstance(exc, APIError):
                status_code = getattr(exc, "status_code", None)
                if status_code is not None:
                    # 4xx are client/business rejections, 5xx are server/transport issues
                    return 400 <= status_code < 500
                return True
        except ImportError:
            pass

        # If it's a timeout/connection description, treat as transport failure
        err_str = str(exc).lower()
        if "timeout" in err_str or "connect" in err_str or "connection" in err_str:
            return False

        return True

    async def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        try:
            raw = await _call(
                self._client.get_order_by_client_id,
                client_order_id,
                label=f"get_order_by_client_id/{client_order_id}"
            )
            return self._to_order(raw)
        except BrokerTimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001
            from alpaca.common.exceptions import APIError
            if isinstance(exc, APIError) and getattr(exc, "status_code", None) == 404:
                return None
            err_str = str(exc).lower()
            if "not found" in err_str or "404" in err_str:
                return None
            raise BrokerTimeoutError(f"Ambiguous query result for client ID {client_order_id}: {exc}") from exc

    async def poll_fills(self) -> None:
        """Surface every cumulative execution watermark not durably processed yet."""
        from alpaca.trading.requests import GetOrdersRequest

        from app.db.database import Database

        db = Database.active()
        # fix(#47): start recovery from the forward reconciliation cursor in DB if available
        after = datetime.now(UTC) - timedelta(days=30)
        account_id = getattr(self, "account_id", None)
        if db and account_id:
            cursor_ts = db.get_fill_recovery_cursor_timestamp(self._broker_mode, account_id)
            if cursor_ts is not None:
                after = cursor_ts - timedelta(minutes=15)


        try:
            # fix(#50): full paginated sweep — never silently drop fills beyond 500
            raw = await _paginate_orders(self._client, GetOrdersRequest(status="all", after=after, limit=500), label="get_orders/poll_fills")

        except BrokerTimeoutError:
            applog.warning("broker.alpaca", "poll_fills skipped: API call timed out")
            return
        except Exception as exc:  # noqa: BLE001
            applog.warning("broker.alpaca", f"fill poll failed: {exc}")
            return

        db = Database.active()
        candidates = []
        for o in raw:
            cumulative = float(o.filled_qty or 0)
            if cumulative <= 0 or not o.filled_avg_price:
                continue
            oid = str(o.id)
            processed = max(
                self._processed_fill_qty.get(oid, 0.0),
                self._emitted_fill_qty.get(oid, 0.0),
            )
            if db:
                processed = max(processed, db.processed_fill_qty(mode=self._broker_mode, account_id=self.account_id, fill_key=oid))
            if cumulative <= processed + 1e-9:
                continue
            candidates.append(o)

        candidates.sort(key=lambda o: (o.filled_at or o.updated_at or o.submitted_at or datetime.now(UTC), str(o.id)))
        queued = {(f.broker_fill_key, f.cumulative_qty) for f in self._fills}
        local_watermarks = {}
        for o in candidates:
            oid = str(o.id)
            cumulative = float(o.filled_qty or 0)
            if (oid, cumulative) in queued:
                continue
            if oid in local_watermarks:
                processed = local_watermarks[oid]
            else:
                processed = max(
                    self._processed_fill_qty.get(oid, 0.0),
                    self._emitted_fill_qty.get(oid, 0.0),
                )
                if db:
                    processed = max(processed, db.processed_fill_qty(mode=self._broker_mode, account_id=self.account_id, fill_key=oid))
            delta = cumulative - processed
            if delta <= 1e-9:
                continue
            strategy, reason = self._meta.get(oid, ("alpaca", ""))

            # fix(#44): incremental execution pricing from change in cumulative notional
            cum_avg_price = float(o.filled_avg_price or 0.0)
            cum_notional = cumulative * cum_avg_price
            ack_qty = self._processed_fill_qty.get(oid, 0.0)
            emitted_qty = self._emitted_fill_qty.get(oid, 0.0)
            if emitted_qty >= ack_qty and emitted_qty >= processed - 1e-9:
                prev_notional = self._emitted_fill_notional.get(oid, 0.0)
            elif ack_qty >= processed - 1e-9:
                prev_notional = self._processed_fill_notional.get(oid, 0.0)
            else:
                # A durable DB watermark survives restart; use its matching cumulative
                # notional so the next delta price remains exact as well as its quantity.
                durable_notional = 0.0
                if db:
                    durable_notional = db.processed_fill_notional(
                        mode=self._broker_mode, account_id=self.account_id, fill_key=oid
                    )
                prev_notional = durable_notional if durable_notional > 0 else processed * cum_avg_price

            if processed > 0 and delta > 0 and cum_notional > prev_notional:
                delta_price = round((cum_notional - prev_notional) / delta, 4)
            else:
                delta_price = cum_avg_price

            self._fills.append(
                Fill(
                    order_id=oid,
                    symbol=o.symbol,
                    side=OrderSide.BUY if "buy" in str(o.side).lower() else OrderSide.SELL,
                    qty=delta,
                    price=delta_price,
                    strategy=strategy,
                    reason=reason,
                    ts=o.filled_at or o.updated_at or datetime.now(UTC),
                    broker_fill_key=oid,
                    cumulative_qty=cumulative,
                    broker_mode=self._broker_mode,
                    broker_account_id=self.account_id,
                )
            )
            # Reserve the cumulative snapshot as in-flight before returning it to
            # the engine. ACK promotes this reservation; NACK releases it.
            local_watermarks[oid] = cumulative
            self._emitted_fill_qty[oid] = cumulative
            self._emitted_fill_notional[oid] = cum_notional


    def ack_fill_processed(self, fill: Fill) -> None:
        """Advance volatile fill bookkeeping only after durable processing succeeds."""
        oid = str(fill.broker_fill_key or fill.order_id or "")
        if not oid:
            return
        cumulative = float(fill.cumulative_qty or 0.0)
        if cumulative > 0:
            previous_qty = self._processed_fill_qty.get(oid, 0.0)
            if cumulative >= previous_qty:
                self._processed_fill_qty[oid] = cumulative
                self._processed_fill_notional[oid] = max(
                    self._processed_fill_notional.get(oid, 0.0),
                    self._emitted_fill_notional.get(oid, 0.0),
                    self._processed_fill_notional.get(oid, 0.0) + float(fill.qty) * float(fill.price),
                )
        if self._emitted_fill_qty.get(oid, 0.0) <= self._processed_fill_qty.get(oid, 0.0) + 1e-9:
            self._emitted_fill_qty.pop(oid, None)
            self._emitted_fill_notional.pop(oid, None)

    def nack_fill_processed(self, fill: Fill) -> None:
        """Release an uncommitted reservation so a later poll can retry it."""
        oid = str(fill.broker_fill_key or fill.order_id or "")
        if not oid:
            return
        cumulative = float(fill.cumulative_qty or 0.0)
        if self._emitted_fill_qty.get(oid, 0.0) <= cumulative + 1e-9:
            self._emitted_fill_qty.pop(oid, None)
            self._emitted_fill_notional.pop(oid, None)


    def drain_fills(self) -> list[Fill]:
        fills, self._fills = self._fills, []
        return fills

    async def cancel(self, order_id: str) -> bool:
        try:
            await _call(self._client.cancel_order_by_id, order_id, label=f"cancel/{order_id}")
            return True
        except BrokerTimeoutError:
            applog.warning("broker.alpaca", f"cancel {order_id} timed out; state is ambiguous")
            return False
        except Exception as exc:  # noqa: BLE001
            applog.warning("broker.alpaca", f"cancel {order_id} failed: {exc}")
            return False

    async def cancel_all(self) -> None:
        """Cancel all open orders. Used by emergency flatten and EOD liquidation."""
        try:
            await _call(
                self._client.cancel_orders,
                label="cancel_all_orders",
            )
            applog.info("broker.alpaca", "cancel_all: all open orders cancelled")
        except BrokerTimeoutError:
            applog.error("broker.alpaca", "cancel_all timed out; state is ambiguous")
        except Exception as exc:  # noqa: BLE001
            applog.warning("broker.alpaca", f"cancel_all failed: {exc}")

    async def close_position(self, symbol: str, reason: str = "manual", qty: float | None = None) -> Order | None:
        try:
            # First, fetch and cancel any outstanding open orders for this symbol to release held quantities
            cancelled_ids: set[str] = set()
            try:
                from alpaca.trading.requests import GetOrdersRequest
                open_orders = await _call(
                    self._client.get_orders,
                    GetOrdersRequest(status="open", symbols=[symbol.upper()]),
                    label=f"get_orders/close_position/{symbol}"
                )
                for o in open_orders:
                    await _call(self._client.cancel_order_by_id, str(o.id), label=f"cancel_order/{o.id}")
                    cancelled_ids.add(str(o.id))
                    applog.info("broker.alpaca", f"Cancelled outstanding open order {o.id} for {symbol} to release held qty")
            except Exception as e:
                applog.warning("broker.alpaca", f"Failed to cancel open orders for {symbol} before close: {e}")

            if qty is not None and qty > 0:
                # Verify the cancels actually landed before submitting the market
                # close. Cancellation is asynchronous server-side; a triggered
                # bracket stop leg racing an unverified close can flip the
                # position to the opposite side with no protection attached.
                import asyncio

                verified_gone = True
                if cancelled_ids:
                    for attempt in range(10):
                        await asyncio.sleep(0.25)
                        try:
                            still_open = await _call(
                                self._client.get_orders,
                                GetOrdersRequest(status="open", symbols=[symbol.upper()]),
                                label=f"verify_cancel/close_position/{symbol}",
                            )
                            open_ids = {str(o.id) for o in still_open}
                            if not (cancelled_ids & open_ids):
                                verified_gone = True
                                break
                            verified_gone = False
                        except Exception as exc:
                            applog.warning("broker.alpaca", f"Cancel verification poll failed for {symbol}: {exc}")
                            verified_gone = False
                            break
                    if not verified_gone:
                        applog.error(
                            "broker.alpaca",
                            f"{symbol}: cancel confirmation incomplete before close — proceeding but stop-leg race window remains open",
                        )

                from alpaca.trading.enums import OrderSide, TimeInForce
                from alpaca.trading.requests import MarketOrderRequest

                pos = await _call(self._client.get_open_position, symbol.upper(), label=f"get_open_position/{symbol}")
                is_long = float(pos.qty) > 0
                opposite_side = OrderSide.SELL if is_long else OrderSide.BUY

                qty_str = str(int(qty)) if float(qty).is_integer() else str(round(qty, 4))
                # Idempotent client_order_id: a timeout-retry of this exact close
                # reuses the same id, so Alpaca deduplicates instead of double-
                # closing (which would flip the position to the opposite side).
                close_cid = f"ad-close-{symbol.upper()}-{uuid.uuid4().hex[:12]}"
                raw = await _call(
                    self._client.submit_order,
                    MarketOrderRequest(
                        symbol=symbol.upper(),
                        qty=qty_str,
                        side=opposite_side,
                        time_in_force=TimeInForce.DAY,
                        client_order_id=close_cid,
                    ),
                    label=f"close_position_partial_market/{symbol}"
                )
            else:
                raw = await _call(self._client.close_position, symbol.upper(), label=f"close_position/{symbol}")
            self._meta[str(raw.id)] = ("engine", reason)
            applog.trade("broker.alpaca", f"Closing {symbol} ({reason}{f', qty={qty}' if qty else ''})")
            return self._to_order(raw)
        except BrokerTimeoutError:
            applog.warning("broker.alpaca", f"close_position {symbol} timed out; state is ambiguous")
            return None
        except Exception as exc:  # noqa: BLE001
            applog.warning("broker.alpaca", f"close {symbol} failed: {exc}")
            return None
    async def is_shortable(self, symbol: str) -> bool:
        if self._shorting_enabled is False:
            return False

        sym = symbol.upper()
        if sym in self._shortable_cache:
            return self._shortable_cache[sym]

        try:
            acct = await _call(self._client.get_account, label="get_account")
            self._shorting_enabled = bool(getattr(acct, "shorting_enabled", True))
            if not self._shorting_enabled:
                self._shortable_cache[sym] = False
                return False
        except Exception as exc:
            if self._shorting_enabled is None:
                applog.warning("broker.alpaca", f"Failed to verify account shorting permission: {exc}")
                return False

        try:
            asset = await _call(self._client.get_asset, sym, label=f"get_asset/{sym}")
            # Asset is shortable if Alpaca says it is shortable and easy to borrow
            shortable = bool(getattr(asset, "shortable", False) and getattr(asset, "easy_to_borrow", False))
            self._shortable_cache[sym] = shortable
            return shortable
        except Exception as exc:
            applog.warning("broker.alpaca", f"Failed to check shortability for {sym}: {exc}")
            return False

    async def flatten_all(self, reason: str = "flatten") -> list[Order]:
        try:
            res = await _call(self._client.close_all_positions, cancel_orders=True, label="close_all_positions")
            applog.trade("broker.alpaca", f"Flattened all positions ({reason})")
            orders = []
            if isinstance(res, list):
                for raw in res:
                    try:
                        orders.append(self._to_order(raw))
                    except Exception:
                        pass
            return orders
        except BrokerTimeoutError as exc:
            applog.error("broker.alpaca", f"flatten_all timed out: {exc}")
            raise
        except Exception as exc:
            applog.error("broker.alpaca", f"flatten failed: {exc}")
            raise
