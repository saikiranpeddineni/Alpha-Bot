import sqlite3, json
conn = sqlite3.connect('c:/Users/peddi/Desktop/Bots/Bot_QA/data/alphadesk.db')
val = conn.cursor().execute("SELECT value FROM app_settings WHERE key='strategies'").fetchone()
print(json.dumps(json.loads(val[0]), indent=2) if val else 'No strategies setting found')
