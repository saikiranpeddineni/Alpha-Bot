import sqlite3

conn = sqlite3.connect(r"C:\Users\peddi\Desktop\Bots\Bot_QA\data\alphadesk.old.db")
cur = conn.cursor()
cur.execute("PRAGMA table_info(trades)")
print(cur.fetchall())
conn.close()
