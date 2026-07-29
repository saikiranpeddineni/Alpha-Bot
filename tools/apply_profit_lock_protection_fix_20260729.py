"""Surgical Rule-41 profit-lock correctness patch.

Run from repository root after pulling fix/heartbeat-runtime-root-causes-20260728.
Idempotent: exits cleanly when already applied.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
TEST = ROOT / "backend/tests/test_profit_lock_rule41.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        print(f"already applied: {label}")
        return text
    if old not in text:
        raise RuntimeError(f"anchor not found for {label}")
    return text.replace(old, new, 1)


engine = ENGINE.read_text(encoding="utf-8")

engine = replace_once(
    engine,
    '''        if self.portfolio and sym in self.portfolio._lots:\n            lot = self.portfolio._lots[sym]\n            if getattr(lot, "initial_stop_loss", None) is not None and lot.initial_stop_loss > 0:\n                return round(float(lot.initial_stop_loss), 4), "ORIGINAL_STRATEGY_STOP"\n            if getattr(lot, "stop_loss", None) is not None and lot.stop_loss > 0:\n                return round(float(lot.stop_loss), 4), "PORTFOLIO_CURRENT_STOP"\n''',
    '''        if self.portfolio and sym in self.portfolio._lots:\n            lot = self.portfolio._lots[sym]\n            # Recovery must preserve the strongest broker-confirmed protection.\n            # A profitable ratcheted stop is authoritative current protection;\n            # falling back to the original entry stop would give back locked P&L.\n            if getattr(lot, "stop_loss", None) is not None and lot.stop_loss > 0:\n                return round(float(lot.stop_loss), 4), "PORTFOLIO_CURRENT_STOP"\n            if getattr(lot, "initial_stop_loss", None) is not None and lot.initial_stop_loss > 0:\n                return round(float(lot.initial_stop_loss), 4), "ORIGINAL_STRATEGY_STOP"\n''',
    "prefer current stop during protection recovery",
)

engine = replace_once(
    engine,
    '''        if position.qty > 0:\n            if order.side != OrderSide.SELL:\n                return False\n\n            if stop_price >= position.avg_entry_price:\n                return False\n        else:\n            if order.side != OrderSide.BUY:\n                return False\n\n            if stop_price <= position.avg_entry_price:\n                return False\n''',
    '''        # Stop validity is relative to the live market, not entry.  Once a\n        # winner has crossed breakeven, a SELL stop above long entry (or BUY stop\n        # below short entry) is legitimate profit protection.  It must still be\n        # strictly behind the current market so Rule-41 never blesses an already\n        # marketable/mis-geometrized stop.\n        market_price = float(getattr(position, "market_price", 0.0) or 0.0)\n        if market_price <= 0:\n            return False\n\n        if position.qty > 0:\n            if order.side != OrderSide.SELL:\n                return False\n            if stop_price >= market_price:\n                return False\n        else:\n            if order.side != OrderSide.BUY:\n                return False\n            if stop_price <= market_price:\n                return False\n''',
    "profit-lock aware stop geometry",
)

# Route broker-confirmed stop updates through PortfolioTracker.note_stop so the
# immutable original-risk invariant has one owner.
engine = replace_once(
    engine,
    '''                if self.portfolio and sym in self.portfolio._lots:\n                    self.portfolio._lots[sym].stop_loss = verified_stop_price\n''',
    '''                if self.portfolio and sym in self.portfolio._lots:\n                    self.portfolio.note_stop(sym, verified_stop_price)\n''',
    "broker-confirmed stop accounting owner",
)

ENGINE.write_text(engine, encoding="utf-8")

TEST.write_text('''from datetime import UTC, datetime\n\nfrom app.core.domain import Order, OrderSide, OrderStatus, OrderType, Position\nfrom app.core.engine import TradingEngine\nfrom app.portfolio.tracker import PortfolioTracker, _OpenLot\n\n\ndef _stop(*, side, price):\n    return Order(\n        id="s", symbol="AAPL", side=side, qty=10.0,\n        order_type=OrderType.STOP, stop_price=price, status=OrderStatus.SUBMITTED,\n    )\n\n\ndef test_long_profit_lock_above_entry_is_valid_when_below_market():\n    engine = TradingEngine.__new__(TradingEngine)\n    p = Position(symbol="AAPL", qty=10.0, avg_entry_price=100.0, market_price=105.0)\n    assert engine._is_valid_protective_stop(_stop(side=OrderSide.SELL, price=103.0), p)\n\n\ndef test_long_stop_at_or_above_market_is_invalid():\n    engine = TradingEngine.__new__(TradingEngine)\n    p = Position(symbol="AAPL", qty=10.0, avg_entry_price=100.0, market_price=105.0)\n    assert not engine._is_valid_protective_stop(_stop(side=OrderSide.SELL, price=105.0), p)\n    assert not engine._is_valid_protective_stop(_stop(side=OrderSide.SELL, price=106.0), p)\n\n\ndef test_short_profit_lock_below_entry_is_valid_when_above_market():\n    engine = TradingEngine.__new__(TradingEngine)\n    p = Position(symbol="AAPL", qty=-10.0, avg_entry_price=100.0, market_price=95.0)\n    assert engine._is_valid_protective_stop(_stop(side=OrderSide.BUY, price=97.0), p)\n\n\ndef test_short_stop_at_or_below_market_is_invalid():\n    engine = TradingEngine.__new__(TradingEngine)\n    p = Position(symbol="AAPL", qty=-10.0, avg_entry_price=100.0, market_price=95.0)\n    assert not engine._is_valid_protective_stop(_stop(side=OrderSide.BUY, price=95.0), p)\n    assert not engine._is_valid_protective_stop(_stop(side=OrderSide.BUY, price=94.0), p)\n\n\ndef test_recovery_prefers_current_profit_lock_over_original_stop():\n    engine = TradingEngine.__new__(TradingEngine)\n    engine.portfolio = PortfolioTracker()\n    engine.portfolio._lots["AAPL"] = _OpenLot(\n        qty=10.0, avg_price=100.0, entry_ts=datetime.now(UTC), strategy="momentum",\n        stop_loss=103.0, initial_stop_loss=98.0, initial_risk_per_share=2.0,\n        initial_risk_dollars=20.0,\n    )\n    p = Position(symbol="AAPL", qty=10.0, avg_entry_price=100.0, market_price=105.0)\n    price, source = engine._resolve_recovery_stop(p)\n    assert price == 103.0\n    assert source == "PORTFOLIO_CURRENT_STOP"\n''', encoding="utf-8")

print("Applied profit-lock Rule-41 correctness patch.")
print("Changed: backend/app/core/engine.py")
print("Added: backend/tests/test_profit_lock_rule41.py")
