"""
PIS Trade Replay Comparison Script
====================================
Reads every completed trade from the Master Bot database, replays it
tick-by-tick through the NEW QA Bot PIS code (PositionBrain), and
produces a side-by-side P&L comparison table.

NO VALUES ARE HARDCODED OR FABRICATED.
- Legacy P&L  : directly from Alpha-Bot Master/data/alphadesk.db
- Bars data   : from Bot_QA/data/bars_cache.db (shared 1-minute OHLCV)
- New PIS     : Bot_QA/backend/app/pis/ (PositionBrain, PISConfig)

Usage:
    cd c:\\Users\\peddi\\Desktop\\Bots\\Bot_QA
    python scripts/pis_trade_replay_comparison.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

# ---------------------------------------------------------------------------
# Path setup – make backend/app importable
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent  # Bot_QA/
BACKEND   = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

# ---------------------------------------------------------------------------
# Database paths
# ---------------------------------------------------------------------------
MASTER_ALPHADESK = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db")
QA_BARS_DB       = REPO_ROOT / "data" / "bars_cache.db"
OUTPUT_CSV       = Path(__file__).parent / "pis_trade_comparison_results.csv"


# ---------------------------------------------------------------------------
# ATR estimation helper
# ---------------------------------------------------------------------------
def _estimate_atr(bars: pd.DataFrame, idx: int, window: int = 14) -> float:
    start = max(0, idx - window)
    sub   = bars.iloc[start : idx + 1]
    if len(sub) < 2:
        return bars.iloc[idx]["close"] * 0.005
    trs = []
    closes = sub["close"].values
    highs  = sub["high"].values
    lows   = sub["low"].values
    for i in range(1, len(sub)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i]  - closes[i - 1]))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else bars.iloc[idx]["close"] * 0.005


# ---------------------------------------------------------------------------
# Load 1-minute bars from bars_cache.db
# ---------------------------------------------------------------------------
def load_bars(symbol: str, entry_epoch: float, end_epoch: float,
              bars_conn: sqlite3.Connection) -> pd.DataFrame:
    start_e = entry_epoch - 1800   # 30 min warmup
    end_e   = end_epoch   + 7200   # 2 h buffer

    df = pd.read_sql(
        """SELECT ts, open, high, low, close, volume
           FROM   bars
           WHERE  symbol = ? AND tf = '1Min' AND ts >= ? AND ts <= ?
           ORDER  BY ts ASC""",
        bars_conn,
        params=(symbol.upper(), int(start_e), int(end_e)),
    )
    if df.empty:
        return df
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df.set_index("dt", inplace=True)
    return df


# ---------------------------------------------------------------------------
# Build a PositionObservation
# ---------------------------------------------------------------------------
def make_observation(bar_ts, symbol, side_float, qty, entry_price,
                     initial_stop, risk_per_share, atr, row,
                     current_stop=None, take_profit=None):
    from app.pis.models import PositionObservation

    close = float(row["close"])
    high  = float(row["high"])
    low   = float(row["low"])
    r_mul = (close - entry_price) * side_float / risk_per_share if risk_per_share > 0 else 0.0

    return PositionObservation(
        timestamp=bar_ts,
        symbol=symbol.upper(),
        side=side_float,
        qty=qty,
        entry_price=entry_price,
        current_price=close,
        high=high,
        low=low,
        close=close,
        volume=float(row["volume"]),
        atr=atr,
        initial_stop=initial_stop,
        risk_per_share=risk_per_share,
        current_stop=current_stop or initial_stop,
        take_profit=take_profit,
        r_multiple=r_mul,
        news_sentiment_score=0.0,
    )


# ---------------------------------------------------------------------------
# Replay one trade through new PIS
# ---------------------------------------------------------------------------
def replay_trade_through_pis(trade: dict, bars_conn: sqlite3.Connection) -> dict:
    from app.pis.models import PISConfig, ExitAction
    from app.pis.position_brain import PositionBrain

    symbol     = trade["symbol"]
    side_str   = (trade.get("side") or "long").lower()
    side_float = 1.0 if "long" in side_str else -1.0
    qty        = float(trade["qty"])
    entry_price = float(trade["entry_price"])

    try:
        entry_at  = datetime.fromisoformat(trade["entry_at"].replace("Z", "+00:00"))
        exit_at   = datetime.fromisoformat(trade["exit_at"].replace("Z", "+00:00"))
    except Exception as e:
        return {"new_exit_at": None, "new_exit_price": None, "new_pnl": None,
                "new_exit_reason": f"PARSE_ERROR:{e}"}

    entry_epoch = entry_at.timestamp()
    exit_epoch  = exit_at.timestamp()

    raw_stop = trade.get("stop_loss")
    initial_stop = float(raw_stop) if raw_stop and float(raw_stop) > 0 else (
        entry_price * (0.99 if side_float == 1.0 else 1.01)
    )
    risk_per_share = abs(entry_price - initial_stop)
    if risk_per_share < 0.001:
        risk_per_share = entry_price * 0.01

    raw_tp = trade.get("take_profit")
    take_profit = float(raw_tp) if raw_tp and float(raw_tp) > 0 else None

    bars = load_bars(symbol, entry_epoch, exit_epoch + 3600, bars_conn)
    if bars.empty:
        return {"new_exit_at": None, "new_exit_price": None, "new_pnl": None,
                "new_exit_reason": "BARS_MISSING"}

    entry_ts_pd    = pd.Timestamp(entry_at)
    bars_from_entry = bars[bars.index >= entry_ts_pd]
    if bars_from_entry.empty:
        return {"new_exit_at": None, "new_exit_price": None, "new_pnl": None,
                "new_exit_reason": "NO_BARS_AFTER_ENTRY"}

    config = PISConfig()

    try:
        idx0   = bars.index.get_loc(bars_from_entry.index[0])
        atr0   = _estimate_atr(bars, idx0)
        row0   = bars_from_entry.iloc[0]
        ts0    = bars_from_entry.index[0].to_pydatetime()
        if ts0.tzinfo is None:
            ts0 = ts0.replace(tzinfo=timezone.utc)

        obs0  = make_observation(ts0, symbol, side_float, qty, entry_price,
                                 initial_stop, risk_per_share, atr0, row0,
                                 take_profit=take_profit)
        brain = PositionBrain(symbol, obs0, config)

        current_stop    = initial_stop
        new_exit_at     = None
        new_exit_price  = None
        new_exit_reason = "EOD_NO_PIS_EXIT"

        for bar_ts_pd, row in bars_from_entry.iloc[1:].iterrows():
            bar_ts = bar_ts_pd.to_pydatetime()
            if bar_ts.tzinfo is None:
                bar_ts = bar_ts.replace(tzinfo=timezone.utc)

            idx = bars.index.get_loc(bar_ts_pd)
            atr = _estimate_atr(bars, idx)

            obs = make_observation(bar_ts, symbol, side_float, qty, entry_price,
                                   initial_stop, risk_per_share, atr, row,
                                   current_stop=current_stop, take_profit=take_profit)

            decision = brain.observe(obs)

            if decision.target_stop_loss and decision.target_stop_loss != current_stop:
                current_stop = decision.target_stop_loss

            # Check if PIS wants out
            action = decision.action
            should_exit = (
                action == ExitAction.FULL_EXIT or
                action == ExitAction.PARTIAL_EXIT or
                (hasattr(action, "should_exit") and action.should_exit)
            )
            if should_exit:
                new_exit_at    = bar_ts
                new_exit_price = float(row["close"])
                reasons = [r.value if hasattr(r, "value") else str(r)
                           for r in decision.reason_codes]
                new_exit_reason = ",".join(reasons) or str(action)
                break

            # Hard stop check
            if side_float == 1.0 and float(row["low"]) <= current_stop:
                new_exit_at    = bar_ts
                new_exit_price = current_stop
                new_exit_reason = "HARD_STOP"
                break
            elif side_float == -1.0 and float(row["high"]) >= current_stop:
                new_exit_at    = bar_ts
                new_exit_price = current_stop
                new_exit_reason = "HARD_STOP"
                break

        brain.close()

        if new_exit_price is None:
            last_row       = bars_from_entry.iloc[-1]
            new_exit_at    = bars_from_entry.index[-1].to_pydatetime()
            new_exit_price = float(last_row["close"])
            new_exit_reason = "EOD_NO_PIS_EXIT"

        new_pnl = round((new_exit_price - entry_price) * side_float * qty, 2)

        return {
            "new_exit_at":     new_exit_at,
            "new_exit_price":  round(new_exit_price, 4),
            "new_pnl":         new_pnl,
            "new_exit_reason": new_exit_reason,
        }

    except Exception as exc:
        return {"new_exit_at": None, "new_exit_price": None, "new_pnl": None,
                "new_exit_reason": f"ERROR:{exc}"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 90)
    print("  PIS TRADE REPLAY COMPARISON  -  Legacy Bot vs New QA PIS Code")
    print("=" * 90)

    if not MASTER_ALPHADESK.exists():
        print(f"ERROR: Master DB not found: {MASTER_ALPHADESK}")
        sys.exit(1)
    if not QA_BARS_DB.exists():
        print(f"ERROR: QA bars DB not found: {QA_BARS_DB}")
        sys.exit(1)

    # Load all completed trades from Master Bot
    mconn  = sqlite3.connect(str(MASTER_ALPHADESK))
    df_raw = pd.read_sql(
        "SELECT id, mode, symbol, pnl, exit_at, payload FROM trades ORDER BY exit_at ASC;",
        mconn
    )
    mconn.close()

    print(f"\nLoaded {len(df_raw)} completed trades  |  Modes: {df_raw['mode'].value_counts().to_dict()}")

    records = []
    for _, row in df_raw.iterrows():
        p = json.loads(row["payload"]) if isinstance(row["payload"], str) else {}
        records.append({
            "trade_id":    row["id"],
            "mode":        row["mode"],
            "symbol":      row["symbol"],
            "side":        p.get("side", "long"),
            "qty":         p.get("qty", 0),
            "strategy":    p.get("strategy", ""),
            "entry_price": p.get("entry_price"),
            "exit_price":  p.get("exit_price"),
            "entry_at":    p.get("entry_at"),
            "exit_at":     p.get("exit_at", row["exit_at"]),
            "stop_loss":   p.get("stop_loss") or p.get("initial_stop"),
            "take_profit": p.get("take_profit"),
            "legacy_pnl":  row["pnl"],
            "legacy_reason": p.get("exit_reason", ""),
        })

    # Step 1: keep only valid records with all required fields
    records_valid = [
        r for r in records
        if r["entry_price"] and r["exit_price"] and r["entry_at"] and r["exit_at"]
        and float(r["qty"]) > 0
    ]

    # Step 2: paper mode only — demo prices are synthetic and don't map to real bars
    records_paper = [r for r in records_valid if r["mode"] == "paper"]

    # Step 3: exclude raw broker fill records and manual closes
    #         (strategy='alpaca' or 'manual' are not bot-generated positions)
    BOT_STRATEGIES = {"orb", "momentum", "rsi2_reversion", "bb_squeeze",
                      "vwap_reversion", "rsi_reversion", "mean_reversion"}
    records_strategy = [r for r in records_paper
                        if str(r.get("strategy", "")).lower() in BOT_STRATEGIES]

    # Step 4: deduplicate — one unique completed position per (symbol, entry_at, side)
    #         The trades table stores multiple order legs per position; we want exactly one.
    seen = set()
    trades = []
    for r in records_strategy:
        # Round entry_at to minute precision for dedup key
        ea = str(r["entry_at"])[:16]
        key = (r["symbol"], ea, r["side"])
        if key not in seen:
            seen.add(key)
            trades.append(r)

    print(f"\nRaw records from DB       : {len(records)}")
    print(f"Valid (fields present)    : {len(records_valid)}")
    print(f"Paper mode only           : {len(records_paper)}")
    print(f"Bot strategy trades only  : {len(records_strategy)}")
    print(f"Deduplicated positions    : {len(trades)}  ← these will be replayed")
    print(f"\nStarting replay through new PIS code...\n")


    bconn   = sqlite3.connect(str(QA_BARS_DB))
    results = []

    for i, trade in enumerate(trades, 1):
        print(f"  [{i:3d}/{len(trades)}] {trade['symbol']:<6} {trade['strategy']:<15} "
              f"entry={str(trade['entry_at'])[:19]}  ...", end="", flush=True)

        replay = replay_trade_through_pis(trade, bconn)

        pnl_delta = round(float(replay["new_pnl"] or 0) - float(trade["legacy_pnl"]), 2)

        results.append({
            "trade_id":          trade["trade_id"],
            "mode":              trade["mode"],
            "symbol":            trade["symbol"],
            "strategy":          trade["strategy"],
            "side":              trade["side"],
            "qty":               trade["qty"],
            "entry_at":          trade["entry_at"],
            "entry_price":       trade["entry_price"],
            "legacy_exit_at":    trade["exit_at"],
            "legacy_exit_price": trade["exit_price"],
            "legacy_pnl":        trade["legacy_pnl"],
            "legacy_reason":     trade["legacy_reason"],
            "new_exit_at":       str(replay["new_exit_at"]) if replay["new_exit_at"] else "N/A",
            "new_exit_price":    replay["new_exit_price"],
            "new_pnl":           replay["new_pnl"],
            "new_reason":        replay["new_exit_reason"],
            "pnl_delta":         pnl_delta,
        })

        status = f"delta={pnl_delta:+.2f}" if replay["new_pnl"] is not None else replay["new_exit_reason"][:15]
        print(f"  legacy={float(trade['legacy_pnl']):+.2f}  new={float(replay['new_pnl'] or 0):+.2f}  {status}")

    bconn.close()

    df_results = pd.DataFrame(results)
    df_results.to_csv(str(OUTPUT_CSV), index=False)

    # Summary table
    print("\n" + "=" * 115)
    print(f"{'SYM':<6} {'SIDE':<5} {'QTY':>5} {'ENTRY_AT':>17} {'EP':>8} "
          f"{'LEG_EXIT_AT':>17} {'LEP':>8} {'LEG_PNL':>9} "
          f"{'NEW_EXIT_AT':>17} {'NEP':>8} {'NEW_PNL':>9} {'DELTA':>8} {'NEW_REASON'}")
    print("-" * 115)

    total_legacy = 0.0
    total_new    = 0.0
    errors       = 0

    for r in results:
        leg_pnl = float(r["legacy_pnl"])
        new_pnl = float(r["new_pnl"]) if r["new_pnl"] is not None else 0.0
        if r["new_pnl"] is None:
            errors += 1
        total_legacy += leg_pnl
        total_new    += new_pnl

        ea  = str(r["entry_at"])[:16].replace("T"," ").replace("+00:00","").replace("Z","")
        lea = str(r["legacy_exit_at"])[:16].replace("T"," ").replace("+00:00","").replace("Z","")
        nea = str(r["new_exit_at"])[:16].replace("T"," ").replace("+00:00","").replace("Z","")

        print(f"{r['symbol']:<6} {r['side']:<5} {int(float(r['qty'])):>5} "
              f"{ea:>17} {float(r['entry_price']):>8.4f} "
              f"{lea:>17} {float(r['legacy_exit_price']):>8.4f} {leg_pnl:>9.2f} "
              f"{nea:>17} {str(r['new_exit_price'] or 'N/A'):>8} {new_pnl:>9.2f} "
              f"{r['pnl_delta'] or 0.0:>8.2f} {str(r['new_reason'])[:25]}")

    print("=" * 115)
    print(f"\n  TOTAL LEGACY P&L : ${total_legacy:>10,.2f}")
    print(f"  TOTAL NEW PIS P&L: ${total_new:>10,.2f}")
    print(f"  NET IMPROVEMENT  : ${total_new - total_legacy:>+10,.2f}")
    print(f"\n  Trades replayed  : {len(results)}")
    print(f"  Replay errors    : {errors}")
    print(f"\n  Full CSV: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
