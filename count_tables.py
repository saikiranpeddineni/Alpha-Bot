import sqlite3

qa_db_path = r"C:\Users\peddi\Desktop\Bots\Bot_QA\data\alphadesk.db"

def count_tables():
    conn = sqlite3.connect(qa_db_path)
    cur = conn.cursor()
    
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = cur.fetchall()
    
    print("Row counts in QA DB:")
    for (table,) in tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            count = cur.fetchone()[0]
            print(f"{table}: {count} rows")
        except Exception as e:
            print(f"{table}: ERROR ({e})")
            
    conn.close()

if __name__ == "__main__":
    count_tables()
