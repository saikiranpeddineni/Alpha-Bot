from types import SimpleNamespace
import pytest

# We can unit test the position metadata reconstruction logic directly
def resolve_bootstrap_lots(raw_positions, orders):
    lots = {}
    for p in raw_positions:
        qty = float(p.qty)
        if getattr(p, "side", "") == "PositionSide.SHORT" or qty < 0:
            qty = -abs(qty)
        sym = p.symbol.upper()
        avg_price = float(p.avg_entry_price)

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
    return lots


def test_bootstrap_position_with_matching_stop_order():
    pos = SimpleNamespace(symbol="AAPL", qty=100, avg_entry_price=150.0, side="PositionSide.LONG")
    stop_order = SimpleNamespace(symbol="AAPL", stop_price=145.0, status="open")

    lots = resolve_bootstrap_lots([pos], [stop_order])
    assert "AAPL" in lots
    assert lots["AAPL"]["stop_loss"] == 145.0
    assert lots["AAPL"]["initial_risk_per_share"] == 5.0


def test_bootstrap_position_without_stop_order_uses_fallback():
    pos_long = SimpleNamespace(symbol="NVDA", qty=50, avg_entry_price=200.0, side="PositionSide.LONG")
    pos_short = SimpleNamespace(symbol="TSLA", qty=-20, avg_entry_price=100.0, side="PositionSide.SHORT")

    lots = resolve_bootstrap_lots([pos_long, pos_short], [])
    
    # Long fallback: 2% below avg_price -> 196.0
    assert lots["NVDA"]["stop_loss"] == 196.0
    assert lots["NVDA"]["initial_risk_per_share"] == 4.0

    # Short fallback: 2% above avg_price -> 102.0
    assert lots["TSLA"]["stop_loss"] == 102.0
    assert lots["TSLA"]["initial_risk_per_share"] == 2.0


def test_bootstrap_multiple_orders_filters_correct_symbol():
    pos = SimpleNamespace(symbol="MSFT", qty=10, avg_entry_price=300.0, side="PositionSide.LONG")
    unrelated_order = SimpleNamespace(symbol="AMZN", stop_price=100.0, status="open")
    matching_order = SimpleNamespace(symbol="MSFT", stop_price=290.0, status="open")

    lots = resolve_bootstrap_lots([pos], [unrelated_order, matching_order])
    assert lots["MSFT"]["stop_loss"] == 290.0
    assert lots["MSFT"]["initial_risk_per_share"] == 10.0
