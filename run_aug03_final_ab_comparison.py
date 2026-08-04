import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(r"C:\Users\peddi\Desktop\Bots\Bot_QA")
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.pis.models import PositionObservation, PISConfig, ExitAction, LifecycleState
from app.pis.position_brain import PositionBrain

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

def estimate_atr(bars: pd.DataFrame, current_idx: int, period: int = 14) -> float:
    if current_idx < 1:
        return 1.0
    start = max(0, current_idx - period)
    subset = bars.iloc[start:current_idx + 1]
    if len(subset) < 2:
        return 1.0
    tr_list = []
    for i in range(1, len(subset)):
        high = subset.iloc[i]["high"]
        low = subset.iloc[i]["low"]
        prev_close = subset.iloc[i-1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
    return float(pd.Series(tr_list).mean()) if tr_list else 1.0

def run_aug03_ab_comparison():
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

        config = PISConfig()
        row0 = bars_from_entry.iloc[0]
        ts0 = bars_from_entry.index[0].to_pydatetime()
        if ts0.tzinfo is None: ts0 = ts0.replace(tzinfo=timezone.utc)
        obs0 = PositionObservation(
            timestamp=ts0, symbol=sym, side=side_float, qty=qty, entry_price=entry_price,
            current_price=float(row0["close"]), high=float(row0["high"]), low=float(row0["low"]), close=float(row0["close"]),
            volume=float(row0["volume"]), atr=1.0, initial_stop=initial_stop, risk_per_share=risk_per_share,
            current_stop=initial_stop, take_profit=target_tp, r_multiple=0.0
        )

        brain = PositionBrain(sym, obs0, config)
        active_stop = initial_stop
        remaining_qty = qty
        harvested_pnl = 0.0
        full_exit_price = None
        exit_notes = []

        for i, (bar_ts_pd, b_row) in enumerate(bars_from_entry.iterrows()):
            bar_ts = bar_ts_pd.to_pydatetime()
            if bar_ts.tzinfo is None: bar_ts = bar_ts.replace(tzinfo=timezone.utc)
            idx_b = i
            atr_b = estimate_atr(bars_from_entry, idx_b)
            close = float(b_row["close"])
            high = float(b_row["high"])
            low = float(b_row["low"])
            r_mul = (close - entry_price) * side_float / risk_per_share

            # Check stop loss hit
            if i > 0:
                if side_float == 1.0 and low <= active_stop:
                    full_exit_price = active_stop
                    exit_notes.append(f"Stop Hit @ ${active_stop:.2f}")
                    break
                elif side_float == -1.0 and high >= active_stop:
                    full_exit_price = active_stop
                    exit_notes.append(f"Stop Hit @ ${active_stop:.2f}")
                    break

            obs_b = PositionObservation(
                timestamp=bar_ts, symbol=sym, side=side_float, qty=remaining_qty, entry_price=entry_price,
                current_price=close, high=high, low=low, close=close, volume=float(b_row["volume"]), atr=atr_b,
                initial_stop=initial_stop, risk_per_share=risk_per_share, current_stop=active_stop,
                take_profit=target_tp, r_multiple=r_mul
            )

            decision = brain.observe(obs_b)

            # Monotonic stop floor ratchet
            if decision.target_stop_loss is not None and decision.target_stop_loss > 0:
                if side_float > 0 and decision.target_stop_loss > active_stop:
                    active_stop = decision.target_stop_loss
                elif side_float < 0 and decision.target_stop_loss < active_stop:
                    active_stop = decision.target_stop_loss

            if decision.action == ExitAction.PARTIAL_EXIT and remaining_qty == qty:
                scaled_qty = qty * decision.exit_ratio
                scale_price = target_tp if target_tp is not None else close
                harvested_pnl += (scale_price - entry_price) * side_float * scaled_qty
                remaining_qty -= scaled_qty
                exit_notes.append(f"70% Scaled @ ${scale_price:.2f}")
                # Ratchet stop to Breakeven
                if side_float > 0 and entry_price > active_stop:
                    active_stop = entry_price
                elif side_float < 0 and entry_price < active_stop:
                    active_stop = entry_price

            elif decision.action == ExitAction.FULL_EXIT:
                reasons = [r.value if hasattr(r, "value") else str(r) for r in decision.reason_codes]
                full_exit_price = close if "PROTECTION_BREACH" not in reasons else active_stop
                exit_notes.append(f"Full Exit ({reasons[0] if reasons else 'SIGNAL'}) @ ${full_exit_price:.2f}")
                break

        brain.close()
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
    print("\n" + "=" * 130)
    print("                      A/B COMPARISON: YESTERDAY'S TRADES (2026-08-03)")
    print("                  VERSION A (MASTER BOT) vs. VERSION B (UPGRADED PIS)")
    print("=" * 130)
    
    header = f"{'SYMBOL':<7} {'SIDE':<5} {'QTY':<5} {'ENTRY':<8} | {'MASTER EXIT':<11} {'MASTER P&L':<11} | {'PIS EXIT':<10} {'PIS P&L':<10} | {'DELTA':<10} | {'PIS EXECUTION FLOW'}"
    print(header)
    print("-" * 130)
    for _, r in df_comp.iterrows():
        pnl_m_str = f"${r['Master P&L']:>8.2f}"
        pnl_p_str = f"${r['PIS P&L']:>8.2f}"
        delta_str = f"{r['Delta']:>+8.2f}"
        print(f"{r['Symbol']:<7} {r['Side']:<5} {r['Qty']:<5} {r['Entry']:<8} | {r['Master Exit']:<11} {pnl_m_str:<11} | {r['PIS Exit']:<10} {pnl_p_str:<10} | {delta_str:<10} | {r['PIS Execution Flow']}")
    print("=" * 130)
    tot_m = df_comp["Master P&L"].sum()
    tot_p = df_comp["PIS P&L"].sum()
    tot_d = tot_p - tot_m
    print(f"{'TOTAL CUMULATIVE:':<29} | {' ':11} ${tot_m:>8.2f} | {' ':10} ${tot_p:>8.2f} | {tot_d:>+8.2f}")
    print("=" * 130)

if __name__ == "__main__":
    run_aug03_ab_comparison()
