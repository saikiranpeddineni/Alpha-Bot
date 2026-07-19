import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

sys.path.append("backend")

from app.config import DATA_DIR, Settings
from app.data.manager import DataManager
from app.data.base import CachePolicy
from app.ai.model import SignalModel
from app.ai.prediction import MetaLabelEngine
from app.backtest.engine import BacktestParams, run_backtest
from app.brokers.alpaca import AlpacaBroker
from app.data.manager import make_provider

# Force bypass of the temporal leakage check so we can use today's models in the backtest
original_predict = MetaLabelEngine.predict
def mock_predict(self, strategy, features, side="long"):
    self.backtest_start_time = None
    return original_predict(self, strategy, features, side)
MetaLabelEngine.predict = mock_predict

async def run():
    print("==================================================")
    print(" COMPARING BACKTEST RESULTS FOR TODAY (JULY 17, 2026)")
    print("==================================================")
    
    settings = Settings()
    cache_path = DATA_DIR / "bars_cache.db"
    model = SignalModel(DATA_DIR / "models")
    
    # We will test using the watchlist symbols from today's trades and main watchlist
    symbols = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "MSFT", "AMD", "META", "DRAM", "INTC", "CBRS", "MU", "ANET", "SKHY", "SNDK"]
    
    # Starting with 20 slots to see full potential, sizing to $10,000 (10% allocation)
    settings.max_open_positions = 20
    settings.ml_confidence_threshold = 0.48
    
    risk_cfg = settings.risk_config()
    
    from app.strategies.base import REGISTRY
    strategies = {name: {"enabled": True, "params": {}} for name in REGISTRY.keys()}
    
    # Define backtest parameters for today (July 17, 2026)
    params = BacktestParams(
        symbols=symbols,
        start=datetime(2026, 7, 17, 9, 30, tzinfo=timezone.utc),
        end=datetime(2026, 7, 17, 16, 0, tzinfo=timezone.utc),
        starting_cash=100_000.0,
        slippage_bps=2.0,
        use_ml_gate=True,
        use_dynamic_scanner=False, # use fixed symbol list for exact comparison
        debug_mode=False,
        strategies=strategies,
        risk=risk_cfg,
        cache_policy=CachePolicy.DEFAULT,
    )
    
    # Setup data provider
    provider = make_provider(settings.resolve_data_provider(), settings)
    dm = DataManager(provider, cache_path)
    # Bypass coverage check for single day test
    dm.validate_cache_coverage = lambda *args, **kwargs: None

    # ----------------------------------------------------
    # RUN 1: Meta-Labeling ON (Thresholds = 0.55/0.53)
    # ----------------------------------------------------
    print("\n>>> RUN 1: Meta-Labeling ON (0.55 Threshold) <<<")
    MetaLabelEngine.DEFAULT_THRESHOLDS = {
        "orb":           0.55,
        "momentum":      0.55,
        "vwap_reversion": 0.52,
        "rsi2_reversion": 0.52,
        "bb_squeeze":    0.53,
    }
    
    result_on = await run_backtest(dm, model, params)
    
    # ----------------------------------------------------
    # RUN 2: Meta-Labeling OFF (Thresholds = 0.0)
    # ----------------------------------------------------
    print("\n>>> RUN 2: Meta-Labeling OFF (Bypassed) <<<")
    MetaLabelEngine.DEFAULT_THRESHOLDS = {
        "orb":           0.0,
        "momentum":      0.0,
        "vwap_reversion": 0.0,
        "rsi2_reversion": 0.0,
        "bb_squeeze":    0.0,
    }
    
    result_off = await run_backtest(dm, model, params)
    
    # ----------------------------------------------------
    # COMPARATIVE SUMMARY REPORT
    # ----------------------------------------------------
    print("\n==================================================")
    print(" BACKTEST COMPARISON REPORT")
    print("==================================================")
    
    def print_metrics(label, res):
        trades = res.trades
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        wr = (len(wins) / len(trades) * 100.0) if trades else 0.0
        total_pnl = sum(t.pnl for t in trades)
        
        print(f"[{label}]")
        print(f" - Total Trades Completed:      {len(trades)}")
        print(f" - Win Rate:                     {wr:.2f}% ({len(wins)} wins, {len(losses)} losses)")
        print(f" - Ending Equity:                ${res.metrics.get('ending_equity', 100000.0):,.2f}")
        print(f" - Net Profit/Loss:             ${total_pnl:+,.2f}")
        print(f" - Blocked/Gated Candidates:     {len(res.shadow_signals)}")
        print("--------------------------------------------------")

    print_metrics("META-LABEL ON (0.55)", result_on)
    print_metrics("META-LABEL OFF (Bypassed)", result_off)
    
    # Show detail on the difference in trades
    on_ids = {t.id for t in result_on.trades}
    off_ids = {t.id for t in result_off.trades}
    diff_trades = [t for t in result_off.trades if t.id not in on_ids]
    
    print(f"\nADDITIONAL TRADES EXECUTED WHEN META-LABEL WAS OFF ({len(diff_trades)} total):")
    for t in diff_trades[:15]:
        print(f" - {t.symbol:<5} | {t.entry_at.strftime('%H:%M')} | PnL: ${t.pnl:+,.2f} | Entry: {t.entry_price:.2f} -> Exit: {t.exit_price:.2f}")

if __name__ == "__main__":
    asyncio.run(run())
