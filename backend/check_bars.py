import sqlite3
conn = sqlite3.connect('c:/Users/peddi/Desktop/Bots/Bot_QA/data/bars_cache.db')
c = conn.cursor()
c.execute("SELECT name FROM sqlite_master WHERE type='table';")
print("Tables:", c.fetchall())

try:
    c.execute("PRAGMA table_info(bars)")
    print("Schema:", c.fetchall())
except:
    pass
