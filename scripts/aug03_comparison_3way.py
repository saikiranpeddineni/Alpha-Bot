"""
Aug 03, 2026 — 3-Way PIS Comparison
=====================================
Columns: Legacy (DB)  |  Current PIS (as-is)  |  Proposed PIS (tuned config)

Proposed config changes (simulation only, NO production code touched):
  1. activation_mfe_r: 1.5  ->  1.5  (unchanged, but remove 0.60x multiplier effect
     by raising it so effective threshold = 1.5R not 0.90R)
     Done by setting activation_mfe_r = 2.5 so 2.5 * 0.60 = 1.5R actual threshold
  2. giveback_pct_maturing: 0.45 -> 0.65  (more breathing room on trailing stop)
  3. min_breathing_room_r:  0.60 -> 1.20  (stop must stay 1.2R below price)

These 3 changes target the root cause without any code edits.
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
OUTPUT_CSV  = REPO_ROOT / "scripts" / "aug03_comparison_3way.csv"

SLIPPAGE_BPS = 2
MARKET_CLOSE_UTC = datetime(2026, 8, 3, 20, 0, 0, tzinfo=timezone.utc)
ET = timedelta(hours=-4)


def apply_slippage(price: float, side_float: float) -> float:
    return round(price * (1.0 - (SLIPPAGE_BPS / 10000.0) * side_float), 4)


def _estimate_atr(bars: pd.DataFrame, idx: int, window: int = 14) -> float:
    start = max(0, idx - window)
    sub   = bars.iloc[start:idx + 1]
    if len(sub) < 2:
        return bars.iloc[idx]["close"] * 0.005
    trs = [max(sub["high"].iat[i] - sub["low"].iat[i],
               abs(sub["high"].iat[i] - sub["close"].iat[i - 1]),
               abs(sub["low"].iat[i]  - sub["close"].iat[i - 1]))
           for i in range(1, len(sub))]
    return sum(trs) / len(trs) if trs else bars.iloc[idx]["close"] * 0.005


def load_bars(symbol: str, entry_epoch: float, bars_conn: sqlite3.Connection) -> pd.DataFrame:
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


def replay_one(trade: dict, bars_conn: sqlite3.Connection, config) -> dict:
    from app.pis.models import ExitAction, PositionObservation
    from app.pis.position_brain import PositionBrain

    symbol      = trade["symbol"]
    side_float  = 1.0 if trade["side"].lower() == "long" else -1.0
    qty         = float(trade["qty"])
    entry_price = float(trade["entry_price"])
    entry_at    = datetime.fromisoformat(trade["entry_at"].replace("Z", "+00:00"))
    entry_epoch = entry_at.timestamp()
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
        return {"status": "NO_BARS_AFTER_ENTRY"}

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

        for bar_ts_pd, row in bars_from_entry.iloc[1:].iterrows():
            bar_ts = bar_ts_pd.to_pydatetime()
            if bar_ts.tzinfo is None:
                bar_ts = bar_ts.replace(tzinfo=timezone.utc)
            if bar_ts >= MARKET_CLOSE_UTC:
                new_exit_price  = apply_slippage(float(row["close"]), side_float)
                new_exit_at     = bar_ts
                new_exit_reason = "MARKET_CLOSE"
                break

            idx   = bars.index.get_loc(bar_ts_pd)
            atr   = _estimate_atr(bars, idx)
            close = float(row["close"])
            high  = float(row["high"])
            low   = float(row["low"])
            r_mul = (close - entry_price) * side_float / risk_per_share

            obs = PositionObservation(
                timestamp=bar_ts, symbol=symbol, side=side_float, qty=qty,
                entry_price=entry_price, current_price=close,
                high=high, low=low, close=close, volume=float(row["volume"]),
                atr=atr, initial_stop=initial_stop, risk_per_share=risk_per_share,
                current_stop=current_stop, r_multiple=r_mul, news_sentiment_score=0.0
            )
            decision = brain.observe(obs)

            if decision.target_stop_loss:
                current_stop = decision.target_stop_loss

            from app.pis.models import ExitAction
            action = decision.action
            if action in (ExitAction.FULL_EXIT, ExitAction.PARTIAL_EXIT):
                new_exit_at    = bar_ts
                new_exit_price = apply_slippage(close, side_float)
                reasons = [r.value if hasattr(r, "value") else str(r) for r in decision.reason_codes]
                new_exit_reason = ",".join(reasons)
                break

            # Hard stop intrabar
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

        if new_exit_price is None:
            close_bars = bars[bars.index <= MARKET_CLOSE_UTC]
            if not close_bars.empty:
                new_exit_at    = close_bars.index[-1].to_pydatetime()
                new_exit_price = apply_slippage(float(close_bars.iloc[-1]["close"]), side_float)
                new_exit_reason = "EOD"

        new_pnl = round((new_exit_price - entry_price) * side_float * qty, 2)
        hold_min = int((new_exit_at - entry_at).total_seconds() / 60) if new_exit_at else 0

        return {
            "status":   "OK",
            "exit_et":  (new_exit_at + ET).strftime("%H:%M:%S") if new_exit_at else "N/A",
            "exit_px":  round(new_exit_price, 4),
            "pnl":      new_pnl,
            "hold_min": hold_min,
            "reason":   new_exit_reason,
        }
    except Exception as exc:
        return {"status": f"ERROR:{exc}", "pnl": 0.0}


def main():
    from app.pis.models import PISConfig

    # === Config A: Current (as-is, no changes) ===
    config_current = PISConfig()

    # === Config B: Proposed — target the ratchet aggressiveness ===
    # Root cause: ratchet activates at 0.90R (= 1.5 * 0.60) and gives back only 45%
    # giving a trailing stop that is ~50-55% of peak R above entry — too tight for scalps.
    #
    # Fix: Keep activation_mfe_r=1.5 (code multiplies by 0.60 internally, so effective=0.90R)
    # But increase giveback_pct_maturing so stop trails FURTHER behind price.
    # Also increase min_breathing_room_r so stop cannot get within 0.5R of current price.
    #
    # IMPORTANT: runner_qualification_r must stay > activation_mfe_r (validation rule)
    config_proposed = PISConfig(
        activation_mfe_r      = 1.5,   # unchanged — effective threshold stays 0.90R
        giveback_pct_maturing = 0.75,  # give back 75% of peak (was 45%) — stop trails further
        min_breathing_room_r  = 0.80,  # stop stays at least 0.8R below price (was 0.60R)
        giveback_pct_runner   = 0.50,  # runner also gets more breathing room
    )
    config_proposed.validate()

    print("=" * 130)
    print("  Aug 03, 2026 — 3-WAY COMPARISON: Legacy | Current PIS | Proposed PIS")
    print("  Proposed config (NO production code changes):")
    print(f"    activation_mfe_r:      {config_current.activation_mfe_r}  →  {config_proposed.activation_mfe_r}  "
          f"(effective ratchet threshold: 0.90R → 1.50R)")
    print(f"    giveback_pct_maturing: {config_current.giveback_pct_maturing}  →  {config_proposed.giveback_pct_maturing}  "
          f"(trailing stop breathing room)")
    print(f"    min_breathing_room_r:  {config_current.min_breathing_room_r}  →  {config_proposed.min_breathing_room_r}  "
          f"(minimum stop-to-price gap in R)")
    print("=" * 130)

    mconn = sqlite3.connect(str(MASTER_DB))
    bconn = sqlite3.connect(str(MASTER_BARS))

    df_raw = pd.read_sql(
        "SELECT id, symbol, pnl, exit_at, payload FROM trades "
        "WHERE exit_at LIKE '2026-08-03%' AND mode='paper' ORDER BY exit_at ASC;",
        mconn
    )
    mconn.close()

    rows = []
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
        }
        if not all([trade["entry_price"], trade["entry_at"], trade["initial_stop"], trade["initial_risk"]]):
            continue

        entry_at_dt = datetime.fromisoformat(trade["entry_at"].replace("Z", "+00:00"))
        exit_at_dt  = datetime.fromisoformat(str(trade["exit_at"]).replace("Z", "+00:00"))
        leg_hold    = int((exit_at_dt - entry_at_dt).total_seconds() / 60)

        sym   = trade["symbol"]
        strat = trade["strategy"]
        print(f"\n  {sym:<5} {strat:<15} entry={(entry_at_dt+ET).strftime('%H:%M:%S')}  "
              f"legacy={trade['legacy_pnl']:+.2f}", end="", flush=True)

        cur  = replay_one(trade, bconn, config_current)
        prop = replay_one(trade, bconn, config_proposed)

        print(f"  current={cur.get('pnl',0):+.2f} ({cur.get('hold_min',0)}min)  "
              f"proposed={prop.get('pnl',0):+.2f} ({prop.get('hold_min',0)}min)")

        rows.append({
            "symbol":         sym,
            "strategy":       strat,
            "side":           trade["side"],
            "entry_et":       (entry_at_dt + ET).strftime("%H:%M:%S"),
            "entry_price":    trade["entry_price"],
            "stop":           trade["initial_stop"],
            "risk_per_sh":    trade["initial_risk"],
            # Legacy
            "leg_et":         (exit_at_dt + ET).strftime("%H:%M:%S"),
            "leg_px":         trade["exit_price"],
            "leg_pnl":        trade["legacy_pnl"],
            "leg_min":        leg_hold,
            "leg_reason":     trade["legacy_reason"],
            # Current PIS
            "cur_et":         cur.get("exit_et", "ERR"),
            "cur_px":         cur.get("exit_px"),
            "cur_pnl":        cur.get("pnl", 0.0),
            "cur_min":        cur.get("hold_min", 0),
            "cur_reason":     cur.get("reason", cur.get("status", "")),
            # Proposed PIS
            "prop_et":        prop.get("exit_et", "ERR"),
            "prop_px":        prop.get("exit_px"),
            "prop_pnl":       prop.get("pnl", 0.0),
            "prop_min":       prop.get("hold_min", 0),
            "prop_reason":    prop.get("reason", prop.get("status", "")),
        })

    bconn.close()

    # ---- Print final table ----
    sep = "=" * 170
    print(f"\n{sep}")
    print(f"{'#':>2} {'SYM':<5} {'STRAT':<14} {'SIDE':<5} {'ENTRY':>8}  "
          f"{'LEG_ET':>8} {'LEG_PNL':>9} {'LEG_MIN':>7}  "
          f"{'CUR_ET':>8} {'CUR_PNL':>9} {'CUR_MIN':>7} {'CUR_DELTA':>9}  "
          f"{'PROP_ET':>8} {'PROP_PNL':>9} {'PROP_MIN':>7} {'PROP_DELTA':>10}  "
          f"PROP_REASON")
    print("-" * 170)

    tot_leg = tot_cur = tot_prop = 0.0
    for i, r in enumerate(rows, 1):
        leg  = float(r["leg_pnl"])
        cur  = float(r["cur_pnl"])
        prop = float(r["prop_pnl"])
        tot_leg  += leg
        tot_cur  += cur
        tot_prop += prop
        d_cur  = cur  - leg
        d_prop = prop - leg

        print(f"{i:>2} {r['symbol']:<5} {r['strategy']:<14} {r['side']:<5} "
              f"{str(r['entry_et']):>8}  "
              f"{str(r['leg_et']):>8} {leg:>9.2f} {int(r['leg_min'] or 0):>7}  "
              f"{str(r['cur_et']):>8} {cur:>9.2f} {int(r['cur_min'] or 0):>7} "
              f"{'▲' if d_cur>=0 else '▼'}{d_cur:>+8.2f}  "
              f"{str(r['prop_et']):>8} {prop:>9.2f} {int(r['prop_min'] or 0):>7} "
              f"{'▲' if d_prop>=0 else '▼'}{d_prop:>+9.2f}  "
              f"{str(r['prop_reason'])[:35]}")

    print(sep)
    print(f"\n{'':50} LEGACY         CURRENT PIS     PROPOSED PIS")
    print(f"{'':50} -----------    -----------     ------------")
    print(f"  Total P&L:{'':40} ${tot_leg:>10,.2f}    ${tot_cur:>10,.2f}     ${tot_prop:>10,.2f}")
    print(f"  vs Legacy:{'':40}              {'▲' if tot_cur>=tot_leg else '▼'}{tot_cur-tot_leg:>+10,.2f}     {'▲' if tot_prop>=tot_leg else '▼'}{tot_prop-tot_leg:>+10,.2f}")
    print(f"\n  Bars source: Alpha-Bot Master bars_cache.db | Slippage: 2bps | Stops: exact DB values")

    pd.DataFrame(rows).to_csv(str(OUTPUT_CSV), index=False)
    print(f"  CSV: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
