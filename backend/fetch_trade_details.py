import sqlite3, json, datetime
conn = sqlite3.connect('c:/Users/peddi/Desktop/Bots/Bot_QA/data/alphadesk.db')
c = conn.cursor()
c.execute('SELECT payload, exit_at, pnl FROM trades WHERE exit_at >= ?', ((datetime.datetime.utcnow() - datetime.timedelta(hours=24)).isoformat(),))
rows = c.fetchall()
trades = []
for row in rows:
    try:
        data = json.loads(row[0])
        trade = {
            "symbol": data.get("symbol"),
            "side": data.get("side"),
            "qty": data.get("qty"),
            "entry_price": data.get("entry_price"),
            "exit_price": data.get("exit_price"),
            "pnl": row[2],
            "entry_time": data.get("entry_time"),
            "exit_time": row[1],
            "exit_reason": data.get("exit_reason", "unknown")
        }
        trades.append(trade)
    except Exception as e:
        pass
for t in trades:
    print(t)
