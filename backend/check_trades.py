import sqlite3, json, datetime
conn = sqlite3.connect('c:/Users/peddi/Desktop/Bots/Bot_QA/backend/data/trades.db')
c = conn.cursor()
c.execute('SELECT payload, exit_at FROM trade_log WHERE exit_at >= ?', ((datetime.datetime.utcnow() - datetime.timedelta(hours=24)).isoformat(),))
rows = c.fetchall()
total_vol = 0
for row in rows:
    try:
        data = json.loads(row[0])
        vol = abs(data.get('qty', 0) * data.get('entry_price', 0))
        total_vol += vol
        print(f"{data.get('symbol')}: qty={data.get('qty')}, price={data.get('entry_price')}, vol={vol}, exit={row[1]}")
    except Exception as e:
        print(e)
print(f"Total 24h Volume: {total_vol}")
