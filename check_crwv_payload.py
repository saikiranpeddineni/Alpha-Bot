import sqlite3, json
conn = sqlite3.connect(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db")
c = conn.cursor()
c.execute("SELECT id, symbol, pnl, payload FROM trades WHERE symbol = 'CRWV' AND exit_at LIKE '2026-08-03%'")
for r in c.fetchall():
    p = json.loads(r[3]) if r[3] else {}
    print("CRWV Payload keys:", p.keys())
    print("filled_at:", p.get("filled_at"), "entry_at:", p.get("entry_at"), "created_at:", p.get("created_at"), "entered_at:", p.get("entered_at"))
    print("entry_price:", p.get("entry_price"), "avg_entry_price:", p.get("avg_entry_price"))
conn.close()
