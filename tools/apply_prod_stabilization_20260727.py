from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one match, found {count}: {old[:100]!r}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


# 1) Weekend/holiday-safe universe-stress history. Thresholds and aggregation unchanged.
replace_once(
    "backend/app/risk/market_regime.py",
    "            start = T - timedelta(days=2)\n            extreme = 0",
    "            # Match breadth's weekend/single-holiday-safe history horizon.\n"
    "            # This changes data availability only; stress thresholds/aggregation are unchanged.\n"
    "            start = T - timedelta(days=5)\n            extreme = 0",
)

# 2) ^VIX/VIX are reference/risk feeds, never tradable strategy symbols.
replace_once(
    "backend/app/core/engine.py",
    "    async def _evaluate_symbol(self, symbol: str, position, equity: float, now: datetime) -> list[tuple[Signal, pd.DataFrame]]:  # noqa: ANN001\n"
    "        assert self.data and self.broker and self.risk and self.pipeline\n"
    "        df = enrich(self.data.frame(symbol, \"1Min\", limit=600))",
    "    async def _evaluate_symbol(self, symbol: str, position, equity: float, now: datetime) -> list[tuple[Signal, pd.DataFrame]]:  # noqa: ANN001\n"
    "        assert self.data and self.broker and self.risk and self.pipeline\n"
    "        symbol = symbol.upper()\n"
    "        # Reference volatility feeds are risk inputs only and must never enter the tradable pipeline.\n"
    "        if symbol in {s.upper() for s in self._vix_symbols()}:\n"
    "            applog.debug(\"engine\", f\"{symbol}: reference/risk symbol excluded from strategy evaluation\")\n"
    "            return []\n"
    "        df = enrich(self.data.frame(symbol, \"1Min\", limit=600))",
)

replace_once(
    "backend/app/core/engine.py",
    "            if sig.action != SignalAction.EXIT and _resurrection_enabled:\n",
    "            if (\n"
    "                sig.action != SignalAction.EXIT\n"
    "                and _resurrection_enabled\n"
    "                and sig.symbol.upper() not in {s.upper() for s in self._vix_symbols()}\n"
    "            ):\n",
)

replace_once(
    "backend/app/core/engine.py",
    "            symbol = item[\"symbol\"]\n            strategy_name = item[\"strategy\"]\n\n            # Find the originating strategy",
    "            symbol = item[\"symbol\"].upper()\n"
    "            strategy_name = item[\"strategy\"]\n"
    "            if symbol in {s.upper() for s in self._vix_symbols()}:\n"
    "                applog.warning(\"engine.resurrection\", f\"Dropping non-tradable reference symbol {symbol} from resurrection queue\")\n"
    "                continue\n\n"
    "            # Find the originating strategy",
)

replace_once(
    "backend/app/core/engine.py",
    "                        for sig, kelly_val in decision_outcome.approved_entries:\n"
    "                            # Attach risk_multiplier from SafetyDecision",
    "                        for sig, kelly_val in decision_outcome.approved_entries:\n"
    "                            # Defense in depth: reference/risk symbols can never reach broker submission.\n"
    "                            if sig.symbol.upper() in {s.upper() for s in self._vix_symbols()}:\n"
    "                                applog.error(\"engine\", f\"Blocked non-tradable reference symbol at submission boundary: {sig.symbol}\")\n"
    "                                continue\n"
    "                            # Attach risk_multiplier from SafetyDecision",
)

# 3) Protection recovery: no tight retry storm after a broker rejection.
replace_once(
    "backend/app/core/engine.py",
    "                    ProtectionState.AWAITING_STOP_CONFIRMATION,\n                    ProtectionState.AMBIGUOUS,\n",
    "                    ProtectionState.AWAITING_STOP_CONFIRMATION,\n"
    "                    ProtectionState.AMBIGUOUS,\n"
    "                    # A rejected recovery remains fail-closed until broker state changes or restart;\n"
    "                    # do not hammer Alpaca every tick while quantity is held by an existing exit.\n"
    "                    ProtectionState.RECOVERY_REJECTED,\n",
)

# Preserve strategy lineage on recovery orders; emergency protection is a role/reason, not a strategy.
replace_once(
    "backend/app/core/engine.py",
    "                    req = OrderRequest(\n"
    "                        symbol=p.symbol,\n"
    "                        side=side,\n"
    "                        qty=required_qty,\n"
    "                        order_type=OrderType.STOP,\n"
    "                        stop_price=stop_px,\n"
    "                        client_order_id=client_order_id,\n"
    "                        strategy=\"emergency_protection\",\n"
    "                        reason=f\"fix(#41) protective stop recovery source={stop_source}\",\n"
    "                    )",
    "                    recovery_strategy = getattr(p, \"strategy\", None) or \"reconciled\"\n"
    "                    if self.portfolio and p.symbol in self.portfolio._lots:\n"
    "                        recovery_strategy = getattr(self.portfolio._lots[p.symbol], \"strategy\", None) or recovery_strategy\n"
    "                    if recovery_strategy in {\"alpaca\", \"emergency_protection\", \"\"}:\n"
    "                        recovery_strategy = \"reconciled\"\n\n"
    "                    req = OrderRequest(\n"
    "                        symbol=p.symbol,\n"
    "                        side=side,\n"
    "                        qty=required_qty,\n"
    "                        order_type=OrderType.STOP,\n"
    "                        stop_price=stop_px,\n"
    "                        client_order_id=client_order_id,\n"
    "                        strategy=recovery_strategy,\n"
    "                        reason=f\"emergency_protection: fix(#41) protective stop recovery source={stop_source}\",\n"
    "                    )",
)

# Surface broker-confirmed stop/target on the same Position objects broadcast to the UI.
replace_once(
    "backend/app/core/engine.py",
    "                if self.portfolio and sym in self.portfolio._lots:\n"
    "                    self.portfolio._lots[sym].stop_loss = verified_stop_price\n"
    "                stop_str = f\"{verified_stop_price:.4f}\" if verified_stop_price else \"N/A\"",
    "                p.stop_loss = verified_stop_price\n"
    "                if self.portfolio and sym in self.portfolio._lots:\n"
    "                    self.portfolio._lots[sym].stop_loss = verified_stop_price\n"
    "                # Populate broker-confirmed take-profit when an active exit limit exists.\n"
    "                for exit_order in symbol_exit_orders:\n"
    "                    limit_px = getattr(exit_order, \"limit_price\", None)\n"
    "                    if (\n"
    "                        exit_order.order_type == OrderType.LIMIT\n"
    "                        and limit_px is not None\n"
    "                        and ((p.qty > 0 and limit_px > p.avg_entry_price) or (p.qty < 0 and limit_px < p.avg_entry_price))\n"
    "                    ):\n"
    "                        p.take_profit = limit_px\n"
    "                        break\n"
    "                stop_str = f\"{verified_stop_price:.4f}\" if verified_stop_price else \"N/A\"",
)

# 4) Alpaca open-order normalization: expose bracket/OCO child legs as real exit orders.
replace_once(
    "backend/app/brokers/alpaca.py",
    "        return [self._to_order(o) for o in raw]\n\n    def _to_order",
    "        normalized: list[Order] = []\n"
    "        for raw_order in raw:\n"
    "            parent = self._to_order(raw_order)\n"
    "            normalized.append(parent)\n"
    "            parent_strategy, parent_reason = self._meta.get(\n"
    "                str(getattr(raw_order, \"id\", \"\")),\n"
    "                (parent.strategy, parent.reason),\n"
    "            )\n"
    "            for child in (getattr(raw_order, \"legs\", None) or []):\n"
    "                child_id = str(getattr(child, \"id\", \"\"))\n"
    "                if child_id and child_id not in self._meta:\n"
    "                    self._meta[child_id] = (parent_strategy, parent_reason)\n"
    "                normalized.append(self._to_order(child))\n"
    "        return normalized\n\n    def _to_order",
)

# Child/restarted orders inherit known symbol strategy instead of anonymous 'alpaca'.
replace_once(
    "backend/app/brokers/alpaca.py",
    "        strategy, reason = self._meta.get(str(o.id), (\"alpaca\", \"\"))\n",
    "        symbol_key = str(getattr(o, \"symbol\", \"\") or \"\").upper()\n"
    "        strategy, reason = self._meta.get(\n"
    "            str(o.id),\n"
    "            (self._symbol_strategies.get(symbol_key, \"alpaca\"), \"\"),\n"
    "        )\n",
)

# Never let an emergency-protection role replace the position's originating strategy.
replace_once(
    "backend/app/brokers/alpaca.py",
    "        self._meta[str(raw.id)] = (req.strategy, req.reason)\n"
    "        self._symbol_strategies[req.symbol.upper()] = req.strategy\n",
    "        self._meta[str(raw.id)] = (req.strategy, req.reason)\n"
    "        if req.strategy != \"emergency_protection\":\n"
    "            self._symbol_strategies[req.symbol.upper()] = req.strategy\n",
)

# Fill lifecycle: in-memory watermark is acknowledged only AFTER durable portfolio/DB processing.
replace_once(
    "backend/app/brokers/alpaca.py",
    "            self._processed_fill_qty[oid] = cumulative\n            local_watermarks[oid] = cumulative\n\n\n    def drain_fills",
    "            # local_watermarks prevents duplicates within this poll only. Durable/in-memory\n"
    "            # acknowledgement happens after PortfolioTracker atomically commits the fill.\n"
    "            local_watermarks[oid] = cumulative\n\n\n"
    "    def ack_fill_processed(self, fill: Fill) -> None:\n"
    "        \"\"\"Advance volatile fill bookkeeping only after durable processing succeeds.\"\"\"\n"
    "        oid = str(fill.broker_fill_key or fill.order_id or \"\")\n"
    "        if not oid:\n"
    "            return\n"
    "        cumulative = float(fill.cumulative_qty or 0.0)\n"
    "        if cumulative > 0:\n"
    "            self._processed_fill_qty[oid] = max(self._processed_fill_qty.get(oid, 0.0), cumulative)\n"
    "        self._processed_fill_notional[oid] = self._processed_fill_notional.get(oid, 0.0) + float(fill.qty) * float(fill.price)\n\n\n"
    "    def drain_fills",
)

replace_once(
    "backend/app/core/engine.py",
    "            trade = self.portfolio.on_fill(\n"
    "                fill, peak_price=peak, trough_price=trough, initial_stop=init_stop,\n"
    "                risk_manager=self.risk, now=now\n"
    "            )\n"
    "            if trade is not None:",
    "            trade = self.portfolio.on_fill(\n"
    "                fill, peak_price=peak, trough_price=trough, initial_stop=init_stop,\n"
    "                risk_manager=self.risk, now=now\n"
    "            )\n"
    "            ack_fill = getattr(self.broker, \"ack_fill_processed\", None)\n"
    "            if ack_fill is not None:\n"
    "                ack_fill(fill)\n"
    "            if trade is not None:",
)

# Focused regression tests for today's production defects.
test_path = Path("backend/tests/test_prod_stabilization_20260727.py")
test_path.write_text(r'''from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.brokers.alpaca import AlpacaBroker
from app.core.engine import TradingEngine
from app.models.domain import Fill, OrderSide


def test_universe_stress_uses_weekend_safe_lookback_source():
    src = Path(__file__).parents[1] / "app" / "risk" / "market_regime.py"
    text = src.read_text(encoding="utf-8")
    block = text[text.index("async def _family_universe_stress"):]
    assert "timedelta(days=5)" in block
    assert "timedelta(days=2)" not in block.split("async def ", 1)[0]


def test_vix_is_blocked_at_strategy_boundary_source():
    src = Path(__file__).parents[1] / "app" / "core" / "engine.py"
    text = src.read_text(encoding="utf-8")
    assert "reference/risk symbol excluded from strategy evaluation" in text
    assert "Blocked non-tradable reference symbol at submission boundary" in text
    assert "Dropping non-tradable reference symbol" in text


def test_emergency_protection_does_not_replace_symbol_strategy():
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._symbol_strategies = {"ORCL": "momentum"}
    broker._meta = {}
    # The source-level guard is intentionally tested without constructing alpaca-py clients.
    src = Path(__file__).parents[1] / "app" / "brokers" / "alpaca.py"
    text = src.read_text(encoding="utf-8")
    assert 'if req.strategy != "emergency_protection"' in text
    assert broker._symbol_strategies["ORCL"] == "momentum"


def test_fill_ack_is_post_durable_processing_source():
    broker_src = (Path(__file__).parents[1] / "app" / "brokers" / "alpaca.py").read_text(encoding="utf-8")
    engine_src = (Path(__file__).parents[1] / "app" / "core" / "engine.py").read_text(encoding="utf-8")
    poll_block = broker_src[broker_src.index("async def poll_fills"):broker_src.index("def drain_fills")]
    assert "self._processed_fill_qty[oid] = cumulative" not in poll_block
    assert "def ack_fill_processed" in broker_src
    on_fill_idx = engine_src.index("trade = self.portfolio.on_fill(")
    ack_idx = engine_src.index('ack_fill = getattr(self.broker, "ack_fill_processed"', on_fill_idx)
    assert ack_idx > on_fill_idx


def test_open_orders_expands_broker_child_legs_source():
    src = (Path(__file__).parents[1] / "app" / "brokers" / "alpaca.py").read_text(encoding="utf-8")
    block = src[src.index("async def open_orders"):src.index("def _to_order")]
    assert 'getattr(raw_order, "legs", None)' in block
    assert "normalized.append(self._to_order(child))" in block


def test_recovery_rejection_is_fail_closed_not_tight_retry_source():
    src = (Path(__file__).parents[1] / "app" / "core" / "engine.py").read_text(encoding="utf-8")
    assert "ProtectionState.RECOVERY_REJECTED" in src
    assert "do not hammer Alpaca every tick" in src
''', encoding="utf-8")

print("Applied production stabilization patches and regression tests.")
