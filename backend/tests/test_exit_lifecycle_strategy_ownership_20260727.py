from pathlib import Path


def test_strategy_restore_uses_entry_ownership_source():
    db = (Path(__file__).parents[1] / "app" / "db" / "database.py").read_text(encoding="utf-8")
    engine = (Path(__file__).parents[1] / "app" / "core" / "engine.py").read_text(encoding="utf-8")
    assert "def get_latest_entry_order_payload" in db
    assert "def get_latest_entry_order_strategy" in db
    assert 'non_owning_strategies = {"emergency_protection"}' in db
    assert "get_latest_entry_order_payload(p.symbol, self.mode)" in engine


def test_reconciliation_restores_original_target_source():
    src = (Path(__file__).parents[1] / "app" / "core" / "engine.py").read_text(encoding="utf-8")
    assert 'take_profit = order_payload.get("take_profit")' in src
    assert "self._position_take_profits[p.symbol] = float(take_profit)" in src


def test_original_take_profit_has_live_safety_net_source():
    src = (Path(__file__).parents[1] / "app" / "core" / "engine.py").read_text(encoding="utf-8")
    assert "original strategy take-profit triggered" in src
    assert "high_val >= target" in src
    assert "low_val <= target" in src


def test_protection_repair_preserves_target_before_cancel_source():
    src = (Path(__file__).parents[1] / "app" / "core" / "engine.py").read_text(encoding="utf-8")
    preserve = src.index("if is_take_profit and limit_px is not None:")
    cancel = src.index("c_res = self.broker.cancel(o.id)", preserve)
    assert preserve < cancel
    assert "self._position_take_profits[p.symbol] = float(limit_px)" in src[preserve:cancel]
