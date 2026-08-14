import json
from collections import defaultdict

ledger = json.load(open('data/replays/rpl_65a9996ef51c/ledger.json'))
trades = ledger.get('trades', [])
actual_trades = [t['details'] for t in trades]

wins = [t for t in actual_trades if t.get('pnl', 0) > 0]
losses = [t for t in actual_trades if t.get('pnl', 0) <= 0]
total_pnl = sum(t.get('pnl', 0) for t in actual_trades)
total_r = sum(t.get('r_multiple', 0) for t in actual_trades)

print(f"Total Trades: {len(actual_trades)}")
print(f"Total PnL: ${total_pnl:.2f}")
print(f"Total R-Multiple: {total_r:.2f}R")
print(f"Win Rate: {len(wins) / len(actual_trades) if actual_trades else 0:.2%}")
print(f"Wins: {len(wins)}, Losses: {len(losses)}")
print("\nTrades:")

symbol_pnl = defaultdict(float)

for t in actual_trades:
    symbol = t['symbol']
    pnl = t.get('pnl', 0)
    symbol_pnl[symbol] += pnl
    r = t.get('r_multiple', 0)
    mfe = t.get('mfe_pct', 0)
    mae = t.get('mae_pct', 0)
    print(f"{symbol:5} | {t['side']:5} | PNL: ${pnl:7.2f} | R: {r:5.2f} | MFE: {mfe:6.2%} | MAE: {mae:6.2%} | Reason: {t.get('exit_reason')}")

print("\nPNL by Symbol:")
for sym, p in sorted(symbol_pnl.items(), key=lambda x: x[1], reverse=True):
    print(f"{sym:5}: ${p:.2f}")
