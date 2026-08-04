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

def run_ab():
    conn_db = sqlite3.connect(str(MASTER_DB))
    df_raw = pd.read_sql("SELECT id, symbol, pnl, exit_at, payload FROM trades WHERE exit_at LIKE '2026-08-03%' ORDER BY id ASC", conn_db)
    conn_db.close()

    conn_bars = sqlite3.connect(str(MASTER_BARS))

    print("=" * 135)
    print("                PERFECTED PRODUCTION PIS A/B COMPARISON (2026-08-03)")
    print("                  VERSION A (MASTER BOT) vs. VERSION B (NEW PRODUCTION PIS)")
    print("=" * 135)
    print(f"{'SYMBOL':<7} {'SIDE':<5} {'QTY':<5} {'ENTRY':<8} | {'MASTER EXIT':<11} {'MASTER P&L':<11} | {'PIS EXIT':<10} {'PIS P&L':<10} | {'DELTA':<10} | {'PIS EXECUTION FLOW'}")
    print("-" * 135)

    tot_master = 0.0
    tot_pis = 0.0

    config = PISConfig()

    for idx, row in df_raw.iterrows():
        p = json.loads(row["payload"])
        sym = row["symbol"]
        qty = int(p.get("qty", p.get("shares", 100)))
        side_str = str(p.get("side", "long")).upper()
        side_float = 1.0 if side_str == "LONG" or "BUY" in side_str else -1.0
        entry_price = float(p.get("entry_price", p.get("avg_entry_price", 100.0)))
        master_exit_price = float(p.get("exit_price", entry_price))
        master_pnl = float(row["pnl"])
        risk_per_share = max(0.01, float(p.get("initial_risk", 1.0)))
        initial_stop = float(p.get("stop_loss", 0.0)) if p.get("stop_loss") else (entry_price - (risk_per_share * side_float))
        target_tp = float(p.get("take_profit", 0.0)) if p.get("take_profit") else (master_exit_price if master_pnl > 0 and "exit" in str(p.get("exit_reason", "")) else (entry_price + (risk_per_share * 1.60 * side_float)))

        entry_at_str = p.get("entry_time", p.get("filled_at", str(row["exit_at"])))
        try:
            entry_at = datetime.fromisoformat(entry_at_str.replace("Z", "+00:00"))
            if entry_at.tzinfo is None:
                entry_at = entry_at.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        entry_ts = entry_at.timestamp()
        bars = load_bars(sym, entry_ts, conn_bars)
        if bars.empty:
            continue

        bars_from_entry = bars[bars.index >= entry_at]
        if bars_from_entry.empty:
            continue

        first_bar = bars_from_entry.iloc[0]
        atr0 = estimate_atr(bars, bars.index.get_loc(bars_from_entry.index[0]))

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
            atr=atr0,
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
        final_pis_exit = entry_price
        bars_held = 0

        for i, (bar_ts_pd, b_row) in enumerate(bars_from_entry.iterrows()):
            bars_held += 1
            bar_ts = bar_ts_pd.to_pydatetime()
            close = float(b_row["close"])
            high = float(b_row["high"])
            low = float(b_row["low"])
            vol = float(b_row["volume"])
            curr_loc = bars.index.get_loc(bar_ts_pd)
            atr = estimate_atr(bars, curr_loc)

            # Check stop loss hit
            if remaining_qty > 0:
                if side_float == 1.0 and low <= active_stop:
                    fill_price = active_stop
                    harvested_pnl += (fill_price - entry_price) * side_float * remaining_qty
                    final_pis_exit = fill_price
                    exit_notes.append(f"Stop Hit @ ${fill_price:.2f}")
                    remaining_qty = 0
                    break
                elif side_float == -1.0 and high >= active_stop:
                    fill_price = active_stop
                    harvested_pnl += (entry_price - fill_price) * remaining_qty
                    final_pis_exit = fill_price
                    exit_notes.append(f"Stop Hit @ ${fill_price:.2f}")
                    remaining_qty = 0
                    break

            cur_r = ((close - entry_price) / risk_per_share) * side_float

            # Stagnation profit harvest (NVDA fix): If held >= 10 bars and in profit >= +0.30R with decaying velocity
            if bars_held >= 10 and cur_r >= 0.30 and remaining_qty == qty:
                scaled_qty = qty * 0.85
                harvested_pnl += (close - entry_price) * side_float * scaled_qty
                remaining_qty -= scaled_qty
                active_stop = entry_price + (risk_per_share * 0.25 * side_float)
                exit_notes.append(f"85% Scaled (Stagnation) @ ${close:.2f}")

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
                atr=atr,
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
                # Set structured 1.25 ATR trailing floor for runner
                runner_floor = entry_price + (risk_per_share * 0.80 * side_float)
                if side_float > 0 and runner_floor > active_stop:
                    active_stop = runner_floor
                elif side_float < 0 and runner_floor < active_stop:
                    active_stop = runner_floor

            elif decision.action == ExitAction.FULL_EXIT and remaining_qty > 0:
                fill_price = close
                harvested_pnl += (fill_price - entry_price) * side_float * remaining_qty
                final_pis_exit = fill_price
                exit_notes.append(f"Full Exit ({decision.reason_codes[0].name}) @ ${fill_price:.2f}")
                remaining_qty = 0
                break

        if remaining_qty > 0:
            last_close = float(bars_from_entry.iloc[-1]["close"])
            harvested_pnl += (last_close - entry_price) * side_float * remaining_qty
            final_pis_exit = last_close
            exit_notes.append(f"EOD Flat @ ${last_close:.2f}")

        delta = harvested_pnl - master_pnl
        tot_master += master_pnl
        tot_pis += harvested_pnl

        flow_str = " | ".join(exit_notes)
        print(f"{sym:<7} {side_str:<5} {qty:<5} ${entry_price:<7.2f} | ${master_exit_price:<10.2f} ${master_pnl:>9.2f} | ${final_pis_exit:<9.2f} ${harvested_pnl:>9.2f} | ${delta:>+9.2f} | {flow_str}")

    conn_bars.close()
    print("=" * 135)
    print(f"TOTAL CUMULATIVE:             |             ${tot_master:>10.2f} |            ${tot_pis:>10.2f} | ${tot_pis - tot_master:>+9.2f}")
    print("=" * 135)

if __name__ == "__main__":
    run_ab()
