import sqlite3
import json

db_path = 'data/alphadesk.db'
conn = sqlite3.connect(db_path)
cur = conn.cursor()

try:
    rows = cur.execute('''
        SELECT simulated_pnl 
        FROM shadow_ledger 
        WHERE ts LIKE '2026-07-17%' AND final_decision = 'rejected' AND replayed = 1
    ''').fetchall()
    
    pnls = [r[0] for r in rows]
    total_pnl = sum(pnls)
    avg_pnl = total_pnl / len(pnls) if pnls else 0.0
    
    print(f"Total replayed trades: {len(pnls)}")
    print(f"Net cumulative PnL: {total_pnl:+.4f}%")
    print(f"Average PnL per trade: {avg_pnl:+.4f}%")
    
except Exception as e:
    print(f"Error: {e}")
finally:
    conn.close()
