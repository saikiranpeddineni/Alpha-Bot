import sqlite3
import json
import pandas as pd
from datetime import datetime, timezone, timedelta

ET = timezone(timedelta(hours=-4))

master_db = r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db"
conn = sqlite3.connect(master_db)

cur = conn.cursor()

# Get all orders on 2026-08-03
cur.execute("SELECT id, ts, mode, payload FROM orders WHERE ts LIKE '2026-08-03%' ORDER BY ts ASC")
order_rows = cur.fetchall()

orders = []
for r in order_rows:
    p = json.loads(r[3]) if r[3] else {}
    orders.append({
        "id": r[0],
        "ts": r[1],
        "mode": r[2],
        "symbol": p.get("symbol"),
        "side": p.get("side"),
        "qty": p.get("qty"),
        "price": p.get("price"),
        "stop_loss": p.get("stop_loss"),
        "take_profit": p.get("take_profit"),
        "strategy": p.get("strategy"),
        "raw": p
    })

# Get all trades on 2026-08-03
cur.execute("SELECT id, symbol, pnl, exit_at, mode, payload FROM trades WHERE exit_at LIKE '2026-08-03%' ORDER BY exit_at ASC")
trade_rows = cur.fetchall()

print(f"Total 2026-08-03 trades: {len(trade_rows)}")
print(f"Total 2026-08-03 orders: {len(orders)}")

trades_detailed = []
for r in trade_rows:
    p = json.loads(r[5]) if r[5] else {}
    sym = r[1]
    entry_at = p.get("entry_at")
    exit_at = p.get("exit_at") or r[3]
    entry_px = p.get("entry_price")
    exit_px = p.get("exit_price")
    qty = p.get("qty")
    side = p.get("side")
    strat = p.get("strategy")
    sl = p.get("initial_stop")
    risk = p.get("initial_risk")
    pnl = r[2]
    reason = p.get("exit_reason")
    
    # Find matching entry order
    # Entry order is right before or at entry_at for the same symbol
    matching_order = None
    if entry_at:
        entry_dt = datetime.fromisoformat(entry_at.replace("Z", "+00:00"))
        # Search orders
        for o in orders:
            if o["symbol"] == sym and (o["side"] in ("buy", "sell")):
                o_dt = datetime.fromisoformat(o["ts"].replace("Z", "+00:00")) if "Z" in o["ts"] else datetime.fromisoformat(o["ts"]).replace(tzinfo=timezone.utc)
                diff = abs((o_dt - entry_dt).total_seconds())
                if diff <= 60: # within 60s
                    matching_order = o
                    break
    
    tp = matching_order.get("take_profit") if matching_order else None
    if tp is None:
        tp = p.get("take_profit")
    
    trades_detailed.append({
        "trade_id": r[0],
        "symbol": sym,
        "strategy": strat,
        "side": side,
        "qty": qty,
        "entry_at_utc": entry_at,
        "entry_at_et": (datetime.fromisoformat(entry_at.replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET")) if entry_at else "N/A",
        "exit_at_utc": exit_at,
        "exit_at_et": (datetime.fromisoformat(exit_at.replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET")) if exit_at else "N/A",
        "entry_price": entry_px,
        "exit_price": exit_px,
        "stop_loss": sl,
        "take_profit": tp,
        "initial_risk": risk,
        "legacy_pnl": pnl,
        "legacy_reason": reason,
        "matching_order": matching_order
    })

for i, t in enumerate(trades_detailed, 1):
    print(f"\n--- Trade #{i}: {t['symbol']} ({t['strategy']}) ---")
    print(f"  Side: {t['side']} | Qty: {t['qty']}")
    print(f"  Entry: {t['entry_price']} @ {t['entry_at_et']} ({t['entry_at_utc']})")
    print(f"  Exit:  {t['exit_price']} @ {t['exit_at_et']} ({t['exit_at_utc']})")
    print(f"  Stop Loss: {t['stop_loss']} | Take Profit: {t['take_profit']} | Initial Risk: {t['initial_risk']}")
    print(f"  Legacy PnL: ${t['legacy_pnl']:.2f} | Reason: {t['legacy_reason']}")

conn.close()
