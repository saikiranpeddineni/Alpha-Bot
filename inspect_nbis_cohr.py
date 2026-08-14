import sqlite3
import pandas as pd
import json
import os

SYMBOLS = ['NBIS', 'COHR']

def query_db():
    conn = sqlite3.connect('alpha.db')
    
    # Check shadow_ledger
    try:
        df = pd.read_sql("SELECT * FROM shadow_ledger WHERE symbol IN ('NBIS', 'COHR') ORDER BY timestamp DESC LIMIT 50", conn)
        print("--- shadow_ledger ---")
        print(df.to_string())
    except Exception as e:
        print("shadow_ledger error:", e)
        
    try:
        df = pd.read_sql("SELECT * FROM policy_rejections WHERE symbol IN ('NBIS', 'COHR') ORDER BY timestamp DESC LIMIT 50", conn)
        print("--- policy_rejections ---")
        print(df.to_string())
    except Exception as e:
        print("policy_rejections error:", e)
        
    try:
        df = pd.read_sql("SELECT * FROM policy_evidence WHERE symbol IN ('NBIS', 'COHR') ORDER BY timestamp DESC LIMIT 50", conn)
        print("--- policy_evidence ---")
        print(df.to_string())
    except Exception as e:
        print("policy_evidence error:", e)

    # Check replays
    replays_dir = 'data/replays/'
    if os.path.exists(replays_dir):
        print("\n--- Latest Replays ---")
        replays = sorted(os.listdir(replays_dir), key=lambda x: os.path.getmtime(os.path.join(replays_dir, x)), reverse=True)
        for r in replays[:3]:
            print(f"Checking replay: {r}")
            ledger_path = os.path.join(replays_dir, r, 'ledger.json')
            if os.path.exists(ledger_path):
                with open(ledger_path) as f:
                    ledger = json.load(f)
                    
                for sym in SYMBOLS:
                    signals = [s for s in ledger.get('signals', []) if s.get('symbol') == sym]
                    cands = [c for c in ledger.get('candidates', []) if c.get('symbol') == sym]
                    ml_decisions = [m for m in ledger.get('ml_decisions', []) if m.get('symbol') == sym]
                    print(f"  {sym} - Signals: {len(signals)}, Cands: {len(cands)}, ML Decisions: {len(ml_decisions)}")

if __name__ == '__main__':
    query_db()
