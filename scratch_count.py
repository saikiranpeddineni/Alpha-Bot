import sqlite3
import pandas as pd
conn = sqlite3.connect('C:/Users/peddi/Desktop/Bots/Bot_QA/data/alphadesk.db')
df = pd.read_sql("SELECT * FROM trades WHERE exit_at LIKE '2026-08-13%' OR exit_at LIKE '2026-08-14%'", conn)
print(f'Trades today: {len(df)}')
df_shadow = pd.read_sql("SELECT * FROM shadow_ledger WHERE ts LIKE '2026-08-13%' OR ts LIKE '2026-08-14%'", conn)
print(f'Shadow trades today: {len(df_shadow)}')
