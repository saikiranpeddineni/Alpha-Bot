import sqlite3
import pandas as pd

conn = sqlite3.connect('data/decision_traces.db')
try:
    df = pd.read_sql_query('SELECT * FROM decision_traces WHERE symbol="SKHY" ORDER BY id DESC LIMIT 1', conn)
    for i in range(len(df)):
        print(df.iloc[i].to_dict())
except Exception as e:
    print(e)
