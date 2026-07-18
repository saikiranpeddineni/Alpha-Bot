import sqlite3
import json
from datetime import datetime

db_path = 'data/alphadesk.db'
conn = sqlite3.connect(db_path)
cur = conn.cursor()

try:
    print("==================================================")
    print(" COMPLETED TRADES FOR TODAY (JULY 17, 2026)")
    print("==================================================")
    
    # Query trades table
    trades = cur.execute('''
        SELECT id, exit_at, symbol, pnl, mode, payload 
        FROM trades 
        WHERE exit_at LIKE '2026-07-17%'
        ORDER BY exit_at ASC
    ''').fetchall()
    
    print(f"Total completed trades: {len(trades)}")
    total_pnl = 0.0
    for t in trades:
        payload = json.loads(t[5]) if t[5] else {}
        entry_price = payload.get("entry_price", 0.0)
        exit_price = payload.get("exit_price", 0.0)
        qty = payload.get("qty", 0)
        strategy = payload.get("strategy", "unknown")
        pnl_val = t[3]
        total_pnl += pnl_val
        print(f" - Exit: {t[1]} | {t[2]} ({strategy}) | PnL: ${pnl_val:.2f} | Entry: {entry_price} -> Exit: {exit_price} | Qty: {qty}")
    
    print(f"\nNet Completed PnL: ${total_pnl:.2f}")

    print("\n==================================================")
    print(" ORDERS SUBMITTED TODAY (JULY 17, 2026)")
    print("==================================================")
    
    orders = cur.execute('''
        SELECT id, ts, mode, payload 
        FROM orders 
        WHERE ts LIKE '2026-07-17%'
        ORDER BY ts ASC
    ''').fetchall()
    
    print(f"Total orders: {len(orders)}")
    for o in orders:
        payload = json.loads(o[3]) if o[3] else {}
        symbol = payload.get("symbol", "unknown")
        side = payload.get("side", "unknown")
        qty = payload.get("qty", 0)
        limit_price = payload.get("limit_price")
        status = payload.get("status", "unknown")
        strategy = payload.get("strategy", "unknown")
        reason = payload.get("reason", "")
        print(f" - Time: {o[1]} | {symbol} {side} | Qty: {qty} @ {limit_price} | Status: {status} | Strat: {strategy} | {reason}")
        
except Exception as e:
    print(f"Error querying database: {e}")
finally:
    conn.close()
