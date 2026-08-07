import sqlite3, json
conn = sqlite3.connect('c:/Users/peddi/Desktop/Bots/Bot_QA/data/alphadesk.db')
c = conn.cursor()
c.execute("SELECT payload FROM trades WHERE payload LIKE '%AAOI%' LIMIT 1")
row = c.fetchone()
if row:
    print(json.dumps(json.loads(row[0]), indent=2))
