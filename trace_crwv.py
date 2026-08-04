import sys
import sqlite3
import json
import pandas as pd
from datetime import datetime, timezone, timedelta
from pathlib import Path
sys.path.insert(0, r"C:\Users\peddi\Desktop\Bots\Bot_QA\backend")
from app.pis.models import PositionObservation, PISConfig
from app.pis.position_brain import PositionBrain

ET = timezone(timedelta(hours=-4))
bars_conn = sqlite3.connect(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db")

# Load CRWV bars from 13:50 UTC
start_e = int(datetime(2026, 8, 3, 13, 40, 0, tzinfo=timezone.utc).timestamp())
end_e = int(datetime(2026, 8, 3, 14, 30, 0, tzinfo=timezone.utc).timestamp())
df = pd.read_sql("SELECT ts, open, high, low, close, volume FROM bars WHERE symbol='CRWV' AND tf='1Min' AND ts>=? AND ts<=? ORDER BY ts ASC", bars_conn, params=(start_e, end_e))
df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
df.set_index("dt", inplace=True)

print("CRWV Bars around entry:")
for dt, row in df.iterrows():
    print(f"  {dt.astimezone(ET).strftime('%H:%M:%S')} ET ({dt.strftime('%H:%M:%S')} UTC): O={row['open']:.2f} H={row['high']:.2f} L={row['low']:.2f} C={row['close']:.2f} V={row['volume']}")

entry_price = 78.5503
initial_stop = 77.15
risk_per_share = 1.4003
qty = 260.0
side = 1.0

bars_from_entry = df[df.index >= pd.Timestamp("2026-08-03 13:54:32+00:00")]
print("\nBars from entry count:", len(bars_from_entry))

config = PISConfig()
row0 = bars_from_entry.iloc[0]
ts0 = bars_from_entry.index[0].to_pydatetime()

obs0 = PositionObservation(
    timestamp=ts0, symbol="CRWV", side=side, qty=qty, entry_price=entry_price,
    current_price=float(row0["close"]), high=float(row0["high"]), low=float(row0["low"]), close=float(row0["close"]),
    volume=float(row0["volume"]), atr=1.0, initial_stop=initial_stop, risk_per_share=risk_per_share,
    current_stop=initial_stop, take_profit=80.78, r_multiple=0.0
)

brain = PositionBrain("CRWV", obs0, config)
print(f"\nInitial Brain state: peak_r={brain.peak_r:.2f}, stop_floor={brain.current_stop_floor:.2f}")

for bar_ts_pd, row in bars_from_entry.iterrows():
    bar_ts = bar_ts_pd.to_pydatetime()
    close = float(row["close"])
    high = float(row["high"])
    low = float(row["low"])
    vol = float(row["volume"])
    r_mul = (close - entry_price) * side / risk_per_share

    obs = PositionObservation(
        timestamp=bar_ts, symbol="CRWV", side=side, qty=qty, entry_price=entry_price,
        current_price=close, high=high, low=low, close=close, volume=vol, atr=1.0,
        initial_stop=initial_stop, risk_per_share=risk_per_share, current_stop=brain.current_stop_floor,
        take_profit=80.78, r_multiple=r_mul
    )
    decision = brain.observe(obs)
    reasons = [r.value if hasattr(r, "value") else str(r) for r in decision.reason_codes]
    print(f"Bar {bar_ts.astimezone(ET).strftime('%H:%M:%S')} ET: C={close:.2f} H={high:.2f} L={low:.2f} | R={r_mul:.2f} PeakR={brain.peak_r:.2f} | Floor={decision.target_stop_loss:.2f} | Health={decision.health.overall:.2f} | Action={decision.action.value} | Reasons={reasons}")
    if low <= decision.target_stop_loss:
        print(f"  --> LOW ({low:.2f}) <= STOP FLOOR ({decision.target_stop_loss:.2f})! BREACHED!")

bars_conn.close()
