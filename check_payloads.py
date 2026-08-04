import sqlite3
import json

conn = sqlite3.connect(r'C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db')
cur = conn.cursor()
cur.execute("SELECT symbol, payload FROM trades WHERE exit_at LIKE '2026-08-03%' AND symbol IN ('NBIS', 'CRWV')")
for row in cur.fetchall():
    sym = row[0]
    p = json.loads(row[1])
    print(f"Symbol: {sym}, Take Profit: {p.get('take_profit')}, Stop Loss: {p.get('stop_loss')}, Initial Risk: {p.get('initial_risk')}, Entry: {p.get('entry_price')}, Exit: {p.get('exit_price')}")
conn.close()
