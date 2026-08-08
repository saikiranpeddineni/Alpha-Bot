import sqlite3
import pandas as pd

master_db = r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db"
conn = sqlite3.connect(master_db)
df = pd.read_sql_query("SELECT DATE(entry_at) as dt, COUNT(1) as trades, SUM(pnl) as pnl FROM trades GROUP BY DATE(entry_at)", conn)
print(df.tail(10))
