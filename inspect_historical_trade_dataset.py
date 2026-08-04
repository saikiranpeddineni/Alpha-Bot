import json
import sqlite3
import pandas as pd
from pathlib import Path

MASTER_DB = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db")
QA_DB = Path(r"C:\Users\peddi\Desktop\Bots\Bot_QA\data\alphadesk.db")

for name, db_path in [("MASTER DB", MASTER_DB), ("QA DB", QA_DB)]:
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        print(f"=== {name} ({db_path}) ===")
        df_trades = pd.read_sql("SELECT * FROM trades ORDER BY exit_at DESC", conn)
        print(f"Total trades count: {len(df_trades)}")
        if not df_trades.empty:
            df_trades["date"] = df_trades["exit_at"].str.slice(0, 10)
            print("Date distribution:")
            print(df_trades["date"].value_counts())
        conn.close()
