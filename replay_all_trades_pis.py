"""
Replay Yesterday's (2026-08-03) Completed Trades with Latest PIS Code
=====================================================================
Proper Bar-by-Bar Lifecycle Execution:
1. Active stop loss entering the bar is evaluated against bar Low/High (intrabar stop breach).
2. Take Profit entering the bar is evaluated against bar High/Low (intrabar target hit).
3. PIS observes completed bar and updates lifecycle, health, thesis, and computes new protection floor.
4. If PIS issues FULL_EXIT or PARTIAL_EXIT, exit at bar close.
5. Otherwise, active stop floor is updated for the next bar.
"""

from __future__ import annotations

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

SLIPPAGE_BPS = 2
MARKET_CLOSE_UTC = datetime(2026, 8, 3, 20, 0, 0, tzinfo=timezone.utc)
ET = timezone(timedelta(hours=-4))


def apply_slippage(price: float, side_float: float) -> float:
    return round(price * (1.0 - (SLIPPAGE_BPS / 10000.0) * side_float), 4)


def estimate_atr(bars: pd.DataFrame, idx: int, window: int = 14) -> float:
    start = max(0, idx - window)
    sub = bars.iloc[start:idx + 1]
    if len(sub) < 2:
        return bars.iloc[idx]["close"] * 0.005
    trs = [
        max(
            sub["high"].iat[i] - sub["low"].iat[i],
            abs(sub["high"].iat[i] - sub["close"].iat[i - 1]),
            abs(sub["low"].iat[i] - sub["close"].iat[i - 1]),
        )
        for i in range(1, len(sub))
    ]
    return sum(trs) / len(trs) if trs else bars.iloc[idx]["close"] * 0.005


def load_bars(symbol: str, entry_epoch: float, bars_conn: sqlite3.Connection) -> pd.DataFrame:
    start_e = int(entry_epoch - 3600)
    end_e = int(MARKET_CLOSE_UTC.timestamp() + 300)
    df = pd.read_sql(
        "SELECT ts, open, high, low, close, volume FROM bars "
        "WHERE symbol=? AND tf='1Min' AND ts>=? AND ts<=? ORDER BY ts ASC",
        bars_conn,
        params=(symbol.upper(), start_e, end_e),
    )
    if df.empty:
        return df
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df.set_index("dt", inplace=True)
    return df


def fetch_completed_trades() -> list[dict]:
    conn = sqlite3.connect(str(MASTER_DB))
    cur = conn.cursor()

    cur.execute("SELECT id, ts, mode, payload FROM orders WHERE ts LIKE '2026-08-03%' ORDER BY ts ASC")
    order_rows = cur.fetchall()
    orders = []
    for r in order_rows:
        p = json.loads(r[3]) if r[3] else {}
        orders.append({
            "symbol": p.get("symbol"),
            "side": p.get("side"),
            "ts": r[1],
            "stop_loss": p.get("stop_loss"),
            "take_profit": p.get("take_profit"),
            "strategy": p.get("strategy"),
        })

    cur.execute("SELECT id, symbol, pnl, exit_at, mode, payload FROM trades WHERE exit_at LIKE '2026-08-03%' ORDER BY exit_at ASC")
    trade_rows = cur.fetchall()

    trades = []
    for r in trade_rows:
        p = json.loads(r[5]) if r[5] else {}
        sym = r[1]
        entry_at_str = p.get("entry_at")
        exit_at_str = p.get("exit_at") or r[3]
        entry_px = float(p.get("entry_price"))
        exit_px = float(p.get("exit_price"))
        qty = float(p.get("qty"))
        side_str = p.get("side", "long").lower()
        strat = p.get("strategy") or r[4]
        sl = float(p.get("initial_stop")) if p.get("initial_stop") is not None else None
        risk = float(p.get("initial_risk")) if p.get("initial_risk") is not None else None
        legacy_pnl = float(r[2])
        reason = p.get("exit_reason", "")

        entry_dt = datetime.fromisoformat(entry_at_str.replace("Z", "+00:00")) if entry_at_str else None
        matching_tp = None
        if entry_dt:
            for o in orders:
                if o["symbol"] == sym and o["side"] in ("buy", "sell"):
                    o_dt = datetime.fromisoformat(o["ts"].replace("Z", "+00:00")) if "Z" in o["ts"] else datetime.fromisoformat(o["ts"]).replace(tzinfo=timezone.utc)
                    if abs((o_dt - entry_dt).total_seconds()) <= 60:
                        matching_tp = o.get("take_profit")
                        break

        if matching_tp is None:
            matching_tp = p.get("take_profit")

        if risk is None or risk < 0.001:
            risk = abs(entry_px - sl) if sl is not None else entry_px * 0.01

        trades.append({
            "trade_id": r[0],
            "symbol": sym,
            "strategy": strat,
            "side": side_str,
            "qty": qty,
            "entry_price": entry_px,
            "exit_price": exit_px,
            "entry_at": entry_at_str,
            "exit_at": exit_at_str,
            "stop_loss": sl,
            "take_profit": matching_tp,
            "initial_risk": risk,
            "legacy_pnl": legacy_pnl,
            "legacy_reason": reason,
        })

    conn.close()
    return trades


def replay_trade_with_pis(trade: dict, bars_conn: sqlite3.Connection, config=None) -> dict:
    from app.pis.models import ExitAction, PositionObservation, PISConfig
    from app.pis.position_brain import PositionBrain

    if config is None:
        config = PISConfig()

    symbol = trade["symbol"]
    side_float = 1.0 if trade["side"] == "long" else -1.0
    qty = float(trade["qty"])
    entry_price = float(trade["entry_price"])
    entry_at = datetime.fromisoformat(trade["entry_at"].replace("Z", "+00:00"))
    entry_epoch = entry_at.timestamp()
    initial_stop = float(trade["stop_loss"])
    take_profit = float(trade["take_profit"]) if trade["take_profit"] is not None else None
    risk_per_share = float(trade["initial_risk"])

    bars = load_bars(symbol, entry_epoch, bars_conn)
    if bars.empty:
        return {"status": "BARS_MISSING", "pnl": 0.0}

    entry_ts_pd = pd.Timestamp(entry_at)
    bars_from_entry = bars[bars.index >= entry_ts_pd]
    if bars_from_entry.empty:
        bars_from_entry = bars[bars.index >= (entry_ts_pd - pd.Timedelta(minutes=1))]
        if bars_from_entry.empty:
            return {"status": "NO_BARS_AFTER_ENTRY", "pnl": 0.0}

    idx0 = bars.index.get_loc(bars_from_entry.index[0])
    atr0 = estimate_atr(bars, idx0)
    row0 = bars_from_entry.iloc[0]
    ts0 = bars_from_entry.index[0].to_pydatetime()
    if ts0.tzinfo is None:
        ts0 = ts0.replace(tzinfo=timezone.utc)

    obs0 = PositionObservation(
        timestamp=ts0,
        symbol=symbol,
        side=side_float,
        qty=qty,
        entry_price=entry_price,
        current_price=float(row0["close"]),
        high=float(row0["high"]),
        low=float(row0["low"]),
        close=float(row0["close"]),
        volume=float(row0["volume"]),
        atr=atr0,
        initial_stop=initial_stop,
        risk_per_share=risk_per_share,
        current_stop=initial_stop,
        take_profit=take_profit,
        r_multiple=0.0,
        news_sentiment_score=0.0,
    )

    brain = PositionBrain(symbol, obs0, config)

    current_stop = initial_stop
    new_exit_at = None
    new_exit_price = None
    new_exit_reason = "EOD"
    bar_logs = []

    # Bar iteration
    for i, (bar_ts_pd, row) in enumerate(bars_from_entry.iterrows()):
        bar_ts = bar_ts_pd.to_pydatetime()
        if bar_ts.tzinfo is None:
            bar_ts = bar_ts.replace(tzinfo=timezone.utc)

        idx = bars.index.get_loc(bar_ts_pd)
        atr = estimate_atr(bars, idx)
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        vol = float(row["volume"])
        r_mul = (close - entry_price) * side_float / risk_per_share

        # 1. Intrabar Stop Loss Breach against active current_stop entering this bar
        # (For bar 0, if entry was mid-bar, price already traded at entry_price)
        if i > 0:
            if side_float == 1.0 and low <= current_stop:
                new_exit_at = bar_ts
                new_exit_price = apply_slippage(current_stop, side_float)
                new_exit_reason = f"ACTIVE_STOP_BREACH (${current_stop:.2f})"
                break
            elif side_float == -1.0 and high >= current_stop:
                new_exit_at = bar_ts
                new_exit_price = apply_slippage(current_stop, side_float)
                new_exit_reason = f"ACTIVE_STOP_BREACH (${current_stop:.2f})"
                break

        # 2. Market close check
        if bar_ts >= MARKET_CLOSE_UTC:
            new_exit_price = apply_slippage(close, side_float)
            new_exit_at = bar_ts
            new_exit_reason = "MARKET_CLOSE (EOD)"
            break

        # 3. PIS Observation & Decision
        obs = PositionObservation(
            timestamp=bar_ts,
            symbol=symbol,
            side=side_float,
            qty=qty,
            entry_price=entry_price,
            current_price=close,
            high=high,
            low=low,
            close=close,
            volume=vol,
            atr=atr,
            initial_stop=initial_stop,
            risk_per_share=risk_per_share,
            current_stop=current_stop,
            take_profit=take_profit,
            r_multiple=r_mul,
            news_sentiment_score=0.0,
        )

        decision = brain.observe(obs)
        reasons = [r.value if hasattr(r, "value") else str(r) for r in decision.reason_codes]
        reason_str = ",".join(reasons)

        bar_logs.append({
            "time_et": bar_ts.astimezone(ET).strftime("%H:%M"),
            "close": close,
            "high": high,
            "low": low,
            "r_mul": round(r_mul, 2),
            "peak_r": round(brain.peak_r, 2),
            "active_stop": round(current_stop, 2),
            "new_stop": round(decision.target_stop_loss or current_stop, 2),
            "health": round(decision.health.overall, 2),
            "state": decision.lifecycle_state.value,
            "action": decision.action.value,
            "reasons": reason_str,
        })

        # 4. Check PIS Exit Decision
        if decision.action in (ExitAction.FULL_EXIT, ExitAction.PARTIAL_EXIT):
            new_exit_at = bar_ts
            if "PROTECTION_BREACH" in reason_str:
                new_exit_price = apply_slippage(current_stop if (side_float > 0 and low <= current_stop) or (side_float < 0 and high >= current_stop) else close, side_float)
            elif "HEALTHY_TREND" in reason_str and take_profit and ((side_float > 0 and high >= take_profit) or (side_float < 0 and low <= take_profit)):
                new_exit_price = apply_slippage(take_profit, side_float)
            else:
                new_exit_price = apply_slippage(close, side_float)
            new_exit_reason = f"PIS Exit ({reason_str})"
            break

        # 5. Update active stop for NEXT bar
        if decision.target_stop_loss is not None and decision.target_stop_loss > 0:
            current_stop = decision.target_stop_loss

    brain.close()

    if new_exit_price is None:
        close_bars = bars[bars.index <= MARKET_CLOSE_UTC]
        if not close_bars.empty:
            new_exit_at = close_bars.index[-1].to_pydatetime()
            new_exit_price = apply_slippage(float(close_bars.iloc[-1]["close"]), side_float)
            new_exit_reason = "MARKET_CLOSE (EOD)"

    new_pnl = round((new_exit_price - entry_price) * side_float * qty, 2)
    hold_min = int((new_exit_at - entry_at).total_seconds() / 60) if new_exit_at else 0

    return {
        "status": "OK",
        "exit_at_utc": new_exit_at.isoformat() if new_exit_at else "N/A",
        "exit_at_et": (new_exit_at.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET")) if new_exit_at else "N/A",
        "exit_price": round(new_exit_price, 4) if new_exit_price else None,
        "pnl": new_pnl,
        "hold_min": hold_min,
        "reason": new_exit_reason,
        "bar_logs": bar_logs,
    }


def main():
    trades = fetch_completed_trades()
    bars_conn = sqlite3.connect(str(MASTER_BARS))

    results = []
    print("\n" + "=" * 120)
    print("  EXACT TRADES REPLAY WITH LATEST PIS CODE (2026-08-03)")
    print("=" * 120)

    for i, trade in enumerate(trades, 1):
        res = replay_trade_with_pis(trade, bars_conn)
        trade_res = {**trade, "pis": res}
        results.append(trade_res)

        entry_dt = datetime.fromisoformat(trade["entry_at"].replace("Z", "+00:00"))
        entry_et = entry_dt.astimezone(ET).strftime("%H:%M:%S")
        exit_dt = datetime.fromisoformat(trade["exit_at"].replace("Z", "+00:00"))
        legacy_exit_et = exit_dt.astimezone(ET).strftime("%H:%M:%S")
        pis_exit_et = res["exit_at_et"].split(" ")[1] if res.get("exit_at_et") != "N/A" else "N/A"

        delta_pnl = res["pnl"] - trade["legacy_pnl"]
        print(f"\n[{i:02d}] {trade['symbol']:<5} ({trade['strategy']:<15} {trade['side'].upper():<5} Qty:{trade['qty']})")
        print(f"     Entry:       ${trade['entry_price']:<9.4f} @ {entry_et} ET  |  SL: ${trade['stop_loss']}  |  TP: ${trade['take_profit']}")
        print(f"     Legacy Exit:  ${trade['exit_price']:<9.4f} @ {legacy_exit_et} ET  |  PnL: ${trade['legacy_pnl']:>8.2f}  |  Reason: {trade['legacy_reason'][:40]}")
        print(f"     PIS Exit:     ${res['exit_price']:<9.4f} @ {pis_exit_et} ET  |  PnL: ${res['pnl']:>8.2f}  |  Reason: {res['reason'][:40]}")
        print(f"     Difference:   ${delta_pnl:>+8.2f} {'(BETTER)' if delta_pnl > 0 else '(WORSE)' if delta_pnl < 0 else '(EQUAL)'}")

    bars_conn.close()

    # Summary Table
    print("\n" + "=" * 145)
    print(f"{'#':<3} {'SYM':<5} {'STRATEGY':<15} {'SIDE':<5} {'ENTRY':<8} {'SL':<8} {'TP':<8} "
          f"{'LEG_EXIT':<9} {'LEG_PNL':<9} {'PIS_EXIT':<9} {'PIS_PNL':<9} {'DELTA':<9} {'PIS REASON'}")
    print("-" * 145)

    tot_leg = 0.0
    tot_pis = 0.0

    for i, r in enumerate(results, 1):
        leg_pnl = r["legacy_pnl"]
        pis_pnl = r["pis"]["pnl"]
        delta = pis_pnl - leg_pnl
        tot_leg += leg_pnl
        tot_pis += pis_pnl

        entry_et = datetime.fromisoformat(r["entry_at"].replace("Z", "+00:00")).astimezone(ET).strftime("%H:%M:%S")
        leg_et = datetime.fromisoformat(r["exit_at"].replace("Z", "+00:00")).astimezone(ET).strftime("%H:%M:%S")
        pis_et = r["pis"]["exit_at_et"].split(" ")[1] if r["pis"].get("exit_at_et") != "N/A" else "N/A"
        tp_str = f"{r['take_profit']:.2f}" if r['take_profit'] is not None else "None"
        sl_str = f"{r['stop_loss']:.2f}" if r['stop_loss'] is not None else "None"

        print(f"{i:<3} {r['symbol']:<5} {r['strategy']:<15} {r['side'].upper():<5} {entry_et:<8} {sl_str:<8} {tp_str:<8} "
              f"{leg_et:<9} ${leg_pnl:>7.2f} {pis_et:<9} ${pis_pnl:>7.2f} {delta:>+8.2f}  {r['pis']['reason']}")

    print("-" * 145)
    tot_delta = tot_pis - tot_leg
    print(f"{'TOTAL PORTFOLIO P&L:':<58} ${tot_leg:>8.2f}           ${tot_pis:>8.2f} {tot_delta:>+8.2f}")
    print("=" * 145)


if __name__ == "__main__":
    main()
