import sqlite3, json, datetime
import pandas as pd

conn = sqlite3.connect('c:/Users/peddi/Desktop/Bots/Bot_QA/data/alphadesk.db')
c = conn.cursor()
yesterday = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=24)).isoformat()
c.execute("SELECT payload, pnl FROM trades WHERE exit_at >= ? AND pnl < 0", (yesterday,))
rows = c.fetchall()

losers = []
for row in rows:
    data = json.loads(row[0])
    losers.append({
        "symbol": data.get("symbol"),
        "side": data.get("side"),
        "pnl": row[1],
        "strategy": data.get("strategy"),
        "exit_reason": data.get("exit_reason"),
        "mfe_pct": data.get("mfe_pct", 0),
        "mae_pct": data.get("mae_pct", 0),
        "hold_bars": data.get("hold_bars", 0)
    })

df = pd.DataFrame(losers)

print("--- Losers by Strategy ---")
print(df.groupby('strategy')['pnl'].agg(['count', 'mean', 'sum']))

print("\n--- Losers by Exit Reason ---")
print(df.groupby('exit_reason')['pnl'].agg(['count']))

print("\n--- Averages ---")
print(f"Average Hold Bars: {df['hold_bars'].mean():.1f}")
print(f"Average Max Favorable Excursion (MFE %): {df['mfe_pct'].mean():.2f}%")
print(f"Average Max Adverse Excursion (MAE %): {df['mae_pct'].mean():.2f}%")

# Find how many trades went into profit first but were held to a loss
went_profit_then_loss = df[df['mfe_pct'] > 0.5] # 0.5% in profit
print(f"\nTrades that went >0.5% in profit before losing: {len(went_profit_then_loss)} / {len(df)}")

never_in_profit = df[df['mfe_pct'] < 0.1]
print(f"Trades that basically never went into profit (<0.1%): {len(never_in_profit)} / {len(df)}")
