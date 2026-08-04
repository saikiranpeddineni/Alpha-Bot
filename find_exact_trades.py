import sqlite3
import json

paths = [
    r"C:\Users\peddi\Desktop\Bots\Bot_QA\data\alphadesk.old.db",
    r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db"
]

for p in paths:
    print(f"\n--- Checking DB: {p} ---")
    try:
        conn = sqlite3.connect(p)
        cur = conn.cursor()
        cur.execute("SELECT symbol, payload FROM trades WHERE symbol IN ('INTC', 'IREN')")
        rows = cur.fetchall()
        for sym, payload_str in rows:
            data = json.loads(payload_str)
            entry_at = data.get("entry_at")
            exit_at = data.get("exit_at")
            pnl = data.get("pnl")
            strat = data.get("strategy")
            qty = data.get("qty")
            entry_px = data.get("entry_price")
            exit_px = data.get("exit_price")
            exit_reason = data.get("exit_reason")
            print(f"Symbol: {sym} | Strat: {strat} | Qty: {qty} | Entry: {entry_px} ({entry_at}) | Exit: {exit_px} ({exit_at}) | PnL: {pnl} | Reason: {exit_reason}")
        conn.close()
    except Exception as e:
        print(f"Error reading {p}: {e}")
