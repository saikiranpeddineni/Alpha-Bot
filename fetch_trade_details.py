import sqlite3
import pandas as pd

master_db_path = r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db"

def inspect_trades():
    conn = sqlite3.connect(master_db_path)
    cur = conn.cursor()
    
    # Get column names
    cur.execute("PRAGMA table_info(trades)")
    columns = [col[1] for col in cur.fetchall()]
    print("Trades columns:", columns)
    
    # Fetch all records for INTC and IREN
    query = "SELECT * FROM trades WHERE symbol IN ('INTC', 'IREN')"
    df = pd.read_sql_query(query, conn)
    print("\nTrades matching INTC and IREN:")
    print(df.to_string())
    conn.close()

if __name__ == "__main__":
    inspect_trades()
