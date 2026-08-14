import sqlite3
import pandas as pd
import json

conn = sqlite3.connect('C:/Users/peddi/Desktop/Bots/Bot_QA/data/alphadesk.db')
try:
    df = pd.read_sql("SELECT * FROM shadow_ledger WHERE entry_time LIKE '2026-08-13%' OR entry_time LIKE '2026-08-14%'", conn)
    print(f"Shadow Ledger trades for today: {len(df)}")
except Exception as e:
    print(e)
    
try:
    df_trades = pd.read_sql("SELECT * FROM trades WHERE entry_time LIKE '2026-08-13%' OR entry_time LIKE '2026-08-14%'", conn)
    print(f"Real Trades for today: {len(df_trades)}")
except Exception as e:
    print(e)
