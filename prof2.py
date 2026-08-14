import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent / 'backend'))

import asyncio
import threading
import time
import traceback
from backend.tests.test_backtest import test_backtest_runs_and_reports, SignalModel, data as b_data
from backend.app.config import RiskConfig

def stack_tracer():
    time.sleep(10)
    print("=== STACK TRACES ===")
    for thread_id, frame in sys._current_frames().items():
        print(f"Thread {thread_id}:")
        traceback.print_stack(frame)
    print("====================")
    
async def main():
    rc = RiskConfig(
        max_open_positions=2,
        max_daily_loss_pct=2.0,
        max_drawdown_pct=10.0,
        strategy_consecutive_loss_limit=2,
        strategy_cooldown_minutes=30,
        symbol_cooldown_minutes=30,
        no_entry_minutes_before_close=30,
        flatten_minutes_before_close=5,
        min_price=1.0,
        max_price=1000.0,
        min_volume=100_000,
        max_spread_pct=0.1,
    )
    d = b_data(Path('./tmp_prof'))
    model = SignalModel(Path('./tmp_prof/models'), risk_config=rc)
    
    await test_backtest_runs_and_reports(d, model, rc)

if __name__ == '__main__':
    Path('./tmp_prof').mkdir(exist_ok=True)
    t = threading.Thread(target=stack_tracer, daemon=True)
    t.start()
    asyncio.run(main())
