import sqlite3
import json
from datetime import datetime, timedelta

db_path = 'data/alphadesk.db'
conn = sqlite3.connect(db_path)
cur = conn.cursor()

try:
    # Fetch all replayed signals for today
    rows = cur.execute('''
        SELECT ts, symbol, strategy, simulated_pnl, payload 
        FROM shadow_ledger 
        WHERE ts LIKE '2026-07-17%' AND final_decision = 'rejected' AND replayed = 1
    ''').fetchall()
    
    signals = []
    for r in rows:
        ts_str, symbol, strategy, pnl, payload_str = r
        payload = json.loads(payload_str)
        replay = payload.get("replay_60m", {})
        bars_held = replay.get("bars_held", 60)
        
        entry_dt = datetime.fromisoformat(ts_str)
        exit_dt = entry_dt + timedelta(minutes=bars_held)
        
        signals.append({
            "symbol": symbol,
            "strategy": strategy,
            "entry_dt": entry_dt,
            "exit_dt": exit_dt,
            "pnl_pct": pnl,
            "bars_held": bars_held
        })
        
    signals.sort(key=lambda x: x["entry_dt"])
    
    def run_sim(starting_cash, max_slots, trade_size):
        cash = starting_cash
        open_positions = []
        executed = []
        skipped = 0
        
        for s in signals:
            current_time = s["entry_dt"]
            
            # Close exited trades
            open_positions.sort(key=lambda x: x["exit_dt"])
            still_open = []
            for p in open_positions:
                if p["exit_dt"] <= current_time:
                    realized_return = p["allocation"] * (p["pnl_pct"] / 100.0)
                    cash += p["allocation"] + realized_return
                    executed.append({
                        "symbol": p["symbol"],
                        "pnl_dollars": realized_return,
                        "pnl_pct": p["pnl_pct"]
                    })
                else:
                    still_open.append(p)
            open_positions = still_open
            
            # Enter trade
            if len(open_positions) < max_slots and cash >= trade_size:
                cash -= trade_size
                open_positions.append({
                    "symbol": s["symbol"],
                    "strategy": s["strategy"],
                    "entry_dt": s["entry_dt"],
                    "exit_dt": s["exit_dt"],
                    "allocation": trade_size,
                    "pnl_pct": s["pnl_pct"]
                })
            else:
                skipped += 1
                
        # Close remaining
        for p in open_positions:
            realized_return = p["allocation"] * (p["pnl_pct"] / 100.0)
            cash += p["allocation"] + realized_return
            executed.append({
                "symbol": p["symbol"],
                "pnl_dollars": realized_return,
                "pnl_pct": p["pnl_pct"]
            })
            
        final_equity = cash
        net_pnl = final_equity - starting_cash
        pct_return = (net_pnl / starting_cash) * 100.0
        
        wins = [t for t in executed if t["pnl_dollars"] > 0]
        losses = [t for t in executed if t["pnl_dollars"] <= 0]
        win_rate = (len(wins) / len(executed) * 100.0) if executed else 0.0
        
        return final_equity, net_pnl, pct_return, len(executed), skipped, win_rate

    # Run Scenario 1
    eq1, pnl1, pct1, exec1, skip1, wr1 = run_sim(100000.0, 20, 5000.0)
    
    # Run Scenario 2
    eq2, pnl2, pct2, exec2, skip2, wr2 = run_sim(200000.0, 20, 10000.0)

    print("==================================================")
    print(" SCENARIO 1: $100k starting capital, $5k trade size")
    print("==================================================")
    print(f"Ending Equity:                  ${eq1:,.2f}")
    print(f"Net Profit/Loss:               ${pnl1:+,.2f} ({pct1:+.2f}%)")
    print(f"Executed Trades:                {exec1}")
    print(f"Skipped Trades:                 {skip1}")
    print(f"Win Rate:                       {wr1:.2f}%")
    print("==================================================")
    
    print("\n==================================================")
    print(" SCENARIO 2: $200k starting capital, $10k trade size")
    print("==================================================")
    print(f"Ending Equity:                  ${eq2:,.2f}")
    print(f"Net Profit/Loss:               ${pnl2:+,.2f} ({pct2:+.2f}%)")
    print(f"Executed Trades:                {exec2}")
    print(f"Skipped Trades:                 {skip2}")
    print(f"Win Rate:                       {wr2:.2f}%")
    print("==================================================")

except Exception as e:
    print(f"Error: {e}")
finally:
    conn.close()
