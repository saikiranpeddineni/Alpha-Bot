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

def run_production_benchmark():
    conn_db = sqlite3.connect(str(MASTER_DB))
    df_raw = pd.read_sql("SELECT id, symbol, pnl, exit_at, payload FROM trades ORDER BY exit_at DESC", conn_db)
    conn_db.close()

    conn_bars = sqlite3.connect(str(MASTER_BARS))

    parsed_trades = []
    for _, row in df_raw.iterrows():
        p = json.loads(row["payload"]) if row["payload"] else {}
        entry_at_str = p.get("entry_at") or p.get("filled_at")
        if not entry_at_str or row["pnl"] is None:
            continue
        parsed_trades.append({
            "id": row["id"],
            "symbol": row["symbol"],
            "side": p.get("side", "long").lower(),
            "qty": float(p.get("qty", 100)),
            "entry_price": float(p.get("entry_price", p.get("avg_entry_price", 100.0))),
            "entry_at": entry_at_str,
            "stop_loss": float(p.get("stop_loss", 0.0)),
            "take_profit": float(p.get("take_profit", 0.0)) if p.get("take_profit") else None,
            "initial_risk": float(p.get("initial_risk", 1.0)) if p.get("initial_risk") else 1.0,
            "legacy_pnl": float(row["pnl"]),
            "date": row["exit_at"][:10] if row["exit_at"] else "unknown"
        })

    df_all = pd.DataFrame(parsed_trades)
    target_dates = sorted([d for d, c in df_all["date"].value_counts().items() if c >= 4 and d != "unknown"], reverse=True)

    results = []

    for target_date in target_dates:
        trades_for_day = df_all[df_all["date"] == target_date]
        day_master_pnl = 0.0
        day_prod_pis_pnl = 0.0
        valid_trades = 0

        for _, tr in trades_for_day.iterrows():
            sym = tr["symbol"]
            side_float = 1.0 if tr["side"] == "long" else -1.0
            qty = tr["qty"]
            entry_price = tr["entry_price"]
            try:
                entry_at = datetime.fromisoformat(tr["entry_at"].replace("Z", "+00:00"))
            except Exception:
                continue

            initial_stop = tr["stop_loss"]
            if initial_stop <= 0:
                initial_stop = entry_price - (1.0 * side_float)
            target_tp = tr["take_profit"]
            risk_per_share = max(0.01, tr["initial_risk"])

            bars = load_bars(sym, entry_at.timestamp(), conn_bars)
            if bars.empty:
                continue

            entry_ts_pd = pd.Timestamp(entry_at)
            bars_from_entry = bars[bars.index >= entry_ts_pd]
            if bars_from_entry.empty:
                bars_from_entry = bars[bars.index >= (entry_ts_pd - pd.Timedelta(minutes=1))]
            if bars_from_entry.empty:
                continue

            valid_trades += 1
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

            for i, (bar_ts_pd, row) in enumerate(bars_from_entry.iterrows()):
                bar_ts = bar_ts_pd.to_pydatetime()
                if bar_ts.tzinfo is None: bar_ts = bar_ts.replace(tzinfo=timezone.utc)
                idx_b = i
                atr_b = estimate_atr(bars_from_entry, idx_b)
                close = float(row["close"])
                high = float(row["high"])
                low = float(row["low"])
                r_mul = (close - entry_price) * side_float / risk_per_share

                # Check broker stop
                if i > 0:
                    if side_float == 1.0 and low <= active_stop:
                        full_exit_price = active_stop
                        break
                    elif side_float == -1.0 and high >= active_stop:
                        full_exit_price = active_stop
                        break

                obs_b = PositionObservation(
                    timestamp=bar_ts, symbol=sym, side=side_float, qty=remaining_qty, entry_price=entry_price,
                    current_price=close, high=high, low=low, close=close, volume=float(row["volume"]), atr=atr_b,
                    initial_stop=initial_stop, risk_per_share=risk_per_share, current_stop=active_stop,
                    take_profit=target_tp, r_multiple=r_mul
                )

                decision = brain.observe(obs_b)

                # Update stop floor monotonically
                if decision.target_stop_loss is not None and decision.target_stop_loss > 0:
                    if side_float > 0 and decision.target_stop_loss > active_stop:
                        active_stop = decision.target_stop_loss
                    elif side_float < 0 and decision.target_stop_loss < active_stop:
                        active_stop = decision.target_stop_loss

                if decision.action == ExitAction.PARTIAL_EXIT and remaining_qty == qty:
                    # 70% scale-out executed at target price
                    scaled_qty = qty * decision.exit_ratio
                    scale_price = target_tp if target_tp is not None else close
                    harvested_pnl += (scale_price - entry_price) * side_float * scaled_qty
                    remaining_qty -= scaled_qty
                    # Ratchet stop to Breakeven
                    if side_float > 0 and entry_price > active_stop:
                        active_stop = entry_price
                    elif side_float < 0 and entry_price < active_stop:
                        active_stop = entry_price

                elif decision.action == ExitAction.FULL_EXIT:
                    reasons = [r.value if hasattr(r, "value") else str(r) for r in decision.reason_codes]
                    full_exit_price = close if "PROTECTION_BREACH" not in reasons else active_stop
                    break

            brain.close()
            if full_exit_price is None:
                full_exit_price = float(bars_from_entry.iloc[-1]["close"])

            final_pnl = harvested_pnl + ((full_exit_price - entry_price) * side_float * remaining_qty)
            day_master_pnl += tr["legacy_pnl"]
            day_prod_pis_pnl += final_pnl

        if valid_trades > 0:
            results.append({
                "date": target_date,
                "trades": valid_trades,
                "master_pnl": round(day_master_pnl, 2),
                "prod_pis_pnl": round(day_prod_pis_pnl, 2),
                "delta": round(day_prod_pis_pnl - day_master_pnl, 2)
            })

    conn_bars.close()

    df_res = pd.DataFrame(results)
    print("\n" + "=" * 90)
    print("      PRODUCTION UPGRADED PIS (70/30 SCALE-OUT + STRUCTURAL INTELLIGENCE)")
    print("=" * 90)
    print(f"{'DATE':<12} {'TRADES':<8} | {'MASTER LIVE P&L':<18} | {'PROD PIS P&L':<18} | {'PIS ALPHA'}")
    print("-" * 90)
    for _, r in df_res.iterrows():
        print(f"{r['date']:<12} {int(r['trades']):<8} | ${r['master_pnl']:>15.2f}  | ${r['prod_pis_pnl']:>15.2f}  | {r['delta']:>+15.2f}")
    print("=" * 90)
    tot_m = df_res['master_pnl'].sum()
    tot_p = df_res['prod_pis_pnl'].sum()
    print(f"{'CUMULATIVE TOTAL:':<21} | ${tot_m:>15.2f}  | ${tot_p:>15.2f}  | {tot_p - tot_m:>+15.2f}")
    print("=" * 90)

if __name__ == "__main__":
    run_production_benchmark()
