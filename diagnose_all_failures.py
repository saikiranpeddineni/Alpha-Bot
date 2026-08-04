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

def estimate_atr(df_bars: pd.DataFrame, current_idx: int, period: int = 14) -> float:
    start_i = max(0, current_idx - period)
    sub = df_bars.iloc[start_i : current_idx + 1]
    if len(sub) < 2:
        r = sub.iloc[-1]["high"] - sub.iloc[-1]["low"]
        return max(0.01, float(r))
    tr_list = []
    for i in range(1, len(sub)):
        h = sub.iloc[i]["high"]
        l = sub.iloc[i]["low"]
        pc = sub.iloc[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_list.append(tr)
    return max(0.01, float(sum(tr_list) / len(tr_list)))

def diagnose():
    conn_db = sqlite3.connect(str(MASTER_DB))
    df_raw = pd.read_sql("SELECT id, symbol, pnl, exit_at, payload FROM trades WHERE exit_at LIKE '2026-08-03%' ORDER BY id ASC", conn_db)
    conn_db.close()

    conn_bars = sqlite3.connect(str(MASTER_BARS))

    underperformed = ["CRWV", "NBIS", "INTC", "NVDA", "BABA"]

    for _, row in df_raw.iterrows():
        sym = row["symbol"]
        if sym not in underperformed:
            continue

        p = json.loads(row["payload"]) if row["payload"] else {}
        entry_at_str = p.get("entry_at") or p.get("filled_at")
        if not entry_at_str or row["pnl"] is None:
            continue

        side_str = p.get("side", "long").lower()
        side_float = 1.0 if side_str == "long" else -1.0
        qty = float(p.get("qty", 100))
        entry_price = float(p.get("entry_price", p.get("avg_entry_price", 100.0)))
        master_exit_price = float(p.get("exit_price", entry_price))
        master_pnl = float(row["pnl"])
        risk_per_share = max(0.01, float(p.get("initial_risk", 1.0)))
        initial_stop = float(p.get("stop_loss", 0.0)) if p.get("stop_loss") else (entry_price - (risk_per_share * side_float))
        target_tp = float(p.get("take_profit", 0.0)) if p.get("take_profit") else (entry_price + (risk_per_share * 1.60 * side_float))

        entry_at = datetime.fromisoformat(entry_at_str.replace("Z", "+00:00"))
        bars = load_bars(sym, entry_at.timestamp(), conn_bars)
        if bars.empty:
            continue

        entry_ts_pd = pd.Timestamp(entry_at)
        bars_from_entry = bars[bars.index >= entry_ts_pd]
        if bars_from_entry.empty:
            bars_from_entry = bars[bars.index >= (entry_ts_pd - pd.Timedelta(minutes=1))]
        if bars_from_entry.empty:
            continue

        print("\n" + "=" * 110)
        print(f"DIAGNOSING {sym} ({side_str.upper()} {qty} shares @ ${entry_price:.2f}) | Master Exit: ${master_exit_price:.2f} (P&L: ${master_pnl:+.2f})")
        print("=" * 110)
        print(f"{'BAR':<4} {'TIME':<8} {'OPEN':<8} {'HIGH':<8} {'LOW':<8} {'CLOSE':<8} {'R-MULT':<7} {'HEALTH':<7} {'LIFECYCLE':<13} {'ACTION':<15} {'STOP':<8} {'REASONS'}")
        print("-" * 110)

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

        for i, (bar_ts_pd, b_row) in enumerate(bars_from_entry.iterrows()):
            bar_ts = bar_ts_pd.to_pydatetime()
            if bar_ts.tzinfo is None: bar_ts = bar_ts.replace(tzinfo=timezone.utc)
            atr_b = estimate_atr(bars_from_entry, i)
            close = float(b_row["close"])
            high = float(b_row["high"])
            low = float(b_row["low"])
            r_mul = (close - entry_price) * side_float / risk_per_share

            # Check stop hit
            stop_hit = False
            if i > 0:
                if side_float == 1.0 and low <= active_stop:
                    stop_hit = True
                elif side_float == -1.0 and high >= active_stop:
                    stop_hit = True

            obs_b = PositionObservation(
                timestamp=bar_ts, symbol=sym, side=side_float, qty=remaining_qty, entry_price=entry_price,
                current_price=close, high=high, low=low, close=close, volume=float(b_row["volume"]), atr=atr_b,
                initial_stop=initial_stop, risk_per_share=risk_per_share, current_stop=active_stop,
                take_profit=target_tp, r_multiple=r_mul
            )

            decision = brain.observe(obs_b)

            if decision.target_stop_loss is not None and decision.target_stop_loss > 0:
                if side_float > 0 and decision.target_stop_loss > active_stop:
                    active_stop = decision.target_stop_loss
                elif side_float < 0 and decision.target_stop_loss < active_stop:
                    active_stop = decision.target_stop_loss

            reasons_str = ",".join([r.name if hasattr(r, "name") else str(r) for r in decision.reason_codes])
            lc_str = brain.lifecycle.name if hasattr(brain.lifecycle, "name") else str(brain.lifecycle)
            time_str = bar_ts.strftime("%H:%M")

            health_val = brain.health.overall if hasattr(brain, "health") and hasattr(brain.health, "overall") else 0.0
            print(f"{i:<4} {time_str:<8} ${float(b_row['open']):<7.2f} ${high:<7.2f} ${low:<7.2f} ${close:<7.2f} {r_mul:>+6.2f}R {health_val:<7.2f} {lc_str:<13} {decision.action.name:<15} ${active_stop:<7.2f} {reasons_str}")

            if stop_hit:
                print(f"      >>> [STOP HIT DETECTED AT BAR {i}] Price low ${low:.2f} <= Stop ${active_stop:.2f}")
                break

            if decision.action == ExitAction.FULL_EXIT:
                print(f"      >>> [FULL EXIT TRIGGERED AT BAR {i}] Action: {decision.action.name}")
                break
            elif decision.action == ExitAction.PARTIAL_EXIT and remaining_qty == qty:
                scaled_qty = qty * decision.exit_ratio
                remaining_qty -= scaled_qty
                print(f"      >>> [PARTIAL EXIT EXECUTED AT BAR {i}] Sold {scaled_qty} shares ({int(decision.exit_ratio*100)}%), {remaining_qty} remaining")

            if i >= 15:
                break

        brain.close()

    conn_bars.close()

if __name__ == "__main__":
    diagnose()
