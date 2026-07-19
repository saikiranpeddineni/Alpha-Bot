import sqlite3
import json

db_path = 'data/alphadesk.db'
conn = sqlite3.connect(db_path)
cur = conn.cursor()

try:
    print("==================================================")
    print(" REJECTED TRADES FOR TODAY (JULY 17, 2026)")
    print("==================================================")
    
    rows = cur.execute('''
        SELECT id, ts, symbol, strategy, simulated_pnl, exit_reason, rejection_reason, replayed 
        FROM shadow_ledger 
        WHERE ts LIKE '2026-07-17%' AND final_decision = 'rejected'
    ''').fetchall()
    
    total_rejected = len(rows)
    replayed_rows = [r for r in rows if r[7] == 1]
    pending_rows = [r for r in rows if r[7] != 1]
    
    print(f"Total rejected signals today: {total_rejected}")
    print(f" - Pending replay: {len(pending_rows)}")
    print(f" - Replayed/analyzed: {len(replayed_rows)}")
    
    if len(replayed_rows) > 0:
        winning = [r for r in replayed_rows if r[4] > 0]
        losing = [r for r in replayed_rows if r[4] <= 0]
        win_rate = len(winning) / len(replayed_rows) * 100
        avg_pnl = sum(r[4] for r in replayed_rows) / len(replayed_rows)
        
        print(f"\nReplayed Win Rate: {win_rate:.2f}% ({len(winning)} wins, {len(losing)} losses)")
        print(f"Average PnL: {avg_pnl:+.4f}%")
        
        print("\nTOP 5 AVOIDED LOSSES (Rejections that saved money):")
        # Sort by simulated PnL ascending (worst performance)
        losing_sorted = sorted(losing, key=lambda x: x[4])
        for r in losing_sorted[:5]:
            print(f" - {r[2]} ({r[3]}): PnL: {r[4]:.4f}% | Reason Blocked: {r[6]}")
            
        print("\nTOP 5 MISSED WINNERS (Rejections that would have made money):")
        # Sort by simulated PnL descending (best performance)
        winning_sorted = sorted(winning, key=lambda x: x[4], reverse=True)
        for r in winning_sorted[:5]:
            print(f" - {r[2]} ({r[3]}): PnL: {r[4]:+.4f}% | Reason Blocked: {r[6]}")
            
except Exception as e:
    print(f"Error querying today's rejections: {e}")
finally:
    conn.close()
