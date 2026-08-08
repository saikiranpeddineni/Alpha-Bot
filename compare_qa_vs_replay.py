import sqlite3
import json
import pandas as pd
from pathlib import Path

# Load live trades
conn = sqlite3.connect('data/alphadesk.db')
df_live = pd.read_sql_query("SELECT symbol, pnl as live_pnl, payload FROM trades WHERE DATE(exit_at) = '2026-08-07'", conn)
conn.close()

live_trades = {}
for _, row in df_live.iterrows():
    p = json.loads(row['payload'])
    sym = row['symbol']
    # If multiple trades per symbol, store as list
    if sym not in live_trades:
        live_trades[sym] = []
    live_trades[sym].append({
        'pnl': row['live_pnl'],
        'entry_price': float(p.get('entry_price', 0)),
        'exit_price': float(p.get('exit_price', 0)),
        'qty': float(p.get('qty', 0))
    })

# Load replay trades
ledger_path = Path('data/replays/rpl_bb724765c7e9/ledger.json')
with open(ledger_path) as f:
    replay_data = json.load(f)

replay_trades = {}
for t in replay_data.get('trades', []):
    sym = t['symbol']
    if sym not in replay_trades:
        replay_trades[sym] = []
    details = t['details']
    replay_trades[sym].append({
        'pnl': details['pnl'],
        'entry_price': details['entry_price'],
        'exit_price': details['exit_price'],
        'qty': details['qty']
    })

# Compare
print("=== Missing in Replay ===")
for sym, trades in live_trades.items():
    if sym not in replay_trades:
        for t in trades:
            print(f"{sym}: Live PnL {t['pnl']} (Not in replay)")
    elif len(trades) > len(replay_trades[sym]):
        print(f"{sym}: Live has {len(trades)} trades, Replay has {len(replay_trades[sym])}")

print("\n=== PnL Differences ===")
for sym, r_trades in replay_trades.items():
    l_trades = live_trades.get(sym, [])
    if len(r_trades) == len(l_trades) and len(r_trades) == 1:
        rt = r_trades[0]
        lt = l_trades[0]
        diff = rt['pnl'] - lt['pnl']
        if abs(diff) > 5.0:
            print(f"{sym}: Live PnL {lt['pnl']:.2f}, Replay PnL {rt['pnl']:.2f}, Diff: {diff:.2f}")
            print(f"  Live: Entry {lt['entry_price']:.2f}, Exit {lt['exit_price']:.2f}, Qty {lt['qty']}")
            print(f"  Replay: Entry {rt['entry_price']:.2f}, Exit {rt['exit_price']:.2f}, Qty {rt['qty']}")
    elif len(r_trades) != len(l_trades):
        print(f"{sym}: Mismatched trade count (Live: {len(l_trades)}, Replay: {len(r_trades)})")
