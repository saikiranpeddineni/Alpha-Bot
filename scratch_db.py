import sqlite3
import pandas as pd
conn = sqlite3.connect('C:/Users/peddi/Desktop/Bots/Bot_QA/data/alphadesk.db')
print('Tables:', pd.read_sql("SELECT name FROM sqlite_master WHERE type='table';", conn)['name'].tolist())
try:
    df = pd.read_sql('SELECT * FROM shadow_ledger', conn)
    print(f'shadow_ledger rows: {len(df)}')
except Exception as e:
    print(e)
try:
    df2 = pd.read_sql('SELECT * FROM trades', conn)
    print(f'trades rows: {len(df2)}')
except Exception as e:
    print(e)
try:
    df3 = pd.read_sql('SELECT * FROM orders', conn)
    print(f'orders rows: {len(df3)}')
except Exception as e:
    print(e)
