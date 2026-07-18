import sqlite3
import json

db_path = 'data/alphadesk.db'
conn = sqlite3.connect(db_path)
cur = conn.cursor()

try:
    rows = cur.execute('''
        SELECT id, ts, symbol, strategy, final_decision, rejection_reason, payload 
        FROM shadow_ledger 
        WHERE ts LIKE '2026-07-17%' AND symbol IN ('DRAM', 'INTC', 'SPY')
    ''').fetchall()
    
    print(f"Found {len(rows)} shadow ledger entries for DRAM, INTC, SPY today:")
    for r in rows:
        print(f" - ID: {r[0]} | TS: {r[1]} | Symbol: {r[2]} | Strat: {r[3]} | Decision: {r[4]} | Reason: {r[5]}")
except Exception as e:
    print(f"Error: {e}")
finally:
    conn.close()
