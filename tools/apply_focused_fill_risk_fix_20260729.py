"""Surgically repair the remaining focused fill/risk failures.

Designed to run against the user's current unstaged consolidated tree.  It refuses
unknown source shapes and never invokes historical broad applicators.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
tracker = ROOT / "backend/app/portfolio/tracker.py"
partial_test = ROOT / "backend/tests/test_partial_fill_close_guard_hardening_20260729.py"
portfolio_test = ROOT / "backend/tests/test_portfolio.py"

# 1) Establish immutable original R geometry on the first broker-confirmed stop.
src = tracker.read_text(encoding="utf-8")
old = '''    def note_stop(self, symbol: str, stop_loss: float | None) -> None:\n        """Record the current broker-confirmed stop without redefining entry risk.\n\n        ``initial_*`` fields are entry-bound accounting evidence.  A later trailing\n        or profit-lock stop may cross breakeven and must never change the R\n        denominator used for MFE/MAE, capture efficiency, or realized R.\n        """\n        lot = self._lots.get(symbol.upper())\n        if lot and stop_loss is not None:\n            lot.stop_loss = stop_loss\n'''
new = '''    def note_stop(self, symbol: str, stop_loss: float | None) -> None:\n        """Record a broker-confirmed stop while preserving immutable entry risk.\n\n        The first valid stop may establish original R geometry when the entry fill\n        did not carry it.  Later trailing, breakeven, or runner ratchets update only\n        the current stop and can never rewrite the original R denominator.\n        """\n        lot = self._lots.get(symbol.upper())\n        if lot is None or stop_loss is None:\n            return\n        stop = float(stop_loss)\n        lot.stop_loss = stop\n        if (\n            lot.initial_stop_loss is None\n            and lot.initial_risk_per_share is None\n            and lot.initial_risk_dollars <= 0\n        ):\n            risk_per_share = abs(float(lot.avg_price) - stop)\n            if risk_per_share > 1e-12:\n                lot.initial_stop_loss = stop\n                lot.initial_risk_per_share = risk_per_share\n                lot.initial_risk_dollars = abs(float(lot.qty)) * risk_per_share\n'''
if old in src:
    tracker.write_text(src.replace(old, new, 1), encoding="utf-8")
elif new not in src:
    raise SystemExit("note_stop source shape is unknown; refusing unsafe patch")

# 2) Replace obsolete identifier/comment assertions with semantic source contracts.
test_src = partial_test.read_text(encoding="utf-8")
start = test_src.index("def test_incremental_fill_does_not_unconditionally_mark_order_filled():")
replacement = '''def test_incremental_fill_does_not_unconditionally_mark_order_filled():\n    body = _drain_source()\n    assert 'getattr(fill, "cumulative_qty"' in body\n    assert "OrderStatus.PARTIALLY_FILLED" in body\n    assert "order.filled_qty" in body\n    assert "order.filled_qty = fill.qty" not in body\n\n\ndef test_close_guard_is_not_released_merely_because_a_sell_fill_arrived():\n    body = _drain_source()\n    assert "if fill.side == OrderSide.SELL" not in body\n    assert "self.portfolio.on_fill" in body\n    assert "_pending_close_symbols.discard" in body\n    assert "_lots.get" in body\n\n\ndef test_close_guard_release_is_side_symmetric_for_long_exit_and_short_cover():\n    body = _drain_source()\n    apply_idx = body.index("self.portfolio.on_fill")\n    release_idx = body.index("_pending_close_symbols.discard", apply_idx)\n    release_context = body[apply_idx:release_idx + len("_pending_close_symbols.discard") + 100]\n    assert "OrderSide.SELL" not in release_context\n    assert "OrderSide.BUY" not in release_context\n\n\ndef test_partial_fill_keeps_close_guard_until_portfolio_accounting_finishes():\n    body = _drain_source()\n    apply_idx = body.index("self.portfolio.on_fill")\n    lot_idx = body.index("_lots.get", apply_idx)\n    release_idx = body.index("_pending_close_symbols.discard", lot_idx)\n    assert apply_idx < lot_idx < release_idx\n'''
partial_test.write_text(test_src[:start] + replacement, encoding="utf-8")

# 3) Add symmetry/immutability regressions once.
p = portfolio_test.read_text(encoding="utf-8")
marker = "def test_session_roll_resets_day_stats():"
extra = '''def test_r_multiple_uses_noted_stop_short():\n    p = PortfolioTracker()\n    p.on_fill(fill(side=OrderSide.SELL, qty=10, price=100.0))\n    p.note_stop("AAPL", 102.0)\n    trade = p.on_fill(fill(side=OrderSide.BUY, qty=10, price=96.0))\n    assert trade.r_multiple == pytest.approx(2.0)\n\n\ndef test_noted_stop_risk_is_immutable_across_ratchets():\n    p = PortfolioTracker()\n    p.on_fill(fill(qty=10, price=100.0))\n    p.note_stop("AAPL", 98.0)\n    p.note_stop("AAPL", 99.0)\n    p.note_stop("AAPL", 101.0)\n    lot = p._lots["AAPL"]\n    assert lot.stop_loss == pytest.approx(101.0)\n    assert lot.initial_stop_loss == pytest.approx(98.0)\n    assert lot.initial_risk_per_share == pytest.approx(2.0)\n    assert lot.initial_risk_dollars == pytest.approx(20.0)\n    trade = p.on_fill(fill(side=OrderSide.SELL, qty=10, price=104.0))\n    assert trade.r_multiple == pytest.approx(2.0)\n\n\ndef test_entry_supplied_initial_risk_is_not_redefined_by_note_stop():\n    p = PortfolioTracker()\n    p.on_fill(fill(qty=10, price=100.0), initial_stop=97.0)\n    p.note_stop("AAPL", 99.0)\n    lot = p._lots["AAPL"]\n    assert lot.stop_loss == pytest.approx(99.0)\n    assert lot.initial_stop_loss == pytest.approx(97.0)\n    assert lot.initial_risk_per_share == pytest.approx(3.0)\n    assert lot.initial_risk_dollars == pytest.approx(30.0)\n\n\n'''
if "def test_r_multiple_uses_noted_stop_short():" not in p:
    if marker not in p:
        raise SystemExit("portfolio test insertion anchor missing; refusing unsafe patch")
    p = p.replace(marker, extra + marker, 1)
    portfolio_test.write_text(p, encoding="utf-8")

print("Applied focused partial-fill test hardening and immutable initial-risk repair.")
print("Changed: backend/app/portfolio/tracker.py")
print("Changed: backend/tests/test_partial_fill_close_guard_hardening_20260729.py")
print("Changed: backend/tests/test_portfolio.py")
