import sqlite3

qa_db_path = r"C:\Users\peddi\Desktop\Bots\Bot_QA\data\alphadesk.db"

def wipe_trades():
    print(f"Connecting to QA DB: {qa_db_path}")
    conn = sqlite3.connect(qa_db_path)
    cur = conn.cursor()
    
    print("Wiping 'trades' table...")
    cur.execute("DELETE FROM trades")
    print(f"Deleted {cur.rowcount} rows from trades.")
    
    print("Wiping 'orders' table...")
    cur.execute("DELETE FROM orders")
    print(f"Deleted {cur.rowcount} rows from orders.")
    
    conn.commit()
    conn.close()
    print("Successfully wiped trades and orders, leaving bot intelligence intact!")

if __name__ == "__main__":
    wipe_trades()
