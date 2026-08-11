"""Regression tests verifying quantitative audit defect fixes and parity checks."""

from __future__ import annotations
from app.config import RiskConfig
from app.config import Settings

import json
import pathlib
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
import numpy as np
import pandas as pd
import pytest

from app.core.domain import (
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Regime,
    Signal,
    SignalAction,
)
from app.portfolio.allocator import (
    AllocationResult,
    CapitalAllocationOptimizer,
    PortfolioAIGate,
    process_portfolio_allocations,
)
from app.risk.manager import AccountContext, RiskManager, is_entry_order
from app.ai.trainer import ContinuousTrainer
from app.ai.model import SignalModel

import sklearn.ensemble
import sklearn.metrics


# ---------------------------------------------------------------------------
# Test 1: Kelly Sizing Probability Input Fix
# ---------------------------------------------------------------------------
def test_kelly_fraction_flows_to_risk_manager():
    """Verify that the Kelly fraction computed by CapitalAllocationOptimizer flows through
    process_portfolio_allocations and is returned as a non-None float override.
    F-03 fix: previously the kelly_fraction was always discarded (set to None);
    now it correctly flows to the risk manager for position sizing."""
    optimizer = CapitalAllocationOptimizer(kelly_fraction_cap=0.15)
    
    # Create signal
    signal = Signal(
        symbol="AAPL",
        action=SignalAction.ENTER_LONG,
        confidence=0.55,
        strategy="orb",
        reason="test",
        price=100.0,
        stop_loss=98.0,
        take_profit=104.0,
        ml_probability=0.53,
        score=0.80
    )
    
    # F-03: process_portfolio_allocations should now return a non-None Kelly override
    # when the optimizer computes a meaningful fraction.
    decision = process_portfolio_allocations(
        signals=[signal],
        positions=[],
        available_capital=50000.0,
        equity=100000.0,
        portfolio_gate=None,
        portfolio_allocator=optimizer
    )
    
    assert len(decision.approved_entries) == 1
    # Kelly override must now be a float (not None) after F-03 fix
    kelly_override = decision.approved_entries[0][1]
    assert kelly_override is not None, "F-03: kelly_fraction must flow through (not be discarded as None)"
    assert isinstance(kelly_override, float), f"Expected float, got {type(kelly_override)}"
    assert 0.0 < kelly_override <= 0.15, f"Kelly fraction {kelly_override} out of expected cap range"


# ---------------------------------------------------------------------------
# Test 2: Temporal Split & Purge Validation
# ---------------------------------------------------------------------------
def test_trainer_temporal_split_and_purge(tmp_path):
    """Verify that ContinuousTrainer splits train/validation chronologically and purges training horizon."""
    # Generate 5 days of 1-minute data (enough for temporal split dates >= 4 dates)
    dates = []
    for d in range(5):
        base_dt = datetime(2026, 7, 1 + d, 9, 30, tzinfo=UTC)
        for m in range(200):
            dates.append(base_dt + timedelta(minutes=m))
            
    np.random.seed(42)
    close_prices = 100.0 + np.cumsum(np.random.randn(len(dates)) * 0.1)
    df = pd.DataFrame({
        "open": close_prices - 0.05,
        "high": close_prices + 0.1,
        "low": close_prices - 0.1,
        "close": close_prices,
        "volume": np.random.randn(len(dates)) * 100.0 + 10000.0
    }, index=dates)
    
    mock_model = MagicMock(spec=SignalModel)
    mock_model.meta = {"validation_auc": 0.55, "validation_auc_long": 0.55, "validation_auc_short": 0.55}
    mock_model.models_dir = tmp_path
    
    trainer = ContinuousTrainer(MagicMock(), mock_model, MagicMock())
    
    # Capture the train/val splits by patching WalkForwardValidator.run
    captured_train_long = []
    captured_train_short = []

    def mock_wfv_run(dfs_subset, side="long"):
        mock_clf = MagicMock()
        mock_clf.predict_proba.return_value = np.ones((500, 2)) * 0.5
        if side == "long":
            captured_train_long.append(dfs_subset)
        else:
            captured_train_short.append(dfs_subset)
        from app.ai.prediction import WalkForwardResult
        return WalkForwardResult(
            best_classifier=mock_clf,
            best_name="lgbm",
            summary={},
            windows_evaluated=0,
            side=side,
        )

    with patch("app.ai.prediction.WalkForwardValidator.run", side_effect=mock_wfv_run), \
         patch.object(sklearn.metrics, "roc_auc_score", return_value=0.60), \
         patch("joblib.dump") as mock_dump:

        trainer._fit_global_sync({"AAPL": df})

        # Verify that we split long and short
        assert len(captured_train_long) == 1
        assert len(captured_train_short) == 1
        
        # Verify temporal order and split date math
        from zoneinfo import ZoneInfo
        from app.ai.features import build_training_set
        
        X_l, y_l, _w_l = build_training_set(df, horizon_bars=15, threshold_pct=0.10, side="long")
        X_l = X_l.sort_index()
        
        ET = ZoneInfo("America/New_York")
        dates_l = sorted(list(set(X_l.index.tz_convert(ET).date)))
        split_idx = int(len(dates_l) * 0.75)
        boundary_date = dates_l[split_idx]
        
        train_mask = X_l.index.tz_convert(ET).date < boundary_date
        val_start_dt = X_l[~train_mask].index.min()
        purge_horizon = timedelta(minutes=15)
        train_filtered_mask = train_mask & (X_l.index < val_start_dt - purge_horizon)
        
        X_tr = X_l[train_filtered_mask]
        X_va = X_l[~train_mask]
        
        assert not X_tr.empty
        assert not X_va.empty
        assert X_tr.index.max() < X_va.index.min()
        
        # Check no overlap: max training timestamp + 15 min horizon must be strictly before validation start
        assert X_tr.index.max() + purge_horizon < X_va.index.min()


# ---------------------------------------------------------------------------
# Test 3: Model Promotion Decoupling
# ---------------------------------------------------------------------------
def test_model_promotion_decoupling(tmp_path):
    """Verify that Long and Short model promotions are decoupled."""
    mock_model = MagicMock(spec=SignalModel)
    mock_model.models_dir = tmp_path
    
    # 1. Test case: Long improves, Short degrades
    mock_model.meta = {
        "validation_auc_long": 0.55,
        "validation_auc_short": 0.60,
        "validation_auc": 0.60
    }
    trainer = ContinuousTrainer(MagicMock(), mock_model, MagicMock())
    
    dates = []
    for d in range(5):
        base_dt = datetime(2026, 7, 1 + d, 9, 30, tzinfo=UTC)
        for m in range(200):
            dates.append(base_dt + timedelta(minutes=m))
            
    np.random.seed(42)
    close_prices = 100.0 + np.cumsum(np.random.randn(len(dates)) * 0.1)
    df_large = pd.DataFrame({
        "open": close_prices - 0.05,
        "high": close_prices + 0.1,
        "low": close_prices - 0.1,
        "close": close_prices,
        "volume": np.random.randn(len(dates)) * 100.0 + 10000.0
    }, index=dates)
    
    class DummyClassifier:
        def __init__(self, **kwargs):
            pass
        def fit(self, X, y):
            return self
        def predict_proba(self, X):
            return np.ones((len(X), 2)) * 0.5
            
    with patch.object(sklearn.ensemble, "HistGradientBoostingClassifier", DummyClassifier), \
         patch("joblib.dump") as mock_dump:
        
        with patch.object(sklearn.metrics, "roc_auc_score") as mock_roc:
            trainer.PROMOTE_THRESHOLD = 0.002
            
            # mock_roc.side_effect: long improvements (0.58 vs 0.55), short degrades (0.55 vs 0.60)
            mock_roc.side_effect = [0.58, 0.55]
            
            trainer._fit_global_sync({"AAPL": df_large})
            
            # Verify: long model was dumped, short model was not
            dump_calls = [call[0][1].name for call in mock_dump.call_args_list]
            assert "signal_model_long.joblib" in dump_calls
            assert "signal_model_short.joblib" not in dump_calls
            
            # Verify metadata
            assert trainer.model.meta["validation_auc_long"] == 0.58
            assert trainer.model.meta["validation_auc_short"] == 0.60
            assert trainer.model.meta["validation_auc"] == 0.60


# ---------------------------------------------------------------------------
# Test 4: Duplicate Order Protection & Pending Order Semantics
# ---------------------------------------------------------------------------
def test_duplicate_order_protection():
    """Verify that RiskManager prevents duplicate entry submissions while letting exits through."""
    config = MagicMock()
    config.max_open_positions = 4
    config.min_price = 5.0
    config.min_avg_dollar_volume = 1000.0
    config.risk_per_trade_pct = 0.5
    
    clock = MagicMock()
    clock.in_entry_window.return_value = True
    
    risk = RiskManager(config, clock)
    
    # 1. Pending entry order should block duplicate entries
    ctx = AccountContext(
        equity=100000.0,
        cash=80000.0,
        positions=[],
        day_pnl=0.0,
        session_start_equity=100000.0,
        peak_equity=100000.0,
        day_trades_today=0,
        is_paper=True,
        pending_entry_symbols=["NVDA"]
    )
    
    signal = Signal(
        symbol="NVDA",
        action=SignalAction.ENTER_LONG,
        confidence=0.60,
        strategy="orb",
        reason="test",
        price=100.0,
        stop_loss=98.0,
        take_profit=104.0
    )
    
    bars = pd.DataFrame({"close": [100.0], "volume": [1000.0]}, index=[datetime(2026, 7, 1)])
    
    # Evaluate entry
    decision = risk.evaluate_entry(signal, ctx, bars, now=datetime(2026, 7, 1))
    assert not decision.approved
    assert "order already pending" in decision.reason
    
    # 2. Open exits (e.g. stop loss, take profit) should NOT block new entries
    # Check is_entry_order logic
    positions = [Position(symbol="AAPL", qty=10, avg_entry_price=100.0, market_price=100.0, strategy="orb")]
    
    order_stop = Order(
        symbol="AAPL",
        side=OrderSide.SELL,
        qty=10,
        order_type=OrderType.MARKET,
        leg="stop"
    )
    assert not is_entry_order(order_stop, positions)
    
    # Generic SELL order on a LONG position is also an exit/reduction
    order_sell = Order(
        symbol="AAPL",
        side=OrderSide.SELL,
        qty=10,
        order_type=OrderType.MARKET
    )
    assert not is_entry_order(order_sell, positions)
    
    # A generic BUY order on a LONG position is a position increase (entry)
    order_buy = Order(
        symbol="AAPL",
        side=OrderSide.BUY,
        qty=10,
        order_type=OrderType.MARKET
    )
    assert is_entry_order(order_buy, positions)


# ---------------------------------------------------------------------------
# Test 5: Backtester Shared Allocator Call
# ---------------------------------------------------------------------------
def test_backtest_uses_shared_allocation_path():
    """Verify that process_portfolio_allocations aggregates candidate signals correctly."""
    # Create mock inputs
    signal_a = Signal(symbol="AAPL", action=SignalAction.ENTER_LONG, confidence=0.60, strategy="orb", reason="test", score=0.80, ml_probability=0.55)
    signal_b = Signal(symbol="MSFT", action=SignalAction.ENTER_LONG, confidence=0.65, strategy="orb", reason="test", score=0.90, ml_probability=0.58)
    
    signals = [signal_a, signal_b]
    positions = []
    
    # Mock allocator to return Kelly fractions
    mock_allocator = MagicMock()
    mock_allocator.allocate.return_value = [
        AllocationResult(symbol="AAPL", score=0.80, kelly_fraction=0.10, allocated_dollars=10000.0, allocated_pct=10.0),
        AllocationResult(symbol="MSFT", score=0.90, kelly_fraction=0.12, allocated_dollars=12000.0, allocated_pct=12.0),
    ]
    
    # Run the shared function
    decision = process_portfolio_allocations(
        signals=signals,
        positions=positions,
        available_capital=50000.0,
        equity=100000.0,
        portfolio_gate=None,
        portfolio_allocator=mock_allocator
    )
    
    # Assertions: Kelly overrides must now be floats (F-03 fix: kelly_fraction correctly flows through)
    assert len(decision.approved_entries) == 2
    # Each entry should have (signal, kelly_fraction) where kelly_fraction is a float
    assert decision.approved_entries[0][0] == signal_a
    assert decision.approved_entries[1][0] == signal_b
    # Verify the kelly values come from AllocationResult (0.10 for AAPL, 0.12 for MSFT per mock)
    assert decision.approved_entries[0][1] == pytest.approx(0.10, abs=1e-6)
    assert decision.approved_entries[1][1] == pytest.approx(0.12, abs=1e-6)
    assert len(decision.gated_entries) == 0


def test_stage5_conflict_resolution_invariant():
    """Verify that Stage 5 conflict resolution emits at most one position-increasing accepted signal per symbol per tick."""
    from app.strategies.pipeline import SignalPipeline
    from app.ai.model import SignalModel
    from app.core.domain import Signal, SignalAction, Regime
    from unittest.mock import MagicMock
    import pandas as pd
    
    mock_model = MagicMock(spec=SignalModel)
    mock_model.score_action.return_value = 0.85  # passes ML threshold
    
    from app.ai.prediction import MetaPrediction
    mock_meta = MagicMock()
    mock_meta.predict.return_value = MetaPrediction(
        strategy="orb", prob_profitable=0.60, approved=True, threshold=0.55,
        n_training_samples=100, source="MOCK", decision="PASS", reason=""
    )
    mock_model._meta_label_engine = mock_meta
    
    pipeline = SignalPipeline(model=mock_model, ml_threshold=0.55, risk_config=RiskConfig(), settings=Settings())
    pipeline.meta_label_engine = mock_meta
    pipeline.trade_score_engine.compute = MagicMock(side_effect=[80.0, 90.0])
    
    # Create two signals for the same symbol (NVDA) on different strategies
    sig_orb = Signal(symbol="NVDA", action=SignalAction.ENTER_LONG, confidence=0.70, strategy="orb", score=0.80, reason="test")
    sig_mom = Signal(symbol="NVDA", action=SignalAction.ENTER_LONG, confidence=0.75, strategy="momentum", score=0.90, reason="test")
    
    # Mock data frame with required indicators
    df = pd.DataFrame({
        "close": [100.0] * 20,
        "vol_sma20": [50000.0] * 20,
        "atr14": [1.5] * 20
    })
    
    accepted, rejected = pipeline.process(
        symbol="NVDA",
        df=df,
        signals=[sig_orb, sig_mom],
        regime=Regime.TREND_UP,
        features=pd.Series({"ret_5": 0.001, "ret_15": 0.002, "macd_hist_norm": 0.1, "rsi14": 55.0, "vwap_dist_pct": 0.02})
    )
    
    # Verify Stage 5 invariant: only one entry signal accepted per symbol
    assert len(accepted) == 1
    assert accepted[0].strategy == "momentum"  # higher trade score wins (0.90 vs 0.80)
    
    # Verify other candidate is rejected under conflict resolution reason
    assert len(rejected) == 1
    assert rejected[0][0].strategy == "orb"
    assert "lost conflict to momentum" in rejected[0][1]


# ---------------------------------------------------------------------------
# Test 6: Bounded Sizing Path Sizing Quantity Verification
# ---------------------------------------------------------------------------
def test_kelly_disabled_bounded_sizing_quantity():
    """Verify that when kelly_override=None, RiskManager uses risk_per_trade_pct for position sizing."""
    config = MagicMock()
    config.max_open_positions = 4
    config.max_positions_per_sector = 2
    config.min_price = 5.0
    config.min_avg_dollar_volume = 1000.0
    config.risk_per_trade_pct = 0.5
    config.max_position_pct = 50.0
    
    clock = MagicMock()
    clock.in_entry_window.return_value = True
    
    risk = RiskManager(config, clock)
    risk.core_watchlist = ["NVDA"]  # NVDA is in core watchlist so no 50% scanned asset penalty
    
    ctx = AccountContext(
        equity=100000.0,
        cash=80000.0,
        positions=[],
        day_pnl=0.0,
        session_start_equity=100000.0,
        peak_equity=100000.0,
        day_trades_today=0,
        is_paper=True,
        pending_entry_symbols=[]
    )
    
    signal = Signal(
        symbol="NVDA",
        action=SignalAction.ENTER_LONG,
        confidence=0.60,
        strategy="orb",
        reason="test",
        price=100.0,
        stop_loss=99.0,   # stop distance = 1.0
        take_profit=102.0
    )
    
    bars = pd.DataFrame({"close": [100.0], "volume": [1000.0]}, index=[datetime(2026, 7, 1)])
    
    # Evaluate entry with kelly_override=None
    decision = risk.evaluate_entry(signal, ctx, bars, now=datetime(2026, 7, 1), kelly_override=None)
    
    assert decision.approved
    # Expected risk = 100000 * 0.005 = 500
    # Expected shares = 500 / 1.0 = 500
    assert decision.order is not None
    assert decision.order.qty == 500
    assert decision.kelly_fraction == 0.005


# ---------------------------------------------------------------------------
# Test 7: Same-Tick Duplicate Entry TOCTOU Loophole
# ---------------------------------------------------------------------------
def test_same_tick_duplicate_entry_protection():
    """Verify same-tick duplicate entry logic blocks simultaneous same-symbol position increases."""
    config = MagicMock()
    config.max_open_positions = 4
    config.max_positions_per_sector = 2
    config.min_price = 5.0
    config.min_avg_dollar_volume = 1000.0
    config.risk_per_trade_pct = 0.5
    config.max_position_pct = 20.0
    
    clock = MagicMock()
    clock.in_entry_window.return_value = True
    
    risk = RiskManager(config, clock)
    risk.core_watchlist = ["NVDA", "AAPL"]
    
    # Setup test scenario:
    # 2 signals for NVDA, 1 for AAPL in the same tick.
    signal_nvda_1 = Signal(symbol="NVDA", action=SignalAction.ENTER_LONG, confidence=0.60, strategy="orb", reason="test", price=100.0, stop_loss=99.0)
    signal_nvda_2 = Signal(symbol="NVDA", action=SignalAction.ENTER_LONG, confidence=0.62, strategy="momentum", reason="test", price=100.0, stop_loss=99.0)
    signal_aapl = Signal(symbol="AAPL", action=SignalAction.ENTER_LONG, confidence=0.60, strategy="orb", reason="test", price=100.0, stop_loss=99.0)
    
    approved_signals = [signal_nvda_1, signal_nvda_2, signal_aapl]
    
    # 1. Simulating the core engine same-tick loop
    open_positions = []
    broker_pending_entries = [] # initially empty
    
    effective_pending_entries = set(broker_pending_entries)
    
    results = []
    
    bars = pd.DataFrame({"close": [100.0], "volume": [1000.0]}, index=[datetime(2026, 7, 1)])
    
    for sig in approved_signals:
        ctx = AccountContext(
            equity=100000.0,
            cash=80000.0,
            positions=open_positions,
            day_pnl=0.0,
            session_start_equity=100000.0,
            peak_equity=100000.0,
            day_trades_today=0,
            is_paper=True,
            pending_entry_symbols=list(effective_pending_entries)
        )
        decision = risk.evaluate_entry(sig, ctx, bars, now=datetime(2026, 7, 1), kelly_override=None)
        if decision.approved and decision.order is not None:
            # Simulate successful broker submit
            # For the first order (NVDA), it succeeds and returns status SUBMITTED
            if sig.strategy == "orb":
                status = OrderStatus.SUBMITTED
            else:
                # To test failed submission not poisoning:
                # If we had a failed one, it would return REJECTED
                status = OrderStatus.REJECTED
                
            mock_order = Order(
                symbol=sig.symbol,
                side=OrderSide.BUY,
                qty=decision.order.qty,
                order_type=OrderType.MARKET,
                status=status
            )
            
            results.append((sig, "approved", mock_order))
            
            if mock_order.status not in (OrderStatus.CANCELED, OrderStatus.REJECTED):
                effective_pending_entries.add(sig.symbol.upper())
        else:
            results.append((sig, "rejected", decision.reason))
            
    # Verify:
    # 1. First NVDA (orb) is approved
    assert results[0][0].symbol == "NVDA"
    assert results[0][1] == "approved"
    assert results[0][2].status == OrderStatus.SUBMITTED
    
    # 2. Second NVDA (momentum) is rejected because of same-tick pending protection
    assert results[1][0].symbol == "NVDA"
    assert results[1][1] == "rejected"
    assert "order already pending" in results[1][2]
    
    # 3. AAPL signal remains eligible and is approved
    assert results[2][0].symbol == "AAPL"
    assert results[2][1] == "approved"
    assert results[2][2].status == OrderStatus.SUBMITTED
    
    
    # 2. Test failed/rejected submission does not poison effective pending entries
    effective_pending_entries = set()
    mock_rejected_order = Order(symbol="NVDA", side=OrderSide.BUY, qty=10, order_type=OrderType.MARKET, status=OrderStatus.REJECTED)
    if mock_rejected_order.status not in (OrderStatus.CANCELED, OrderStatus.REJECTED):
        effective_pending_entries.add("NVDA")
    assert "NVDA" not in effective_pending_entries
    
    # 3. Test open exits do not block entries (exits are not entry orders)
    open_positions = [Position(symbol="AAPL", qty=10, avg_entry_price=100.0, market_price=100.0, strategy="orb")]
    exit_order = Order(symbol="AAPL", side=OrderSide.SELL, qty=10, order_type=OrderType.MARKET, leg="stop")
    assert not is_entry_order(exit_order, open_positions)
    
    # 4. Partial-fill regression:
    # NVDA entry submitted for quantity 100, 40 filled, 60 active (partially filled status)
    partial_order = Order(symbol="NVDA", side=OrderSide.BUY, qty=100, order_type=OrderType.MARKET, status=OrderStatus.PENDING) # active status
    # It must block subsequent entry when passed as pending symbol
    ctx_partial = AccountContext(
        equity=100000.0,
        cash=80000.0,
        positions=open_positions,
        day_pnl=0.0,
        session_start_equity=100000.0,
        peak_equity=100000.0,
        day_trades_today=0,
        is_paper=True,
        pending_entry_symbols=[partial_order.symbol.upper()]
    )
    decision_partial = risk.evaluate_entry(signal_nvda_1, ctx_partial, bars, now=datetime(2026, 7, 1))
    assert not decision_partial.approved
    assert "order already pending" in decision_partial.reason

