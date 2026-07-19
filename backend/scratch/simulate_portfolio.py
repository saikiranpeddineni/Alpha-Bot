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
        
    # Sort signals chronologically by entry_dt
    signals.sort(key=lambda x: x["entry_dt"])
    
    print(f"Loaded {len(signals)} signals for simulation.")
    
    # Portfolio Settings
    STARTING_CASH = 100000.0
    MAX_OPEN_POSITIONS = 10
    ALLOCATION_PER_TRADE = 10000.0  # $10,000 per trade
    
    cash = STARTING_CASH
    open_positions = []  # list of dicts: {"symbol", "exit_dt", "allocation", "pnl_pct", "entry_dt"}
    executed_trades = []
    skipped_capacity = 0
    
    for s in signals:
        current_time = s["entry_dt"]
        
        # 1. Close any open positions that have exited before or at the current signal's entry time
        open_positions.sort(key=lambda x: x["exit_dt"])
        
        still_open = []
        for p in open_positions:
            if p["exit_dt"] <= current_time:
                realized_return = p["allocation"] * (p["pnl_pct"] / 100.0)
                cash += p["allocation"] + realized_return
                executed_trades.append({
                    "symbol": p["symbol"],
                    "strategy": p["strategy"],
                    "entry_dt": p["entry_dt"],
                    "exit_dt": p["exit_dt"],
                    "pnl_dollars": realized_return,
                    "pnl_pct": p["pnl_pct"]
                })
            else:
                still_open.append(p)
        open_positions = still_open
        
        # 2. Try to enter the current signal
        if len(open_positions) < MAX_OPEN_POSITIONS and cash >= ALLOCATION_PER_TRADE:
            cash -= ALLOCATION_PER_TRADE
            open_positions.append({
                "symbol": s["symbol"],
                "strategy": s["strategy"],
                "entry_dt": s["entry_dt"],
                "exit_dt": s["exit_dt"],
                "allocation": ALLOCATION_PER_TRADE,
                "pnl_pct": s["pnl_pct"]
            })
        else:
            skipped_capacity += 1
            
    # Close any remaining open positions at the end of the simulation
    for p in open_positions:
        realized_return = p["allocation"] * (p["pnl_pct"] / 100.0)
        cash += p["allocation"] + realized_return
        executed_trades.append({
            "symbol": p["symbol"],
            "strategy": p["strategy"],
            "entry_dt": p["entry_dt"],
            "exit_dt": p["exit_dt"],
            "pnl_dollars": realized_return,
            "pnl_pct": p["pnl_pct"]
        })
    open_positions = []
    
    final_equity = cash
    net_pnl = final_equity - STARTING_CASH
    pct_return = (net_pnl / STARTING_CASH) * 100.0
    
    # Analyze executed trades
    wins = [t for t in executed_trades if t["pnl_dollars"] > 0]
    losses = [t for t in executed_trades if t["pnl_dollars"] <= 0]
    
    print("\n==================================================")
    print(" PORTFOLIO WALK-FORWARD SIMULATION RESULTS")
    print("==================================================")
    print(f"Starting Cash:                  ${STARTING_CASH:,.2f}")
    print(f"Ending Equity:                  ${final_equity:,.2f}")
    print(f"Net Profit/Loss:               ${net_pnl:+,.2f} ({pct_return:+.2f}%)")
    print("--------------------------------------------------")
    print(f"Total Signals Offered:          {len(signals)}")
    print(f"Executed Trades:                {len(executed_trades)}")
    print(f"Skipped (Capacity/Cash limit):  {skipped_capacity}")
    print(f"Portfolio Win Rate:             {len(wins) / len(executed_trades) * 100.0 if executed_trades else 0.0:.2f}% ({len(wins)} wins, {len(losses)} losses)")
    print("==================================================")
    
    print("\nTOP 10 EXECUTED TRADES IN SIMULATION:")
    executed_trades.sort(key=lambda x: x["entry_dt"])
    for t in executed_trades[:10]:
        print(f" - {t['entry_dt'].strftime('%H:%M:%S')} -> {t['exit_dt'].strftime('%H:%M:%S')} | {t['symbol']:<6} ({t['strategy']:<10}) | PnL: ${t['pnl_dollars']:+,.2f} ({t['pnl_pct']:+.2f}%)")
        
except Exception as e:
    print(f"Error: {e}")
finally:
    conn.close()
