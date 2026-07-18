import sqlite3
import json

db_path = 'data/alphadesk.db'
conn = sqlite3.connect(db_path)
cur = conn.cursor()

try:
    row = cur.execute('SELECT value FROM app_settings WHERE key = "risk_config"').fetchone()
    if row:
        parsed = json.loads(row[0])
        parsed["ml_confidence_threshold"] = 0.52
        cur.execute('UPDATE app_settings SET value = ? WHERE key = "risk_config"', (json.dumps(parsed),))
        print("--> Restored database setting 'risk_config' ml_confidence_threshold to 0.52")
        conn.commit()
except Exception as e:
    print(f"Error: {e}")
finally:
    conn.close()
