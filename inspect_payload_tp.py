import sqlite3, json
conn = sqlite3.connect(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db")
c = conn.cursor()
c.execute("SELECT symbol, pnl, payload FROM trades WHERE exit_at LIKE '2026-08-03%'")
for r in c.fetchall():
    p = json.loads(r[2]) if r[2] else {}
    print(f"Symbol: {r[0]:<6} Master PnL: ${r[1]:<8.2f} TP in payload: {p.get('take_profit')} Exit Price: {p.get('exit_price')} Exit Reason: {p.get('exit_reason')}")
conn.close()
