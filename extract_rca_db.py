import sqlite3
import pandas as pd
import json

def extract_rca(symbol):
    print(f"\n{symbol} OPPORTUNITY RCA")
    print("-" * 50)
    
    # Try all databases to find the latest
    db_files = ['alpha.db', 'alphadesk.db', 'alpha_test.db']
    latest_ts = None
    best_row = None
    
    for db in db_files:
        try:
            conn = sqlite3.connect(db)
            df = pd.read_sql(f"SELECT * FROM shadow_ledger WHERE symbol = '{symbol}' ORDER BY ts DESC LIMIT 10", conn)
            if not df.empty:
                ts = df.iloc[0]['ts']
                if latest_ts is None or ts > latest_ts:
                    latest_ts = ts
                    best_row = df.iloc[0]
        except Exception:
            pass
            
    if best_row is None:
        print("Discovery: No shadow_ledger entries found in databases.")
        return
        
    print(f"Latest timestamp found: {latest_ts}")
    print(f"Final Decision: {best_row.get('final_decision')}")
    print(f"Rejection Stage: {best_row.get('rejection_stage')}")
    print(f"Rejection Reason: {best_row.get('rejection_reason')}")
    print(f"Market Regime: {best_row.get('market_regime')}")
    print(f"Strategy: {best_row.get('strategy')}")
    print(f"Direction: {best_row.get('direction')}")
    
    gate_json = best_row.get('gate_evidence_json')
    if gate_json:
        try:
            ev = json.loads(gate_json)
            print("Gate Evidence:")
            for k, v in ev.items():
                print(f"  {k}: {v}")
        except:
            print("Gate Evidence: Invalid JSON")
    else:
        print("Gate Evidence: None")
        
    # Full row
    print("\nRow details:")
    print(best_row)

if __name__ == '__main__':
    extract_rca('NBIS')
    extract_rca('COHR')
