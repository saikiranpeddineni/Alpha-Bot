import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add backend directory to sys.path so we can import app modules directly
backend_path = str(Path(__file__).parent.resolve())
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.backtest.engine import BacktestParams, run_backtest
from app.data.manager import DataManager, make_provider
from app.ai.model import SignalModel
from app.config import get_settings, MODELS_DIR, DATA_DIR

async def main():
    settings = get_settings()
    provider_name = settings.resolve_data_provider("paper")
    print(f"Using provider: {provider_name}")
    
    # Monkeypatch validate_cache_coverage to avoid coverage validation failure network lookups
    def mock_validate(self, symbol, timeframe, start, end, cached_df):
        pass
    DataManager.validate_cache_coverage = mock_validate

    db_path = DATA_DIR / "bars_cache.db"
    provider = make_provider(provider_name, settings)
    data = DataManager(provider, db_path)
    
    # Wrap history to log loading progress
    orig_history = data.history
    async def verbose_history(symbol, timeframe, start, end, prefer_cache=True, cache_policy=None):
        print(f"data.history: symbol={symbol} timeframe={timeframe} start={start} end={end}")
        return await orig_history(symbol, timeframe, start, end, prefer_cache, cache_policy)
    data.history = verbose_history
    
    model = SignalModel(MODELS_DIR)
    
    # We run backtest for July 6, 2026 to July 7, 2026
    params = BacktestParams(
        symbols=["AMD", "ANET", "CBRS"],
        start=datetime(2026, 7, 6, 0, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 7, 6, 14, 15, 0, tzinfo=timezone.utc),
        starting_cash=100_000.0,
        use_ml_gate=True,
        use_dynamic_scanner=True,
    )
    
    print("Starting reproduction backtest...")
    result = await run_backtest(data, model, params)
    print("Reproduction backtest complete!")
    print(f"Bars processed: {result.bars_processed}")

if __name__ == "__main__":
    asyncio.run(main())
