import sqlite3
import pandas as pd

def check_recent_rejections():
    conn = sqlite3.connect('data/alphadesk.db')
    cur = conn.cursor()
    
    # Query shadow_ledger for last 10 sessions (since 2026-09-22)
    rows = cur.execute("""
        SELECT symbol, strategy, rejection_stage, rejection_reason, count(*) 
        FROM shadow_ledger 
        WHERE ts >= '2026-09-22' 
        GROUP BY symbol, strategy, rejection_stage, rejection_reason 
        ORDER BY count(*) DESC
    """).fetchall()
    
    print(f"Total grouped rejection entries since 2026-09-22: {len(rows)}")
    
    # Aggregate by stage and reason
    stage_reason = {}
    for sym, strat, stage, reason, cnt in rows:
        key = (stage or 'None', reason or 'None')
        stage_reason[key] = stage_reason.get(key, 0) + cnt
        
    print("\n--- REJECTION REASONS SINCE 2026-09-22 ---")
    for (st, re), count in sorted(stage_reason.items(), key=lambda x: x[1], reverse=True)[:30]:
        print(f"Count: {count:4d} | Stage: {st:25s} | Reason: {re}")

    conn.close()

if __name__ == '__main__':
    check_recent_rejections()
