import sqlite3
conn = sqlite3.connect('alpha.db')
cursor = conn.cursor()
cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN ('orders', 'trades', 'policy_rejections', 'shadow_ledger')")
for row in cursor.fetchall():
    print(f"--- {row[0]} ---")
    print(row[1])
