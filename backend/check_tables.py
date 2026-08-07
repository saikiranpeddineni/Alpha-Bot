import sqlite3
conn = sqlite3.connect('c:/Users/peddi/Desktop/Bots/Bot_QA/backend/data/trades.db')
print(conn.cursor().execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall())
