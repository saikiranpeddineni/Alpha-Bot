import sqlite3
import pandas as pd

conn = sqlite3.connect('data/alphadesk.db')
try:
    df = pd.read_sql_query("SELECT * FROM pis_profit_memory WHERE symbol='NBIS'", conn)
    print("--- pis_profit_memory ---")
    print(df.to_string())
except Exception as e:
    print(f"Error: {e}")
conn.close()
