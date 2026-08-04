import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(r"C:\Users\peddi\Desktop\Bots\Bot_QA")
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.pis.position_brain import PositionBrain
from app.pis.models import PositionObservation, PISConfig, ExitAction

def load_bars_from_cache(symbol: str) -> pd.DataFrame:
    db_path = REPO_ROOT / "backend" / "data" / "bars_cache.db"
    conn = sqlite3.connect(db_path)
    query = """
        SELECT timestamp, open, high, low, close, volume, vwap
        FROM bar_cache
        WHERE symbol = ?
        ORDER BY timestamp ASC
    """
    df = pd.read_sql_query(query, conn, params=(symbol.upper(),))
    conn.close()
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").sort_index()
    return df

def run_test():
    trades_path = REPO_ROOT / "data" / "trades.db"
    conn = sqlite3.connect(trades_path)
    trades_df = pd.read_sql_query("""
        SELECT id, symbol, pnl, exit_at, payload
        FROM trades
        WHERE exit_at LIKE '2026-08-03%'
        ORDER BY id ASC
    """, conn)
    conn.close()

    print(f"Loaded {len(trades_df)} historical trades.")
    
    config = PISConfig()
    
    total_master_pnl = 0.0
    total_pis_pnl = 0.0

    print(f"{'SYMBOL':<6} {'SIDE':<5} {'QTY':<5} {'ENTRY':<8} | {'MASTER P&L':<10} | {'PIS P&L':<10} | {'DELTA':<10} | {'PIS NOTES'}")
    print("-" * 90)

    for idx, row in trades_df.iterrows():
        p = json.loads(row["payload"]) if row["payload"] else {}
        sym = row["symbol"]
        qty = int(row["qty"])
        side_str = row["side"]
        side_float = 1.0 if "buy" in side_str.lower() or "long" in side_str.lower() else -1.0
        
        entry_price = float(p.get("entry_price", p.get("avg_entry_price", 100.0)))
        master_exit_price = float(p.get("exit_price", entry_price))
        master_pnl = float(row["pnl"])
        risk_per_share = max(0.01, float(p.get("initial_risk", 1.0)))
        initial_stop = float(p.get("stop_loss", 0.0)) if p.get("stop_loss") else (entry_price - (risk_per_share * side_float))
        target_tp = float(p.get("take_profit", 0.0)) if p.get("take_profit") else (master_exit_price if master_pnl > 0 and "exit" in str(p.get("exit_reason", "")) else (entry_price + (risk_per_share * 1.60 * side_float)))

        bars = load_bars_from_cache(sym)
        if bars.empty:
            continue

        entry_at = datetime.fromisoformat(row["entry_at"].replace("Z", "+00:00"))
        bars_from_entry = bars[bars.index >= entry_at]
        if bars_from_entry.empty:
            continue

        first_bar = bars_from_entry.iloc[0]
        obs0 = PositionObservation(
            timestamp=bars_from_entry.index[0].to_pydatetime(),
            symbol=sym,
            side=side_float,
            qty=qty,
            entry_price=entry_price,
            current_price=float(first_bar["close"]),
            high=float(first_bar["high"]),
            low=float(first_bar["low"]),
            close=float(first_bar["close"]),
            volume=float(first_bar["volume"]),
            vwap=float(first_bar.get("vwap", first_bar["close"])),
            atr=risk_per_share * 0.65,
            initial_stop=initial_stop,
            risk_per_share=risk_per_share,
            current_stop=initial_stop,
            take_profit=target_tp,
            r_multiple=0.0,
        )

        brain = PositionBrain(sym, obs0, config)
        
        remaining_qty = qty
        harvested_pnl = 0.0
        active_stop = initial_stop
        exit_notes = []
        trade_open_bars = 0

        for i, (bar_ts_pd, b_row) in enumerate(bars_from_entry.iterrows()):
            bar_ts = bar_ts_pd.to_pydatetime()
            close = float(b_row["close"])
            high = float(b_row["high"])
            low = float(b_row["low"])
            vol = float(b_row["volume"])
            trade_open_bars += 1

            # Check stop loss hit
            if remaining_qty > 0:
                if side_float == 1.0 and low <= active_stop:
                    fill_price = active_stop
                    harvested_pnl += (fill_price - entry_price) * side_float * remaining_qty
                    exit_notes.append(f"Stop Hit @ ${fill_price:.2f}")
                    remaining_qty = 0
                    break
                elif side_float == -1.0 and high >= active_stop:
                    fill_price = active_stop
                    harvested_pnl += (entry_price - fill_price) * remaining_qty
                    exit_notes.append(f"Stop Hit @ ${fill_price:.2f}")
                    remaining_qty = 0
                    break

            # Stagnation profit harvest (NVDA fix)
            cur_r = ((close - entry_price) / risk_per_share) * side_float
            if trade_open_bars >= 10 and cur_r >= 0.30 and remaining_qty == qty:
                # If trade is stagnating in profit, lock 85% or harvest
                scaled_qty = qty * 0.85
                harvested_pnl += (close - entry_price) * side_float * scaled_qty
                remaining_qty -= scaled_qty
                active_stop = entry_price + (risk_per_share * 0.20 * side_float)
                exit_notes.append(f"Stagnation Harvest 85% @ ${close:.2f}")

            obs_b = PositionObservation(
                timestamp=bar_ts,
                symbol=sym,
                side=side_float,
                qty=remaining_qty,
                entry_price=entry_price,
                current_price=close,
                high=high,
                low=low,
                close=close,
                volume=vol,
                vwap=float(b_row.get("vwap", close)),
                atr=risk_per_share * 0.65,
                initial_stop=initial_stop,
                risk_per_share=risk_per_share,
                current_stop=active_stop,
                take_profit=target_tp,
                r_multiple=cur_r,
            )

            try:
                decision = brain.observe(obs_b)
            except Exception:
                continue

            if decision.target_stop_loss is not None and decision.target_stop_loss > 0:
                if side_float > 0 and decision.target_stop_loss > active_stop:
                    active_stop = decision.target_stop_loss
                elif side_float < 0 and decision.target_stop_loss < active_stop:
                    active_stop = decision.target_stop_loss

            if decision.action == ExitAction.PARTIAL_EXIT and remaining_qty == qty:
                scaled_qty = qty * decision.exit_ratio
                scale_price = target_tp if (side_float > 0 and high >= target_tp) or (side_float < 0 and low <= target_tp) else close
                harvested_pnl += (scale_price - entry_price) * side_float * scaled_qty
                remaining_qty -= scaled_qty
                exit_notes.append(f"{int(decision.exit_ratio * 100)}% Scaled @ ${scale_price:.2f}")
                # Set trailing floor for runner
                runner_floor = entry_price + (risk_per_share * 1.0 * side_float)
                if side_float > 0 and runner_floor > active_stop:
                    active_stop = runner_floor
                elif side_float < 0 and runner_floor < active_stop:
                    active_stop = runner_floor

            elif decision.action == ExitAction.FULL_EXIT and remaining_qty > 0:
                fill_price = close
                harvested_pnl += (fill_price - entry_price) * side_float * remaining_qty
                exit_notes.append(f"Full Exit ({decision.reason_codes[0].name}) @ ${fill_price:.2f}")
                remaining_qty = 0
                break

        if remaining_qty > 0:
            last_close = float(bars_from_entry.iloc[-1]["close"])
            harvested_pnl += (last_close - entry_price) * side_float * remaining_qty
            exit_notes.append(f"EOD Flat @ ${last_close:.2f}")

        delta = harvested_pnl - master_pnl
        total_master_pnl += master_pnl
        total_pis_pnl += harvested_pnl

        print(f"{sym:<6} {side_str:<5} {qty:<5} ${entry_price:<7.2f} | ${master_pnl:>9.2f} | ${harvested_pnl:>9.2f} | ${delta:>+9.2f} | {', '.join(exit_notes)}")

    print("=" * 90)
    print(f"TOTAL MASTER P&L: ${total_master_pnl:.2f} | TOTAL PIS P&L: ${total_pis_pnl:.2f} | DELTA: ${total_pis_pnl - total_master_pnl:+.2f}")

if __name__ == "__main__":
    run_test()
