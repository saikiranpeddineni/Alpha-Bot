import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(r"C:\Users\peddi\Desktop\Bots\Bot_QA")
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

MASTER_DB = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db")
MASTER_BARS = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db")

def load_bars(symbol: str, entry_ts: float, conn: sqlite3.Connection) -> pd.DataFrame:
    start_ts = entry_ts - 7200
    end_ts = entry_ts + 28800
    query = """
    SELECT ts, open, high, low, close, volume
    FROM bars
    WHERE symbol = ? AND ts >= ? AND ts <= ?
    ORDER BY ts ASC
    """
    df = pd.read_sql(query, conn, params=(symbol, start_ts, end_ts))
    if df.empty:
        return df
    df["datetime"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df.drop_duplicates(subset=["datetime"], keep="first", inplace=True)
    df.set_index("datetime", inplace=True)
    return df

def run_climax_test():
    conn_db = sqlite3.connect(str(MASTER_DB))
    df_raw = pd.read_sql("SELECT id, symbol, pnl, exit_at, payload FROM trades WHERE exit_at LIKE '2026-08-03%' ORDER BY id ASC", conn_db)
    conn_db.close()

    conn_bars = sqlite3.connect(str(MASTER_BARS))

    results = []

    for _, row in df_raw.iterrows():
        p = json.loads(row["payload"]) if row["payload"] else {}
        entry_at_str = p.get("entry_at") or p.get("filled_at")
        if not entry_at_str or row["pnl"] is None:
            continue

        sym = row["symbol"]
        side_str = p.get("side", "long").lower()
        side_float = 1.0 if side_str == "long" else -1.0
        qty = float(p.get("qty", 100))
        entry_price = float(p.get("entry_price", p.get("avg_entry_price", 100.0)))
        master_exit_price = float(p.get("exit_price", entry_price))
        master_pnl = float(row["pnl"])
        risk_per_share = max(0.01, float(p.get("initial_risk", 1.0)))
        initial_stop = float(p.get("stop_loss", 0.0)) if p.get("stop_loss") else (entry_price - (risk_per_share * side_float))
        target_tp = float(p.get("take_profit", 0.0)) if p.get("take_profit") else (entry_price + (risk_per_share * 1.60 * side_float))

        try:
            entry_at = datetime.fromisoformat(entry_at_str.replace("Z", "+00:00"))
        except Exception:
            continue

        bars = load_bars(sym, entry_at.timestamp(), conn_bars)
        if bars.empty:
            continue

        entry_ts_pd = pd.Timestamp(entry_at)
        bars_from_entry = bars[bars.index >= entry_ts_pd]
        if bars_from_entry.empty:
            bars_from_entry = bars[bars.index >= (entry_ts_pd - pd.Timedelta(minutes=1))]
        if bars_from_entry.empty:
            continue

        peak_r = 0.0
        trough_r = 0.0
        active_stop = initial_stop
        remaining_qty = qty
        harvested_pnl = 0.0
        full_exit_price = None
        exit_notes = []
        is_runner = False
        scaled = False
        prev_low = None
        prev_high = None

        for i, (bar_ts_pd, b_row) in enumerate(bars_from_entry.iterrows()):
            close = float(b_row["close"])
            high = float(b_row["high"])
            low = float(b_row["low"])
            r_mul = (close - entry_price) * side_float / risk_per_share
            cur_peak = (high - entry_price) / risk_per_share if side_float > 0 else (entry_price - low) / risk_per_share
            peak_r = max(peak_r, cur_peak)

            # Check stop hit
            if i > 0:
                if side_float == 1.0 and low <= active_stop:
                    full_exit_price = active_stop
                    exit_notes.append(f"Stop Hit @ ${active_stop:.2f}")
                    break
                elif side_float == -1.0 and high >= active_stop:
                    full_exit_price = active_stop
                    exit_notes.append(f"Stop Hit @ ${active_stop:.2f}")
                    break

            # 1. Early Adverse Invalidation
            if r_mul <= -0.40 and peak_r < 0.50 and i >= 2:
                full_exit_price = close
                exit_notes.append(f"Adverse Thesis Invalidation @ ${close:.2f} ({r_mul:.2f}R)")
                break

            # 2. Target / Exhaustion Wick Scale-Out
            bar_range = high - low
            upper_wick = (high - close) if side_float > 0 else (close - low)
            wick_ratio = upper_wick / max(1e-6, bar_range)
            target_hit = (side_float > 0 and high >= target_tp) or (side_float < 0 and low <= target_tp)
            exhaustion = (peak_r >= 1.20 and wick_ratio >= 0.45 and bar_range >= 0.50)

            if (target_hit or exhaustion) and not scaled:
                scale_price = target_tp if target_hit else close
                scaled_qty = qty * 0.70
                harvested_pnl += (scale_price - entry_price) * side_float * scaled_qty
                remaining_qty -= scaled_qty
                scaled = True
                is_runner = True
                exit_notes.append(f"70% Scaled @ ${scale_price:.2f}")
                # Move floor to Breakeven
                if side_float > 0 and entry_price > active_stop:
                    active_stop = entry_price
                elif side_float < 0 and entry_price < active_stop:
                    active_stop = entry_price

            # 3. Intelligent Climax Runner Protection:
            # If runner is in deep profit (>= 1.5R), trail candle-by-candle (previous bar low/high)
            # to prevent sharp blow-off reversals from giving back profit!
            if is_runner and peak_r >= 1.50 and i > 0:
                if side_float > 0 and prev_low is not None:
                    # Ratchet stop to previous bar low if higher than current active_stop
                    if prev_low > active_stop:
                        active_stop = prev_low
                elif side_float < 0 and prev_high is not None:
                    if prev_high < active_stop:
                        active_stop = prev_high

            prev_low = low
            prev_high = high

        if full_exit_price is None:
            full_exit_price = float(bars_from_entry.iloc[-1]["close"])
            exit_notes.append(f"EOD Close @ ${full_exit_price:.2f}")

        pis_pnl = harvested_pnl + ((full_exit_price - entry_price) * side_float * remaining_qty)
        delta = pis_pnl - master_pnl

        results.append({
            "Symbol": sym,
            "Side": side_str.upper(),
            "Qty": int(qty),
            "Entry": f"${entry_price:.2f}",
            "Master Exit": f"${master_exit_price:.2f}",
            "Master P&L": round(master_pnl, 2),
            "PIS Exit": f"${full_exit_price:.2f}",
            "PIS P&L": round(pis_pnl, 2),
            "Delta": round(delta, 2),
            "PIS Execution Flow": " | ".join(exit_notes)
        })

    conn_bars.close()

    df_comp = pd.DataFrame(results)
    print("\n" + "=" * 135)
    print("             INTELLIGENT CLIMAX RUNNER PIS (CANDLE-BY-CANDLE TRAILING AT PEAK)")
    print("=" * 135)
    header = f"{'SYMBOL':<7} {'SIDE':<5} {'QTY':<5} {'ENTRY':<8} | {'MASTER EXIT':<11} {'MASTER P&L':<11} | {'PIS EXIT':<10} {'PIS P&L':<10} | {'DELTA':<10} | {'PIS EXECUTION FLOW'}"
    print(header)
    print("-" * 135)
    for _, r in df_comp.iterrows():
        pnl_m_str = f"${r['Master P&L']:>8.2f}"
        pnl_p_str = f"${r['PIS P&L']:>8.2f}"
        delta_str = f"{r['Delta']:>+8.2f}"
        print(f"{r['Symbol']:<7} {r['Side']:<5} {r['Qty']:<5} {r['Entry']:<8} | {r['Master Exit']:<11} {pnl_m_str:<11} | {r['PIS Exit']:<10} {pnl_p_str:<10} | {delta_str:<10} | {r['PIS Execution Flow']}")
    print("=" * 135)
    tot_m = df_comp["Master P&L"].sum()
    tot_p = df_comp["PIS P&L"].sum()
    tot_d = tot_p - tot_m
    print(f"{'TOTAL CUMULATIVE:':<29} | {' ':11} ${tot_m:>8.2f} | {' ':10} ${tot_p:>8.2f} | {tot_d:>+8.2f}")
    print("=" * 135)

if __name__ == "__main__":
    run_climax_test()
