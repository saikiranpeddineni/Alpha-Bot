import asyncio
import sys
from datetime import datetime, UTC
from pathlib import Path

sys.path.append("backend")

from app.config import DATA_DIR, RiskConfig
from app.data.manager import DataManager
from app.data.base import CachePolicy
from app.ai.model import SignalModel
from app.backtest.engine import BacktestParams, run_backtest

async def run():
    cache_path = DATA_DIR / "bars_cache.db"
    model = SignalModel(DATA_DIR / "models")
    risk_cfg = RiskConfig(
        risk_per_trade_pct=0.5, max_open_positions=4,
        max_daily_loss_pct=3.0, max_drawdown_pct=10.0,
        consecutive_loss_limit=3, cooldown_minutes=30,
        no_entry_minutes_before_close=30, flatten_minutes_before_close=5,
        ml_confidence_threshold=0.55,
        score_threshold_trend=55.0, score_threshold_range=70.0,
        score_threshold_volatile=65.0, score_threshold_event=75.0,
    )

    from app.strategies.base import REGISTRY
    strategies = {name: {"enabled": True if name != "rsi2_reversion" else False, "params": {}} for name in REGISTRY.keys()}

    params = BacktestParams(
        symbols=["SPY", "QQQ", "NVDA", "AMD", "TSLA", "AAPL", "META", "MSFT"],
        start=datetime(2026, 7, 6, tzinfo=UTC),
        end=datetime(2026, 7, 7, 23, 59, tzinfo=UTC),
        starting_cash=100_000.0,
        slippage_bps=3.0,
        use_ml_gate=True,
        use_dynamic_scanner=True,
        debug_mode=True,
        strategies=strategies,
        risk=risk_cfg,
        cache_policy=CachePolicy.IMMUTABLE_CACHE_ONLY,
    )

    class BlockingProvider:
        name = "blocking"
        async def get_bars(self, *a, **kw): raise RuntimeError("NETWORK BLOCKED")
        async def get_quotes(self, *a, **kw): return {}

    dm = DataManager(BlockingProvider(), cache_path)
    # Bypass coverage check since the cache is missing a single warmup day
    dm.validate_cache_coverage = lambda *args, **kwargs: None
    
    result = await run_backtest(dm, model, params)
    print(f"Run complete: {len(result.trades)} trades, {len(result.shadow_signals)} raw candidates")

if __name__ == "__main__":
    asyncio.run(run())
