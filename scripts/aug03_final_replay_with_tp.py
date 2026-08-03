"""
Aug 03, 2026 — FINAL ACCURATE PIS Replay WITH TAKE PROFIT
===========================================================
Key fix vs previous replay:
  - take_profit passed to PIS from actual orders table (exact broker TP levels)
  - This is the true apples-to-apples: what would PIS-as-sole-authority do
    with the SAME entry, SAME stop, SAME take_profit as the live master bot?

No fabricated values. All data from Master bot DB and bars_cache.db.
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
OUTPUT_CSV  = REPO_ROOT / "scripts" / "aug03_final_replay_with_tp.csv"

SLIPPAGE_BPS     = 2
MARKET_CLOSE_UTC = datetime(2026, 8, 3, 20, 0, 0, tzinfo=timezone.utc)
ET               = timedelta(hours=-4)


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


def replay_one(trade: dict, bars_conn: sqlite3.Connection) -> dict:
    from app.pis.models import PISConfig, ExitAction, PositionObservation
    from app.pis.position_brain import PositionBrain

    symbol      = trade["symbol"]
    side_float  = 1.0 if trade["side"].lower() == "long" else -1.0
    qty         = float(trade["qty"])
    entry_price = float(trade["entry_price"])
    entry_at    = datetime.fromisoformat(trade["entry_at"].replace("Z", "+00:00"))
    entry_epoch = entry_at.timestamp()
    initial_stop   = float(trade["initial_stop"])
    risk_per_share = float(trade["initial_risk"])
    take_profit    = trade.get("take_profit")  # NEW: from orders table

    if risk_per_share < 0.001:
        risk_per_share = abs(entry_price - initial_stop)

    bars = load_bars(symbol, entry_epoch, bars_conn)
    if bars.empty:
        return {"status": "BARS_MISSING"}

    entry_ts_pd     = pd.Timestamp(entry_at)
    bars_from_entry = bars[bars.index >= entry_ts_pd]
    if bars_from_entry.empty:
        return {"status": "NO_BARS_AFTER_ENTRY"}

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
            current_stop=initial_stop, r_multiple=0.0, news_sentiment_score=0.0,
            take_profit=take_profit,
        )
        brain = PositionBrain(symbol, obs0, config)

        current_stop    = initial_stop
        new_exit_at     = None
        new_exit_price  = None
        new_exit_reason = "EOD"
        bars_held       = 0

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
                current_stop=current_stop, r_multiple=r_mul, news_sentiment_score=0.0,
                take_profit=take_profit,
            )
            decision = brain.observe(obs)
            bars_held += 1

            if decision.target_stop_loss:
                current_stop = decision.target_stop_loss

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

        new_pnl  = round((new_exit_price - entry_price) * side_float * qty, 2)
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
    # ---- Load TP from orders table ----
    mconn = sqlite3.connect(str(MASTER_DB))

    orders_raw = mconn.execute(
        "SELECT ts, payload FROM orders WHERE ts LIKE '2026-08-03%' ORDER BY ts;"
    ).fetchall()

    tp_by_sym  = {}  # symbol -> take_profit (from entry order)
    sl_by_sym  = {}  # symbol -> stop_loss   (from entry order)
    entry_orders = {}  # symbol -> first entry order ts (to match to trade)

    for ts, payload_str in orders_raw:
        p = json.loads(payload_str) if payload_str else {}
        sym = (p.get("symbol") or "").upper()
        leg = str(p.get("leg","")).lower()
        if leg == "entry" and sym:
            tp = p.get("take_profit")
            sl = p.get("stop_loss")
            if tp is not None:
                if sym not in tp_by_sym:
                    tp_by_sym[sym] = []
                tp_by_sym[sym].append((ts, float(tp)))
            if sl is not None:
                if sym not in sl_by_sym:
                    sl_by_sym[sym] = []
                sl_by_sym[sym].append((ts, float(sl)))

    # For symbols with multiple trades (INTC, SPCX), pair TP to trade by entry time
    # We'll match by closest entry time
    def get_tp_for_trade(symbol: str, entry_at_str: str) -> float | None:
        tps = tp_by_sym.get(symbol)
        if not tps:
            return None
        # Find closest TP entry order to the trade's entry
        entry_dt = datetime.fromisoformat(entry_at_str.replace("Z", "+00:00")).timestamp()
        best = min(tps, key=lambda x: abs(
            datetime.fromisoformat(x[0].replace("Z", "+00:00")).timestamp() - entry_dt
        ))
        return best[1]

    # ---- Load trades ----
    df_raw = pd.read_sql(
        "SELECT id, symbol, pnl, exit_at, payload FROM trades "
        "WHERE exit_at LIKE '2026-08-03%' AND mode='paper' ORDER BY exit_at ASC;",
        mconn
    )
    mconn.close()

    bconn = sqlite3.connect(str(MASTER_BARS))

    print("=" * 135)
    print("  Aug 03, 2026 — FINAL ACCURATE PIS Replay (WITH TAKE PROFIT from orders table)")
    print("  Entry, stop, AND take_profit all from actual broker orders — zero fabrication")
    print("=" * 135)

    results = []

    for _, row in df_raw.iterrows():
        p = json.loads(row["payload"]) if isinstance(row["payload"], str) else {}

        entry_at_str = p.get("entry_at")
        if not entry_at_str:
            continue

        tp = get_tp_for_trade(row["symbol"].upper(), entry_at_str)

        trade = {
            "symbol":       row["symbol"],
            "side":         p.get("side", "long"),
            "qty":          p.get("qty", 0),
            "strategy":     p.get("strategy", ""),
            "entry_price":  p.get("entry_price"),
            "exit_price":   p.get("exit_price"),
            "entry_at":     entry_at_str,
            "exit_at":      p.get("exit_at", row["exit_at"]),
            "initial_stop": p.get("initial_stop"),
            "initial_risk": p.get("initial_risk"),
            "legacy_pnl":   float(row["pnl"]),
            "legacy_reason":p.get("exit_reason", ""),
            "take_profit":  tp,
        }

        if not all([trade["entry_price"], trade["initial_stop"], trade["initial_risk"]]):
            continue

        entry_at_dt = datetime.fromisoformat(entry_at_str.replace("Z", "+00:00"))
        exit_at_dt  = datetime.fromisoformat(str(trade["exit_at"]).replace("Z", "+00:00"))
        leg_hold    = int((exit_at_dt - entry_at_dt).total_seconds() / 60)

        sym = trade["symbol"]
        print(f"\n  {sym:<5} {trade['strategy']:<14} entry={(entry_at_dt+ET).strftime('%H:%M')}  "
              f"stop={trade['initial_stop']}  TP={tp}  legacy={trade['legacy_pnl']:+.2f}",
              end="  ", flush=True)

        replay = replay_one(trade, bconn)
        new_pnl = replay.get("pnl", 0.0)
        delta   = round(new_pnl - trade["legacy_pnl"], 2)
        print(f"new={new_pnl:+.2f} ({replay.get('hold_min',0)}min)  "
              f"delta={delta:+.2f}  {replay.get('reason','')[:30]}")

        results.append({
            "symbol":       sym,
            "strategy":     trade["strategy"],
            "side":         trade["side"],
            "entry_et":     (entry_at_dt + ET).strftime("%H:%M:%S"),
            "entry_price":  trade["entry_price"],
            "stop":         trade["initial_stop"],
            "take_profit":  tp,
            "risk_per_sh":  trade["initial_risk"],
            "leg_et":       (exit_at_dt + ET).strftime("%H:%M:%S"),
            "leg_px":       trade["exit_price"],
            "leg_pnl":      trade["legacy_pnl"],
            "leg_min":      leg_hold,
            "leg_reason":   trade["legacy_reason"],
            "new_et":       replay.get("exit_et","ERR"),
            "new_px":       replay.get("exit_px"),
            "new_pnl":      new_pnl,
            "new_min":      replay.get("hold_min",0),
            "new_reason":   replay.get("reason", replay.get("status","")),
            "delta":        delta,
        })

    bconn.close()

    # ---- Final table ----
    sep = "=" * 160
    print(f"\n{sep}")
    print(f"{'#':>2} {'SYM':<5} {'STRAT':<14} {'SIDE':<5} {'ENTRY':>8} {'STOP':>8} {'TP':>8}  "
          f"{'LEG_ET':>8} {'LEG_PNL':>9} {'LEG_MIN':>7} {'LEG_REASON':<28}  "
          f"{'NEW_ET':>8} {'NEW_PNL':>9} {'NEW_MIN':>7} {'DELTA':>8}  NEW_REASON")
    print("-" * 160)

    tot_leg = tot_new = 0.0
    for i, r in enumerate(results, 1):
        leg = float(r["leg_pnl"])
        new = float(r["new_pnl"])
        tot_leg += leg
        tot_new += new
        d = r["delta"]
        sign = "▲" if d >= 0 else "▼"

        print(f"{i:>2} {r['symbol']:<5} {r['strategy']:<14} {r['side']:<5} "
              f"{str(r['entry_et']):>8} {float(r['stop']):>8.4f} "
              f"{str(r['take_profit'] or 'N/A'):>8}  "
              f"{str(r['leg_et']):>8} {leg:>9.2f} {int(r['leg_min'] or 0):>7} "
              f"{str(r['leg_reason'])[:28]:<28}  "
              f"{str(r['new_et']):>8} {new:>9.2f} {int(r['new_min'] or 0):>7} "
              f"{sign}{d:>+7.2f}  {str(r['new_reason'])[:40]}")

    print(sep)
    print(f"\n  LEGACY TOTAL P&L    : ${tot_leg:>10,.2f}")
    print(f"  NEW PIS TOTAL P&L   : ${tot_new:>10,.2f}")
    print(f"  NET DELTA           : ${tot_new - tot_leg:>+10,.2f}")
    print(f"\n  Data: Master bars_cache.db  |  TP: from orders table  |  Slippage: 2bps")

    pd.DataFrame(results).to_csv(str(OUTPUT_CSV), index=False)
    print(f"  CSV: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
