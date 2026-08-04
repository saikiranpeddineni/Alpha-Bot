import sqlite3
import pandas as pd
import json

qa_db = r'C:\Users\peddi\Desktop\Bots\Bot_QA\data\alphadesk.db'
master_db = r'C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db'
output_file = r'C:\Users\peddi\.gemini\antigravity-ide\brain\2a7f2d36-8d9c-4389-9e9e-bcf280018d1f\bot_performance_analysis.md'

def get_trades_df(db_path):
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql("SELECT * FROM trades", conn)
        if df.empty:
            return pd.DataFrame()
        
        # Parse payload
        def parse_payload(p):
            try:
                return json.loads(p)
            except:
                return {}
        
        parsed = df['payload'].apply(parse_payload).apply(pd.Series)
        for col in parsed.columns:
            df[col] = parsed[col]
            
        return df
    except Exception as e:
        print(f"Error reading {db_path}: {e}")
        return pd.DataFrame()
    finally:
        conn.close()

qa_trades = get_trades_df(qa_db)
master_trades = get_trades_df(master_db)

with open(output_file, 'w', encoding='utf-8') as f:
    f.write("# 🤖 Bot Performance & Trade Analysis\n\n")
    
    def write_analysis(bot_name, df):
        f.write(f"## {bot_name}\n\n")
        if df.empty:
            f.write("> [!WARNING]\n> No trades found for this bot today.\n\n")
            return
            
        # Ensure numerical columns
        df['pnl'] = pd.to_numeric(df.get('pnl', 0), errors='coerce').fillna(0)
        df['pnl_pct'] = pd.to_numeric(df.get('pnl_pct', 0), errors='coerce').fillna(0)
        
        total_trades = len(df)
        wins = len(df[df['pnl'] > 0])
        win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0
        total_pnl = df['pnl'].sum()
        
        best = df.loc[df['pnl'].idxmax()] if df['pnl'].max() > 0 else None
        worst = df.loc[df['pnl'].idxmin()] if df['pnl'].min() < 0 else None
        
        f.write(f"**Total Trades**: {total_trades} | **Win Rate**: {win_rate:.1f}% | **Net P&L**: ${total_pnl:.2f}\n\n")
        
        if best is not None:
            f.write(f"- **Best Trade**: {best.get('symbol', 'N/A')} ({best.get('strategy', 'N/A')}) -> +${best['pnl']:.2f}\n")
        if worst is not None:
            f.write(f"- **Worst Trade**: {worst.get('symbol', 'N/A')} ({worst.get('strategy', 'N/A')}) -> ${worst['pnl']:.2f}\n")
            
        f.write("\n### All Trades\n\n")
        f.write("| Exit Time | Symbol | Side | Qty | Entry | Exit | P&L | P&L % | Strategy | Exit Reason |\n")
        f.write("|---|---|---|---|---|---|---|---|---|---|\n")
        
        for _, row in df.sort_values(by='exit_at', ascending=False).iterrows():
            exit_time = str(row.get('exit_at', ''))[:19]
            sym = row.get('symbol', '')
            side = row.get('side', '')
            qty = row.get('qty', 0)
            entry = row.get('entry_price', 0)
            exit_p = row.get('exit_price', 0)
            pnl = row.get('pnl', 0)
            pnl_pct = row.get('pnl_pct', 0) * 100
            strat = row.get('strategy', '')
            reason = row.get('exit_reason', '')
            
            f.write(f"| {exit_time} | **{sym}** | {side} | {qty} | ${entry:.2f} | ${exit_p:.2f} | **${pnl:.2f}** | {pnl_pct:.2f}% | {strat} | {reason} |\n")
            
        f.write("\n---\n\n")

    write_analysis("Alpha-Bot Master", master_trades)
    write_analysis("Bot_QA (Fresh DB)", qa_trades)

print("Artifact written successfully.")
