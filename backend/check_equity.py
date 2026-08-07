import sqlite3, json, datetime
conn = sqlite3.connect('c:/Users/peddi/Desktop/Bots/Bot_QA/data/alphadesk.db')
c = conn.cursor()
c.execute('SELECT equity FROM equity_snapshots ORDER BY ts DESC LIMIT 1')
print("Equity:", c.fetchone())
