import asyncio
from datetime import datetime, UTC, timedelta
from app.backtest.engine import BacktestParams, run_backtest
from app.config import RiskConfig, Settings
from app.data.manager import DataManager
from app.data.synthetic import SyntheticProvider
from app.ai.model import SignalModel
import cProfile
import pstats
import pathlib

async def main():
    tmp_path = pathlib.Path("tmp_backtest_fast")
    tmp_path.mkdir(exist_ok=True)
    data = DataManager(SyntheticProvider(), tmp_path / "cache.db")
    model = SignalModel(tmp_path / "models", risk_config=RiskConfig())
    
    start_dt = datetime(2026, 1, 5, 9, 30, tzinfo=UTC)
    end_dt = datetime(2026, 1, 5, 16, 0, tzinfo=UTC)
    
    params = BacktestParams(
        symbols=["SPY"],
        start=start_dt,
        end=end_dt,
        starting_cash=100_000.0,
        risk=RiskConfig(),
    )
    
    print("Running fast backtest for 20 minutes...")
    
    # We profile this part
    pr = cProfile.Profile()
    pr.enable()
    
    await run_backtest(data, model, params, settings=Settings())
    
    pr.disable()
    print("Done. Stats:")
    ps = pstats.Stats(pr)
    ps.sort_stats('cumtime').print_stats(40)

if __name__ == "__main__":
    asyncio.run(main())
