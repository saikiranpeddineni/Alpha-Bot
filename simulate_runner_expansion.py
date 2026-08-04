import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(r"C:\Users\peddi\Desktop\Bots\Bot_QA")
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.pis.models import PositionObservation, PISConfig, ExitAction
from app.pis.position_brain import PositionBrain

MASTER_BARS = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db")
ET = timezone(timedelta(hours=-4))
MARKET_CLOSE_UTC = datetime(2026, 8, 3, 20, 0, 0, tzinfo=timezone.utc)

from run_detailed_replay_report import load_bars, estimate_atr, apply_slippage

conn = sqlite3.connect(str(MASTER_BARS))

runners_data = [
    {"symbol": "CRWV", "side": "long", "qty": 260, "entry_price": 78.5503, "entry_at": "2026-08-03T13:54:32.417387Z", "stop_loss": 77.15, "risk": 1.40, "fixed_tp": 80.78, "master_pnl": 592.34},
    {"symbol": "NBIS", "side": "long", "qty": 103, "entry_price": 199.4001, "entry_at": "2026-08-03T13:56:58.824707Z", "stop_loss": 195.79, "risk": 3.61, "fixed_tp": 204.88, "master_pnl": 580.91},
    {"symbol": "TQQQ", "side": "long", "qty": 309, "entry_price": 66.7398, "entry_at": "2026-08-03T14:47:04.757041Z", "stop_loss": 66.29, "risk": 0.45, "fixed_tp": 67.12, "master_pnl": 117.48},
]

print("=" * 120)
print("  SIMULATION: WHAT HAPPENS IF PIS LETS RUNNERS RUN TO 5R-6R (NO HARD BROKER TP)")
print("=" * 120)

for item in runners_data:
    sym = item["symbol"]
    qty = item["qty"]
    entry_price = item["entry_price"]
    entry_at = datetime.fromisoformat(item["entry_at"].replace("Z", "+00:00"))
    stop_loss = item["stop_loss"]
    risk = item["risk"]
    
    # Run PIS in pure Runner / Dynamic Extension mode (no hard take profit exit, only trailing stop + distribution)
    config = PISConfig()
    config.activation_mfe_r = 1.0
    config.runner_qualification_r = 1.5
    config.giveback_pct_runner = 0.25  # Trailing stop keeps 75% of peak profit
    
    bars = load_bars(sym, entry_at.timestamp(), conn)
    entry_ts_pd = pd.Timestamp(entry_at)
    bars_from_entry = bars[bars.index >= entry_ts_pd]
    
    row0 = bars_from_entry.iloc[0]
    ts0 = bars_from_entry.index[0].to_pydatetime()
    if ts0.tzinfo is None: ts0 = ts0.replace(tzinfo=timezone.utc)
    
    obs0 = PositionObservation(
        timestamp=ts0, symbol=sym, side=1.0, qty=qty, entry_price=entry_price,
        current_price=float(row0["close"]), high=float(row0["high"]), low=float(row0["low"]), close=float(row0["close"]),
        volume=float(row0["volume"]), atr=1.0, initial_stop=stop_loss, risk_per_share=risk,
        current_stop=stop_loss, take_profit=None, r_multiple=0.0, news_sentiment_score=0.0
    )
    
    brain = PositionBrain(sym, obs0, config)
    current_stop = stop_loss
    exit_px = None
    exit_at = None
    exit_reason = "EOD"
    
    for i, (bar_ts_pd, row) in enumerate(bars_from_entry.iterrows()):
        bar_ts = bar_ts_pd.to_pydatetime()
        if bar_ts.tzinfo is None: bar_ts = bar_ts.replace(tzinfo=timezone.utc)
        
        idx = bars.index.get_loc(bar_ts_pd)
        atr = estimate_atr(bars, idx)
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        r_mul = (close - entry_price) / risk
        
        if i > 0 and low <= current_stop:
            exit_px = apply_slippage(current_stop, 1.0)
            exit_at = bar_ts
            exit_reason = f"TRAILING_STOP_HIT (${current_stop:.2f})"
            break
            
        if bar_ts >= MARKET_CLOSE_UTC:
            exit_px = apply_slippage(close, 1.0)
            exit_at = bar_ts
            exit_reason = "MARKET_CLOSE (EOD)"
            break
            
        obs = PositionObservation(
            timestamp=bar_ts, symbol=sym, side=1.0, qty=qty, entry_price=entry_price,
            current_price=close, high=high, low=low, close=close, volume=float(row["volume"]), atr=atr,
            initial_stop=stop_loss, risk_per_share=risk, current_stop=current_stop,
            take_profit=None, r_multiple=r_mul, news_sentiment_score=0.0
        )
        decision = brain.observe(obs)
        if decision.target_stop_loss is not None and decision.target_stop_loss > current_stop:
            current_stop = decision.target_stop_loss
            
    runner_pnl = round((exit_px - entry_price) * qty, 2)
    delta = runner_pnl - item["master_pnl"]
    
    print(f"[{sym}] Entry: ${entry_price:.2f} | Initial Stop: ${stop_loss:.2f}")
    print(f"      Master Fixed TP Exit: ${item['fixed_tp']:.2f}  ->  Realized P&L: ${item['master_pnl']:>8.2f}")
    print(f"      PIS Dynamic Trailing: ${exit_px:.2f}  ->  Realized P&L: ${runner_pnl:>8.2f} ({exit_reason} at {exit_at.astimezone(ET).strftime('%H:%M:%S')} ET)")
    print(f"      EXTRA PROFIT CAPTURED: ${delta:>+8.2f} ({(runner_pnl / item['master_pnl'] - 1.0)*100:>+5.1f}% Gain!)\n")

conn.close()
