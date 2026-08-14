import sqlite3
import json

try:
    conn = sqlite3.connect('backend/data/replay_sessions.db')
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM replay_sessions WHERE replay_id='rpl_7ce7723410e8'").fetchone()
    if row:
        print(dict(row))
    else:
        print("Not found")
except Exception as e:
    print(e)
