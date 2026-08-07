import sqlite3, json, datetime

conn = sqlite3.connect('c:/Users/peddi/Desktop/Bots/Bot_QA/data/alphadesk.db')
c = conn.cursor()
yesterday = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=24)).isoformat()
c.execute("SELECT payload, pnl, exit_at FROM trades WHERE exit_at >= ? ORDER BY exit_at ASC", (yesterday,))
rows = c.fetchall()

print(f"Total Trades Today: {len(rows)}")

winners = []
losers = []
for row in rows:
    data = json.loads(row[0])
    pnl = row[1]
    trade_info = {
        "symbol": data.get("symbol"),
        "pnl": pnl,
        "strategy": data.get("strategy"),
        "exit_reason": data.get("exit_reason")
    }
    if pnl > 0:
        winners.append(trade_info)
    else:
        losers.append(trade_info)

avg_win = sum(w["pnl"] for w in winners) / len(winners) if winners else 0
avg_loss = sum(l["pnl"] for l in losers) / len(losers) if losers else 0
total_pnl = sum(r[1] for r in rows)

print(f"Total PnL: ${total_pnl:.2f}")
print(f"Winners: {len(winners)} (Avg Win: ${avg_win:.2f})")
print(f"Losers: {len(losers)} (Avg Loss: ${avg_loss:.2f})")
print(f"Win Rate: {(len(winners)/len(rows))*100 if rows else 0:.1f}%")

print("\n--- All Trades ---")
for r in rows:
    data = json.loads(r[0])
    print(f"{data.get('symbol')} | {data.get('side').upper()} | PnL: ${r[1]:.2f} | Reason: {data.get('exit_reason')} | Strat: {data.get('strategy')}")
