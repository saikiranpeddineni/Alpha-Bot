import sqlite3
import json

db_path = 'data/alphadesk.db'
conn = sqlite3.connect(db_path)
cur = conn.cursor()

try:
    rows = cur.execute('SELECT payload FROM shadow_ledger').fetchall()
    signals = [json.loads(r[0]) for r in rows]
    
    total_signals = len(signals)
    rejected_signals = [s for s in signals if s.get("final_decision") == "rejected"]
    accepted_signals = [s for s in signals if s.get("final_decision") == "accepted"]
    replayed_signals = [s for s in signals if s.get("replayed")]
    
    winning_replayed = [s for s in replayed_signals if s.get("simulated_pnl", 0) > 0]
    
    total_replayed = len(replayed_signals)
    win_rate = (len(winning_replayed) / total_replayed * 100.0) if total_replayed > 0 else 0.0
    
    avg_pnl = (sum(s.get("simulated_pnl", 0) for s in replayed_signals) / total_replayed) if total_replayed > 0 else 0.0
    avg_mfe = (sum(s.get("mfe_pct", 0) for s in replayed_signals) / total_replayed) if total_replayed > 0 else 0.0
    avg_mae = (sum(s.get("mae_pct", 0) for s in replayed_signals) / total_replayed) if total_replayed > 0 else 0.0
    
    print("==================================================")
    print(" SHADOW LEDGER ANALYTICS SUMMARY")
    print("==================================================")
    print(f"Total Candidate Signals Evaluated:  {total_signals}")
    print(f"Accepted Signals (Executed):        {len(accepted_signals)}")
    print(f"Rejected Signals (Blocked):         {len(rejected_signals)}")
    print(f"Replayed/Analyzed Rejections:       {total_replayed}")
    print(f"Win Rate of Replayed Rejections:    {win_rate:.2f}%")
    print(f"Avg Simulated Return:               {avg_pnl:.4f}%")
    print(f"Avg Maximum Favorable Excursion:    {avg_mfe:.4f}%")
    print(f"Avg Maximum Adverse Excursion:      {avg_mae:.4f}%")
    print("==================================================")

    # Strategy Breakdown
    by_strategy = {}
    for s in signals:
        strat = s.get("strategy", "unknown")
        if strat not in by_strategy:
            by_strategy[strat] = {"total": 0, "replayed": 0, "pnl": 0.0}
        by_strategy[strat]["total"] += 1
        if s.get("replayed"):
            by_strategy[strat]["replayed"] += 1
            by_strategy[strat]["pnl"] += s.get("simulated_pnl", 0.0)
            
    print("\nBREAKDOWN BY STRATEGY:")
    for strat, data in by_strategy.items():
        rep = data["replayed"]
        avg_ret = (data["pnl"] / rep) if rep > 0 else 0.0
        print(f" - {strat.upper():<12} | Candidates: {data['total']:<4} | Replayed: {rep:<4} | Avg Return: {avg_ret:+.4f}%")

    # Rejection Stage Breakdown
    by_rejection = {}
    for s in signals:
        if s.get("final_decision") != "rejected":
            continue
        stage = s.get("rejection_stage") or "unknown"
        if stage not in by_rejection:
            by_rejection[stage] = {"count": 0, "replayed": 0, "pnl": 0.0}
        by_rejection[stage]["count"] += 1
        if s.get("replayed"):
            by_rejection[stage]["replayed"] += 1
            by_rejection[stage]["pnl"] += s.get("simulated_pnl", 0.0)

    print("\nBREAKDOWN BY REJECTION GATE/STAGE:")
    for stage, data in by_rejection.items():
        rep = data["replayed"]
        avg_ret = (data["pnl"] / rep) if rep > 0 else 0.0
        print(f" - {stage:<20} | Blocked: {data['count']:<4} | Replayed: {rep:<4} | Avg Return: {avg_ret:+.4f}%")
    print("==================================================")

except Exception as e:
    print(f"Error: {e}")
finally:
    conn.close()
