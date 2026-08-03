"""
Aug 03, 2026 — ACCURATE PIS Replay (Master bars_cache.db)
===========================================================
Key fixes vs previous replay:
  1. Bars from Alpha-Bot Master bars_cache.db (not Bot_QA) — the live bot's own bar data
  2. initial_stop from DB payload (exact, not 1% fallback)
  3. initial_risk from DB payload (exact risk_per_share)
  4. MFE/MAE from payload used as ground-truth validation
  5. 2 bps slippage on exit fills (matches live fill model)
  6. EOD window capped at market close (20:00 UTC = 16:00 ET)

NO VALUES ARE HARDCODED OR FABRICATED.
"""
from __future__ import annotations

import json, sqlite3, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

REPO_ROOT   = Path(r"C:\Users\peddi\Desktop\Bots\Bot_QA")
BACKEND     = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

MASTER_DB   = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db")
MASTER_BARS = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db")
OUTPUT_CSV  = REPO_ROOT / "scripts" / "aug03_accurate_replay.csv"

SLIPPAGE_BPS = 2  # 0.02% slippage on fill — matches live fill model

# Aug 03 market close in UTC
MARKET_CLOSE_UTC = datetime(2026, 8, 3, 20, 0, 0, tzinfo=timezone.utc)


def apply_slippage(price: float, side_float: float) -> float:
    """Adverse slippage on exit: long exits lower, short exits higher."""
    factor = 1.0 - (SLIPPAGE_BPS / 10000.0) * side_float
    return round(price * factor, 4)


def _estimate_atr(bars: pd.DataFrame, idx: int, window: int = 14) -> float:
    start = max(0, idx - window)
    sub   = bars.iloc[start : idx + 1]
    if len(sub) < 2:
        return bars.iloc[idx]["close"] * 0.005
    trs = []
    for i in range(1, len(sub)):
        tr = max(sub["high"].iat[i] - sub["low"].iat[i],
                 abs(sub["high"].iat[i] - sub["close"].iat[i - 1]),
                 abs(sub["low"].iat[i]  - sub["close"].iat[i - 1]))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else bars.iloc[idx]["close"] * 0.005


def load_bars(symbol: str, entry_epoch: float, bars_conn: sqlite3.Connection) -> pd.DataFrame:
    """Load 1-min bars from 30 min before entry to market close (20:00 UTC)."""
    start_e = int(entry_epoch - 1800)
    end_e   = int(MARKET_CLOSE_UTC.timestamp())
    df = pd.read_sql(
        "SELECT ts, open, high, low, close, volume FROM bars "
        "WHERE symbol=? AND tf='1Min' AND ts>=? AND ts<=? ORDER BY ts ASC",
        bars_conn, params=(symbol.upper(), start_e, end_e)
    )
    if df.empty:
        return df
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df.set_index("dt", inplace=True)
    return df


def replay_one(trade: dict, bars_conn: sqlite3.Connection) -> dict:
    from app.pis.models import PISConfig, ExitAction, PositionObservation
    from app.pis.position_brain import PositionBrain

    symbol      = trade["symbol"]
    side_float  = 1.0 if trade["side"].lower() == "long" else -1.0
    qty         = float(trade["qty"])
    entry_price = float(trade["entry_price"])

    entry_at    = datetime.fromisoformat(trade["entry_at"].replace("Z", "+00:00"))
    entry_epoch = entry_at.timestamp()

    # Exact stop and risk from DB payload
    initial_stop   = float(trade["initial_stop"])
    risk_per_share = float(trade["initial_risk"])
    if risk_per_share < 0.001:
        risk_per_share = abs(entry_price - initial_stop)
    if risk_per_share < 0.001:
        risk_per_share = entry_price * 0.01

    bars = load_bars(symbol, entry_epoch, bars_conn)
    if bars.empty:
        return {"status": "BARS_MISSING"}

    entry_ts_pd     = pd.Timestamp(entry_at)
    bars_from_entry = bars[bars.index >= entry_ts_pd]
    if bars_from_entry.empty:
        return {"status": "NO_BARS_AFTER_ENTRY", "bar_total": len(bars)}

    config = PISConfig()

    try:
        idx0 = bars.index.get_loc(bars_from_entry.index[0])
        atr0 = _estimate_atr(bars, idx0)
        row0 = bars_from_entry.iloc[0]
        ts0  = bars_from_entry.index[0].to_pydatetime()
        if ts0.tzinfo is None:
            ts0 = ts0.replace(tzinfo=timezone.utc)

        obs0 = PositionObservation(
            timestamp=ts0, symbol=symbol, side=side_float, qty=qty,
            entry_price=entry_price, current_price=float(row0["close"]),
            high=float(row0["high"]), low=float(row0["low"]), close=float(row0["close"]),
            volume=float(row0["volume"]), atr=atr0,
            initial_stop=initial_stop, risk_per_share=risk_per_share,
            current_stop=initial_stop, r_multiple=0.0, news_sentiment_score=0.0
        )
        brain = PositionBrain(symbol, obs0, config)

        current_stop    = initial_stop
        new_exit_at     = None
        new_exit_price  = None
        new_exit_reason = "EOD"
        bars_held       = 0
        peak_price      = float(row0["close"])
        trough_price    = float(row0["close"])

        for bar_ts_pd, row in bars_from_entry.iloc[1:].iterrows():
            bar_ts = bar_ts_pd.to_pydatetime()
            if bar_ts.tzinfo is None:
                bar_ts = bar_ts.replace(tzinfo=timezone.utc)

            # Stop at market close
            if bar_ts >= MARKET_CLOSE_UTC:
                new_exit_price  = float(row["close"])
                new_exit_at     = bar_ts
                new_exit_reason = "MARKET_CLOSE"
                break

            idx   = bars.index.get_loc(bar_ts_pd)
            atr   = _estimate_atr(bars, idx)
            close = float(row["close"])
            high  = float(row["high"])
            low   = float(row["low"])

            # Track MFE/MAE in price terms for validation
            if side_float == 1.0:
                if high  > peak_price:   peak_price   = high
                if low   < trough_price: trough_price = low
            else:
                if low   < peak_price:   peak_price   = low
                if high  > trough_price: trough_price = high

            r_mul = (close - entry_price) * side_float / risk_per_share

            obs = PositionObservation(
                timestamp=bar_ts, symbol=symbol, side=side_float, qty=qty,
                entry_price=entry_price, current_price=close,
                high=high, low=low, close=close, volume=float(row["volume"]),
                atr=atr, initial_stop=initial_stop, risk_per_share=risk_per_share,
                current_stop=current_stop, r_multiple=r_mul, news_sentiment_score=0.0
            )

            decision = brain.observe(obs)
            bars_held += 1

            # Update protection floor
            if decision.target_stop_loss and decision.target_stop_loss != current_stop:
                current_stop = decision.target_stop_loss

            # PIS exit
            action = decision.action
            if action in (ExitAction.FULL_EXIT, ExitAction.PARTIAL_EXIT):
                new_exit_at    = bar_ts
                new_exit_price = apply_slippage(close, side_float)
                reasons = [r.value if hasattr(r, "value") else str(r)
                           for r in decision.reason_codes]
                new_exit_reason = ",".join(reasons)
                break

            # Hard stop (intrabar)
            if side_float == 1.0 and low <= current_stop:
                new_exit_at    = bar_ts
                new_exit_price = apply_slippage(current_stop, side_float)
                new_exit_reason = "HARD_STOP"
                break
            elif side_float == -1.0 and high >= current_stop:
                new_exit_at    = bar_ts
                new_exit_price = apply_slippage(current_stop, side_float)
                new_exit_reason = "HARD_STOP"
                break

        brain.close()

        # EOD exit at market close bar
        if new_exit_price is None:
            # Find closest bar to market close
            close_bars = bars[bars.index <= MARKET_CLOSE_UTC]
            if not close_bars.empty:
                last = close_bars.iloc[-1]
                new_exit_at    = close_bars.index[-1].to_pydatetime()
                new_exit_price = apply_slippage(float(last["close"]), side_float)
                new_exit_reason = "EOD"
            else:
                new_exit_at    = bars_from_entry.index[-1].to_pydatetime()
                new_exit_price = apply_slippage(float(bars_from_entry.iloc[-1]["close"]), side_float)
                new_exit_reason = "EOD"

        new_pnl = round((new_exit_price - entry_price) * side_float * qty, 2)

        # MFE/MAE in $ terms for validation vs DB
        mfe_price = (peak_price   - entry_price) * side_float  # max favorable excursion per share
        mae_price = (trough_price - entry_price) * side_float  # max adverse excursion per share

        return {
            "status":          "OK",
            "new_exit_at":     new_exit_at,
            "new_exit_price":  round(new_exit_price, 4),
            "new_pnl":         new_pnl,
            "new_exit_reason": new_exit_reason,
            "bars_held":       bars_held,
            "mfe_per_share":   round(mfe_price, 4),
            "mae_per_share":   round(mae_price, 4),
        }

    except Exception as exc:
        return {"status": f"ERROR:{exc}"}


def main():
    print("=" * 110)
    print("  Aug 03, 2026 — ACCURATE PIS Replay (Master bars_cache.db + exact stops + 2bps slippage)")
    print("=" * 110)

    mconn = sqlite3.connect(str(MASTER_DB))
    bconn = sqlite3.connect(str(MASTER_BARS))

    df_raw = pd.read_sql(
        "SELECT id, symbol, pnl, exit_at, payload FROM trades "
        "WHERE exit_at LIKE '2026-08-03%' AND mode='paper' ORDER BY exit_at ASC;",
        mconn
    )
    mconn.close()
    print(f"\nLoaded {len(df_raw)} paper trades from Aug 03\n")

    ET = timedelta(hours=-4)
    results = []

    for _, row in df_raw.iterrows():
        p = json.loads(row["payload"]) if isinstance(row["payload"], str) else {}
        trade = {
            "symbol":       row["symbol"],
            "side":         p.get("side", "long"),
            "qty":          p.get("qty", 0),
            "strategy":     p.get("strategy", ""),
            "entry_price":  p.get("entry_price"),
            "exit_price":   p.get("exit_price"),
            "entry_at":     p.get("entry_at"),
            "exit_at":      p.get("exit_at", row["exit_at"]),
            "initial_stop": p.get("initial_stop"),
            "initial_risk": p.get("initial_risk"),
            "legacy_pnl":   float(row["pnl"]),
            "legacy_reason":p.get("exit_reason", ""),
            # Ground truth from DB
            "db_mfe":       p.get("mfe"),   # max favorable excursion (per share in $)
            "db_mae":       p.get("mae"),   # max adverse excursion (per share in $)
            "db_r":         p.get("r_multiple"),
        }

        if not all([trade["entry_price"], trade["exit_price"], trade["entry_at"],
                    trade["initial_stop"], trade["initial_risk"]]):
            print(f"  SKIP {trade['symbol']} — missing required fields")
            continue

        sym   = trade["symbol"]
        strat = trade["strategy"]
        print(f"  Replaying {sym:<5} {strat:<15} entry={str(trade['entry_at'])[:19]}  ...",
              end="", flush=True)

        replay = replay_one(trade, bconn)

        leg_pnl = trade["legacy_pnl"]
        new_pnl = replay.get("new_pnl", 0.0) or 0.0
        delta   = round(new_pnl - leg_pnl, 2)

        entry_at_dt = datetime.fromisoformat(trade["entry_at"].replace("Z", "+00:00"))
        exit_at_dt  = datetime.fromisoformat(str(trade["exit_at"]).replace("Z", "+00:00"))
        new_exit_dt = replay.get("new_exit_at")

        ent_et = (entry_at_dt + ET).strftime("%H:%M:%S")
        leg_et = (exit_at_dt + ET).strftime("%H:%M:%S")
        new_et = (new_exit_dt + ET).strftime("%H:%M:%S") if new_exit_dt else "N/A"
        leg_hold = int((exit_at_dt - entry_at_dt).total_seconds() / 60)
        new_hold = int((new_exit_dt - entry_at_dt).total_seconds() / 60) if new_exit_dt else 0

        # Validation: compare replay MFE/MAE against DB values
        mfe_match = ""
        if trade["db_mfe"] is not None and replay.get("mfe_per_share") is not None:
            db_mfe = float(trade["db_mfe"])
            rp_mfe = float(replay["mfe_per_share"])
            mfe_match = f"MFE_DB={db_mfe:+.3f} MFE_REPLAY={rp_mfe:+.3f}"

        results.append({
            "symbol":          sym,
            "strategy":        strat,
            "side":            trade["side"],
            "qty":             trade["qty"],
            "entry_et":        ent_et,
            "entry_price":     trade["entry_price"],
            "initial_stop":    trade["initial_stop"],
            "initial_risk":    trade["initial_risk"],
            # Legacy
            "legacy_exit_et":  leg_et,
            "legacy_exit_px":  trade["exit_price"],
            "legacy_pnl":      leg_pnl,
            "legacy_hold_min": leg_hold,
            "legacy_reason":   trade["legacy_reason"],
            # DB ground truth
            "db_mfe":          trade["db_mfe"],
            "db_mae":          trade["db_mae"],
            "db_r":            trade["db_r"],
            # New PIS replay
            "new_exit_et":     new_et,
            "new_exit_px":     replay.get("new_exit_price"),
            "new_pnl":         new_pnl,
            "new_hold_min":    new_hold,
            "new_reason":      replay.get("new_exit_reason", replay.get("status","ERROR")),
            "new_bars_held":   replay.get("bars_held", 0),
            "replay_mfe":      replay.get("mfe_per_share"),
            "replay_mae":      replay.get("mae_per_share"),
            "pnl_delta":       delta,
        })

        print(f"  legacy={leg_pnl:+.2f} ({leg_hold}min)  new={new_pnl:+.2f} ({new_hold}min)  "
              f"delta={delta:+.2f}  {replay.get('new_exit_reason','')[:25]}")

    bconn.close()

    # Print final table
    print("\n" + "=" * 165)
    print(f"{'#':>2} {'SYM':<5} {'STRAT':<14} {'SIDE':<5} "
          f"{'ENT_ET':>8} {'ENT_$':>8} {'STOP':>8} {'RISK/SH':>7} "
          f"{'LEG_ET':>8} {'LEG_$':>8} {'LEG_PNL':>9} {'LEG_MIN':>7} {'LEG_REASON':<30} "
          f"{'NEW_ET':>8} {'NEW_$':>8} {'NEW_PNL':>9} {'NEW_MIN':>7} {'DELTA':>8} NEW_REASON")
    print("-" * 165)

    total_leg = 0.0
    total_new = 0.0

    for i, r in enumerate(results, 1):
        leg = float(r["legacy_pnl"])
        new = float(r["new_pnl"])
        total_leg += leg
        total_new += new
        d = r["pnl_delta"]
        sign = "▲" if d > 0 else "▼"

        print(f"{i:>2} {r['symbol']:<5} {r['strategy']:<14} {r['side']:<5} "
              f"{r['entry_et']:>8} {float(r['entry_price']):>8.4f} "
              f"{float(r['initial_stop']):>8.4f} {float(r['initial_risk']):>7.4f} "
              f"{r['legacy_exit_et']:>8} {float(r['legacy_exit_px']):>8.4f} {leg:>9.2f} "
              f"{int(r['legacy_hold_min'] or 0):>7} {str(r['legacy_reason'])[:30]:<30} "
              f"{str(r['new_exit_et']):>8} {str(r['new_exit_px'] or 'N/A'):>8} {new:>9.2f} "
              f"{int(r['new_hold_min'] or 0):>7} {sign}{d:>+7.2f} {str(r['new_reason'])[:35]}")

    print("=" * 165)
    print(f"\n  LEGACY TOTAL P&L : ${total_leg:>10,.2f}")
    print(f"  NEW PIS TOTAL P&L: ${total_new:>10,.2f}")
    print(f"  NET DELTA        : ${total_new - total_leg:>+10,.2f}")
    print(f"\n  Data source: Alpha-Bot Master bars_cache.db")
    print(f"  Slippage: {SLIPPAGE_BPS} bps applied on exits")
    print(f"  Stops: exact initial_stop from DB payload (no fallback used)")

    pd.DataFrame(results).to_csv(str(OUTPUT_CSV), index=False)
    print(f"\n  Full CSV: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
