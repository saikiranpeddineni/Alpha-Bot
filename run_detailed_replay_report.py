"""
Comprehensive PIS Replay with Detailed Minute-by-Minute Breakdown
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(r"C:\Users\peddi\Desktop\Bots\Bot_QA")
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.pis.models import ExitAction, PositionObservation, PISConfig
from app.pis.position_brain import PositionBrain

MASTER_DB = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db")
MASTER_BARS = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db")

SLIPPAGE_BPS = 2
MARKET_CLOSE_UTC = datetime(2026, 8, 3, 20, 0, 0, tzinfo=timezone.utc)
ET = timezone(timedelta(hours=-4))

SENTIMENTS = {'INTC': 0.09, 'CRWV': -0.02, 'NBIS': 0.0, 'NOW': 0.0, 'SPCX': 0.0, 'META': 0.32, 'TQQQ': 0.0, 'AMZN': -0.16, 'BABA': -0.23, 'NVDA': 0.0}

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

def replay_trade(trade: dict, bars_conn: sqlite3.Connection, use_sentiment: bool = True) -> dict:
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
    sentiment_score = SENTIMENTS.get(symbol, 0.0) if use_sentiment else 0.0

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
        news_sentiment_score=sentiment_score,
    )

    brain = PositionBrain(symbol, obs0, config)
    current_stop = initial_stop
    new_exit_at = None
    new_exit_price = None
    new_exit_reason = "EOD"
    bar_logs = []

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

        if bar_ts >= MARKET_CLOSE_UTC:
            new_exit_price = apply_slippage(close, side_float)
            new_exit_at = bar_ts
            new_exit_reason = "MARKET_CLOSE (EOD)"
            break

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
            news_sentiment_score=sentiment_score,
        )

        decision = brain.observe(obs)
        reasons = [r.value if hasattr(r, "value") else str(r) for r in decision.reason_codes]
        reason_str = ",".join(reasons)

        bar_logs.append({
            "bar_idx": i + 1,
            "time_et": bar_ts.astimezone(ET).strftime("%H:%M:%S"),
            "open": round(float(row["open"]), 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(close, 2),
            "volume": int(vol),
            "r_mul": round(r_mul, 2),
            "peak_r": round(brain.peak_r, 2),
            "active_stop": round(current_stop, 2),
            "new_stop": round(decision.target_stop_loss or current_stop, 2),
            "health": round(decision.health.overall, 2),
            "state": decision.lifecycle_state.value,
            "action": decision.action.value,
            "reasons": reason_str,
        })

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
        "exit_at_et": (new_exit_at.astimezone(ET).strftime("%H:%M:%S ET")) if new_exit_at else "N/A",
        "exit_price": round(new_exit_price, 4) if new_exit_price else None,
        "pnl": new_pnl,
        "hold_min": hold_min,
        "reason": new_exit_reason,
        "bar_logs": bar_logs,
    }

def run_all():
    trades = fetch_completed_trades()
    bars_conn = sqlite3.connect(str(MASTER_BARS))

    print(f"Loaded {len(trades)} completed trades from 2026-08-03.")
    all_data = []

    for t in trades:
        pis_res = replay_trade(t, bars_conn, use_sentiment=True)
        all_data.append({
            "trade": t,
            "pis": pis_res,
        })

    bars_conn.close()

    # Write full JSON artifact for inspection
    with open("replay_output.json", "w") as f:
        json.dump(all_data, f, indent=2)
    print("Replay completed successfully. Output saved to replay_output.json")

if __name__ == "__main__":
    run_all()
