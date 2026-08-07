import sqlite3, json, datetime

conn = sqlite3.connect('c:/Users/peddi/Desktop/Bots/Bot_QA/data/alphadesk.db')
c = conn.cursor()
yesterday = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=24)).isoformat()
c.execute("SELECT payload, pnl, exit_at FROM trades WHERE exit_at >= ? AND pnl < 0 ORDER BY pnl ASC", (yesterday,))
rows = c.fetchall()

artifact_path = 'C:/Users/peddi/.gemini/antigravity-ide/brain/3dbec354-c949-4690-8a49-0034f7e0a741/all_losers_analysis.md'

with open(artifact_path, 'w') as f:
    f.write("# Detailed Analysis of All 31 Losing Trades\n\n")
    f.write("This document contains a pin-to-pin breakdown of every single losing trade executed by the bot over the last 24 hours.\n\n")
    
    for i, row in enumerate(rows):
        data = json.loads(row[0])
        pnl = row[1]
        
        symbol = data.get("symbol", "UNKNOWN")
        side = data.get("side", "unknown").upper()
        entry_time = data.get("entry_at", "N/A")
        if entry_time != "N/A":
            entry_time = entry_time.split('.')[0] + " UTC"
            
        exit_time = row[2]
        if exit_time:
            exit_time = exit_time.split('.')[0] + " UTC"
            
        strategy = data.get("strategy", "Unknown")
        exit_reason = data.get("exit_reason", "Unknown")
        
        mfe_pct = data.get("mfe_pct", 0.0)
        mae_pct = data.get("mae_pct", 0.0)
        
        behavior = "Instant Reversal"
        if mfe_pct > 0.3:
            behavior = "Went into profit briefly, then reversed"
        elif mfe_pct > 1.0:
            behavior = "Went into deep profit, but failed to hit target and reversed"
            
        f.write(f"### {i+1}. {symbol} ({side}) | PnL: ${pnl:.2f}\n")
        f.write(f"- **Entry Time:** {entry_time}\n")
        f.write(f"- **Exit Time:** {exit_time}\n")
        f.write(f"- **Reason for Entry:** Triggered by `{strategy}` strategy.\n")
        f.write(f"- **Reason for Exit:** `{exit_reason}`\n")
        f.write(f"- **Signal Behavior:** {behavior} (Max Profit seen: {mfe_pct:.2f}%, Max Loss taken: {mae_pct:.2f}%)\n")
        f.write("---\n")
        
print("Report generated.")
