import sqlite3
import pandas as pd
import json
import numpy as np
from datetime import datetime, timezone

# Load forensic trades from our saved json
json_path = r"c:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\reports\forensic_30_sessions_trades_with_bars.json"
with open(json_path, "r") as f:
    trades = json.load(f)

print(f"Loaded {len(trades)} trades from {json_path}")

# Convert to DataFrame
df = pd.DataFrame([{k: v for k, v in t.items() if k != "bars"} for t in trades])

# Let's inspect the distribution of trades with peak_r >= 1.0
high_peak_trades = [t for t in trades if t["peak_r"] >= 1.0]
print(f"Total trades with Peak R >= 1.0: {len(high_peak_trades)}")

# Sort by R surrendered descending
sorted_by_r_surrender = sorted(trades, key=lambda x: x["r_surrendered"], reverse=True)
sorted_by_dollar_surrender = sorted(trades, key=lambda x: x["dollars_surrendered"], reverse=True)

print("\n" + "="*90)
print("TOP 15 TRADES WITH HIGHEST DOLLARS SURRENDERED (PEAK $ -> REALIZED $)")
print("="*90)
header = f"{'SYM':<6} {'DATE':<11} {'STRAT':<14} {'SIDE':<5} {'PEAK R':<8} {'EXIT R':<8} {'PEAK $':<10} {'REAL P&L':<10} {'SURRENDER $':<12} {'EXIT REASON'}"
print(header)
print("-" * 105)
for t in sorted_by_dollar_surrender[:15]:
    reason = t['exit_reason'][:30] if t['exit_reason'] else "N/A"
    print(f"{t['symbol']:<6} {t['date']:<11} {t['strategy']:<14} {t['side']:<5} {t['peak_r']:<8.2f} {t['exit_r']:<8.2f} ${t['peak_dollars']:<9.2f} ${t['pnl']:<9.2f} ${t['dollars_surrendered']:<11.2f} {reason}")

print("\n" + "="*90)
print("TOP 15 TRADES WITH HIGHEST R-MULTIPLE SURRENDERED (PEAK R -> REALIZED R)")
print("="*90)
print(header)
print("-" * 105)
for t in sorted_by_r_surrender[:15]:
    reason = t['exit_reason'][:30] if t['exit_reason'] else "N/A"
    print(f"{t['symbol']:<6} {t['date']:<11} {t['strategy']:<14} {t['side']:<5} {t['peak_r']:<8.2f} {t['exit_r']:<8.2f} ${t['peak_dollars']:<9.2f} ${t['pnl']:<9.2f} ${t['dollars_surrendered']:<11.2f} {reason}")

# Detailed minute-by-minute bar trace for the top 5 worst givebacks
print("\n" + "="*90)
print("DETAILED MINUTE-BY-MINUTE BAR FORENSICS FOR WORST PROFIT GIVEBACKS")
print("="*90)

for idx, t in enumerate(sorted_by_dollar_surrender[:5], 1):
    print(f"\n--- CASE #{idx}: {t['symbol']} on {t['date']} ({t['strategy']} | {t['side'].upper()}) ---")
    print(f"Entry: ${t['entry_px']:.2f} at {t['entry_at']} | Exit: ${t['exit_px']:.2f} at {t['exit_at']}")
    print(f"Qty: {t['qty']} | Risk/Sh: ${t['risk_per_share']:.2f} | Init Risk: ${t['init_risk_dollars']:.2f}")
    print(f"Peak R: {t['peak_r']:.2f} (+${t['peak_dollars']:.2f}) -> Exit R: {t['exit_r']:.2f} (P&L: ${t['pnl']:.2f})")
    print(f"Surrendered: {t['r_surrendered']:.2f} R (${t['dollars_surrendered']:.2f}) | Exit Reason: {t['exit_reason']}")
    print(f"Total Bars: {t['total_bars']} | Peak at Bar #{t['peak_bar_idx']} ({t['peak_bar_ratio']*100:.0f}% into trade)")
    print(f"{'BAR':<4} {'TIME':<9} {'OPEN':<8} {'HIGH':<8} {'LOW':<8} {'CLOSE':<8} {'VOL':<7} {'PEAK R':<8} {'CLOSE R':<8}")
    print("-" * 75)
    for b in t["bars"]:
        # extract time from dt
        time_str = b["dt"].split(" ")[1] if " " in b["dt"] else b["dt"]
        print(f"{b['bar']:<4} {time_str:<9} {b['open']:<8.2f} {b['high']:<8.2f} {b['low']:<8.2f} {b['close']:<8.2f} {b['vol']:<7.0f} {b['peak_r_on_bar']:<8.2f} {b['close_r_on_bar']:<8.2f}")
