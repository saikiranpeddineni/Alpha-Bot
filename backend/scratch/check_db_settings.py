import sqlite3
import json

db_path = 'data/alphadesk.db'
conn = sqlite3.connect(db_path)
cur = conn.cursor()

try:
    print("==================================================")
    print(" DATABASE SETTINGS")
    print("==================================================")
    rows = cur.execute('SELECT key, value FROM app_settings').fetchall()
    for r in rows:
        key, val = r
        print(f"Key: {key}")
        try:
            parsed = json.loads(val)
            print(json.dumps(parsed, indent=2))
            
            # If this is the risk config, update ml_confidence_threshold to 0.48
            if key == "risk_config" or "ml_confidence_threshold" in parsed:
                parsed["ml_confidence_threshold"] = 0.48
                cur.execute('UPDATE app_settings SET value = ? WHERE key = ?', (json.dumps(parsed), key))
                print(f"--> Updated database setting '{key}' ml_confidence_threshold to 0.48")
        except Exception:
            print(val)
        print("--------------------------------------------------")
    conn.commit()
except Exception as e:
    print(f"Error: {e}")
finally:
    conn.close()
