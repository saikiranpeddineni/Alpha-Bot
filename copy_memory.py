import sqlite3

master_db_path = r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db"
qa_db_path = r"C:\Users\peddi\Desktop\Bots\Bot_QA\data\alphadesk.db"

def copy_tables():
    print(f"Connecting to Master DB: {master_db_path}")
    conn_master = sqlite3.connect(master_db_path)
    
    print(f"Connecting to QA DB: {qa_db_path}")
    conn_qa = sqlite3.connect(qa_db_path)
    
    tables_to_copy = ["orders", "trades", "contextual_performance_memory"]
    
    for table in tables_to_copy:
        print(f"\nProcessing table: {table}")
        
        cur_master = conn_master.cursor()
        cur_master.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
        if not cur_master.fetchone():
            print(f"Table {table} DOES NOT EXIST in Master DB! Skipping.")
            continue
            
        cur_master.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table}'")
        schema = cur_master.fetchone()[0]
        
        cur_qa = conn_qa.cursor()
        cur_qa.execute(f"DROP TABLE IF EXISTS {table}")
        cur_qa.execute(schema)
        
        cur_master.execute(f"SELECT * FROM {table}")
        rows = cur_master.fetchall()
        
        if not rows:
            print(f"No rows found in {table} in Master DB.")
            continue
            
        placeholders = ",".join(["?"] * len(rows[0]))
        cur_qa.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
        conn_qa.commit()
        
        print(f"Successfully copied {len(rows)} rows into {table} in QA DB.")
        
    conn_master.close()
    conn_qa.close()
    print("\nCopy complete!")

if __name__ == "__main__":
    copy_tables()
