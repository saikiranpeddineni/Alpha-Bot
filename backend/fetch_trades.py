import sqlite3
import pandas as pd
import json

qa_db = r'C:\Users\peddi\Desktop\Bots\Bot_QA\data\alphadesk.db'
master_db = r'C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db'

def list_tables(db_path):
    conn = sqlite3.connect(db_path)
    result = {}
    try:
        tables = pd.read_sql("SELECT name FROM sqlite_master WHERE type='table'", conn)
        result['tables'] = list(tables['name'].values)
        for t in result['tables']:
            df = pd.read_sql(f"PRAGMA table_info({t})", conn)
            result[t] = list(df['name'].values)
    except Exception as e:
        result['error'] = str(e)
    finally:
        conn.close()
    return result

print(json.dumps({"QA": list_tables(qa_db), "Master": list_tables(master_db)}, indent=2))
