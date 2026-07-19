import sqlite3
import json
from datetime import datetime

db_path = 'data/alphadesk.db'
conn = sqlite3.connect(db_path)
cur = conn.cursor()

try:
    print("==================================================")
    print(" COMPLETED TRADES OVER THE PAST 4 DAYS")
    print("==================================================")
    
    rows = cur.execute('''
        SELECT id, exit_at, symbol, pnl, mode, payload 
        FROM trades 
        WHERE exit_at >= '2026-07-14'
        ORDER BY exit_at ASC
    ''').fetchall()
    
    print(f"Total completed trades found: {len(rows)}\n")
    
    by_date = {}
    for r in rows:
        exit_at_str = r[1]
        dt = datetime.fromisoformat(exit_at_str)
        date_str = dt.strftime('%Y-%m-%d')
        
        if date_str not in by_date:
            by_date[date_str] = []
        by_date[date_str].append(r)
        
    for date_str in sorted(by_date.keys()):
        trades_today = by_date[date_str]
        print(f"--- DATE: {date_str} (Trades: {len(trades_today)}) ---")
        day_pnl = 0.0
        for t in trades_today:
            payload = json.loads(t[5]) if t[5] else {}
            strategy = payload.get("strategy", "unknown")
            entry_price = payload.get("entry_price", 0.0)
            exit_price = payload.get("exit_price", 0.0)
            qty = payload.get("qty", 0)
            pnl_val = t[3]
            day_pnl += pnl_val
            print(f"   {t[1][11:19]} | {t[2]:<5} ({strategy:<10}) | PnL: ${pnl_val:+,.2f} | Entry: {entry_price:.2f} -> Exit: {exit_price:.2f} | Qty: {qty}")
        print(f"   >>> Net Day PnL: ${day_pnl:+,.2f} <<<\n")
        
except Exception as e:
    print(f"Error: {e}")
finally:
    conn.close()
