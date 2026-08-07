import sqlite3
conn = sqlite3.connect('c:/Users/peddi/Desktop/Bots/Bot_QA/data/bars_cache.db')
c = conn.cursor()
c.execute("SELECT DISTINCT symbol FROM bars WHERE symbol IN ('AAOI', 'DKNG', 'BE', 'NBIS', 'ABNB')")
print(c.fetchall())
