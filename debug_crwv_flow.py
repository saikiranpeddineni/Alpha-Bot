import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(r"C:\Users\peddi\Desktop\Bots\Bot_QA")
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.pis.position_brain import PositionBrain
from app.pis.models import PositionObservation, PISConfig, ExitAction

MASTER_BARS = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db")
conn_bars = sqlite3.connect(str(MASTER_BARS))

entry_at = datetime.fromisoformat("2026-08-03T13:38:15.540183+00:00")
start_ts = entry_at.timestamp() - 60
end_ts = entry_at.timestamp() + 18000

df = pd.read_sql("SELECT ts, open, high, low, close, volume FROM bars WHERE symbol = 'CRWV' AND ts >= ? AND ts <= ? ORDER BY ts ASC", conn_bars, params=(start_ts, end_ts))
df["datetime"] = pd.to_datetime(df["ts"], unit="s", utc=True)
df.set_index("datetime", inplace=True)

bars_from_entry = df[df.index >= pd.Timestamp(entry_at)]
print(f"Total bars from entry: {len(bars_from_entry)}")

config = PISConfig()
row0 = bars_from_entry.iloc[0]
ts0 = bars_from_entry.index[0].to_pydatetime()
obs0 = PositionObservation(
    timestamp=ts0, symbol="CRWV", side=1.0, qty=260, entry_price=78.55,
    current_price=float(row0["close"]), high=float(row0["high"]), low=float(row0["low"]), close=float(row0["close"]),
    volume=float(row0["volume"]), atr=0.60, initial_stop=77.63, risk_per_share=0.92,
    current_stop=77.63, take_profit=80.02, r_multiple=0.0
)

brain = PositionBrain("CRWV", obs0, config)

for i, (ts, r) in enumerate(bars_from_entry.iterrows()):
    c = float(r["close"])
    h = float(r["high"])
    l = float(r["low"])
    r_mul = (c - 78.55) / 0.92
    obs = PositionObservation(
        timestamp=ts.to_pydatetime(), symbol="CRWV", side=1.0, qty=260, entry_price=78.55,
        current_price=c, high=h, low=l, close=c, volume=float(r["volume"]), atr=0.60,
        initial_stop=77.63, risk_per_share=0.92, current_stop=77.63, take_profit=80.02, r_multiple=r_mul
    )
    dec = brain.observe(obs)
    print(f"Bar {i} ({ts.strftime('%H:%M')}): Close ${c:.2f} High ${h:.2f} Low ${l:.2f} -> Action: {dec.action} Ratio: {dec.exit_ratio} Stop: ${dec.target_stop_loss} Reasons: {[r.name for r in dec.reason_codes]}")
    if i >= 10:
        break

conn_bars.close()
