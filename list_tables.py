import sqlite3

master_db_path = r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db"
conn_master = sqlite3.connect(master_db_path)
cur_master = conn_master.cursor()
cur_master.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cur_master.fetchall()
print("Tables in Master DB:")
for t in tables:
    print(t[0])
conn_master.close()
