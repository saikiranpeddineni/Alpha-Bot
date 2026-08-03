"""
Aug 03, 2026 – Targeted PIS Replay for 12 Specific Trades
===========================================================
Pulls the EXACT 12 completed trades from Aug 03 from the Master Bot DB,
replays each through the new QA PIS code tick-by-tick using real 1-minute bars,
and incorporates news sentiment from the shadow_ledger payload.

NO VALUES ARE HARDCODED. All data from DB or live code.
"""
from __future__ import annotations

import json, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND   = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

MASTER_ALPHADESK = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db")
QA_BARS_DB       = REPO_ROOT / "data" / "bars_cache.db"
OUTPUT_CSV       = Path(__file__).parent / "aug03_replay_results.csv"

# -----------------------------------------------------------------------
# The 12 specific Aug 03 trades to replay (identified by symbol+strategy+date)
# Matched exactly from the dashboard data the user provided
# -----------------------------------------------------------------------
TARGET_TRADES = [
    # (symbol, side, strategy, approx_entry_utc_prefix)  - for matching from DB
    ("INTC",  "short", "orb",           "2026-08-03T13:4"),
    ("CRWV",  "long",  "orb",           "2026-08-03T13:5"),
    ("NBIS",  "long",  "momentum",      "2026-08-03T13:5"),
    ("NOW",   "long",  "rsi2_reversion","2026-08-03T14:0"),
    ("SPCX",  "long",  "momentum",      "2026-08-03T14:2"),
    ("META",  "long",  "rsi2_reversion","2026-08-03T14:3"),
    ("TQQQ",  "long",  "rsi2_reversion","2026-08-03T14:4"),
    ("AMZN",  "long",  "rsi2_reversion","2026-08-03T14:5"),
    ("SPCX",  "long",  "orb",           "2026-08-03T15:0"),
    ("BABA",  "long",  "orb",           "2026-08-03T15:2"),
    ("NVDA",  "long",  "rsi2_reversion","2026-08-03T16:0"),
    ("INTC",  "long",  "bb_squeeze",    "2026-08-03T16:1"),
]


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


def load_bars(symbol: str, entry_epoch: float, bars_conn: sqlite3.Connection) -> pd.DataFrame:
    """Load bars from 30 min before entry to EOD (~7h window)."""
    start_e = entry_epoch - 1800
    end_e   = entry_epoch + 25200   # +7h = covers full remaining session
    df = pd.read_sql(
        "SELECT ts, open, high, low, close, volume FROM bars "
        "WHERE symbol=? AND tf='1Min' AND ts>=? AND ts<=? ORDER BY ts ASC",
        bars_conn, params=(symbol.upper(), int(start_e), int(end_e))
    )
    if df.empty:
        return df
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df.set_index("dt", inplace=True)
    return df


def get_news_sentiment(symbol: str, entry_at: datetime, mconn: sqlite3.Connection) -> float:
    """
    Pull any news sentiment from shadow_ledger for this symbol around entry time.
    Falls back to 0.0 if no data.
    """
    try:
        from app.pis.sentiment import NewsSentimentAnalyzer
        analyzer = NewsSentimentAnalyzer()

        # Look in shadow_ledger for gate_evidence_json or payload with vix/sentiment data
        # within ±2 hours of entry
        entry_epoch = entry_at.timestamp()
        start_epoch = entry_epoch - 7200
        end_epoch   = entry_epoch + 7200

        df_sl = pd.read_sql(
            """SELECT payload FROM shadow_ledger
               WHERE symbol=? AND ts >= datetime(?, 'unixepoch') AND ts <= datetime(?, 'unixepoch')
               ORDER BY ts DESC LIMIT 5""",
            mconn,
            params=(symbol.upper(),
                    int(start_epoch),
                    int(end_epoch))
        )

        headlines = []
        for _, row in df_sl.iterrows():
            p = json.loads(row["payload"]) if isinstance(row["payload"], str) else {}
            # Check for sentiment data in gate_evidence
            ge = p.get("gate_evidence", {})
            if isinstance(ge, str):
                try:
                    ge = json.loads(ge)
                except Exception:
                    ge = {}
            news_items = ge.get("news_items", []) or ge.get("news", [])
            for item in news_items:
                if isinstance(item, dict):
                    h = item.get("headline") or item.get("title") or item.get("summary", "")
                    if h:
                        headlines.append(h)

        if headlines:
            return analyzer.analyze_headlines(headlines)
        return 0.0
    except Exception:
        return 0.0


def replay_one_trade(trade: dict, bars_conn: sqlite3.Connection, mconn: sqlite3.Connection) -> dict:
    from app.pis.models import PISConfig, ExitAction, PositionObservation
    from app.pis.position_brain import PositionBrain

    symbol      = trade["symbol"]
    side_float  = 1.0 if trade["side"].lower() == "long" else -1.0
    qty         = float(trade["qty"])
    entry_price = float(trade["entry_price"])

    entry_at = datetime.fromisoformat(trade["entry_at"].replace("Z", "+00:00"))
    exit_at  = datetime.fromisoformat(trade["exit_at"].replace("Z", "+00:00"))

    entry_epoch = entry_at.timestamp()

    raw_stop = trade.get("stop_loss") or trade.get("initial_stop")
    initial_stop = float(raw_stop) if raw_stop and float(raw_stop) > 0 else (
        entry_price * (0.99 if side_float == 1.0 else 1.01)
    )
    risk_per_share = abs(entry_price - initial_stop)
    if risk_per_share < 0.001:
        risk_per_share = entry_price * 0.01

    raw_tp = trade.get("take_profit")
    take_profit = float(raw_tp) if raw_tp and float(raw_tp) > 0 else None

    # Get news sentiment for this symbol near entry time
    news_sentiment = get_news_sentiment(symbol, entry_at, mconn)

    bars = load_bars(symbol, entry_epoch, bars_conn)
    if bars.empty:
        return {"status": "BARS_MISSING", "news_sentiment": news_sentiment}

    entry_ts_pd    = pd.Timestamp(entry_at)
    bars_from_entry = bars[bars.index >= entry_ts_pd]
    if bars_from_entry.empty:
        return {"status": "NO_BARS_AFTER_ENTRY", "news_sentiment": news_sentiment}

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
            current_stop=initial_stop, take_profit=take_profit,
            r_multiple=0.0, news_sentiment_score=news_sentiment
        )
        brain = PositionBrain(symbol, obs0, config)

        current_stop     = initial_stop
        new_exit_at      = None
        new_exit_price   = None
        new_exit_reason  = "EOD_NO_PIS_EXIT"
        bars_held        = 0

        for bar_ts_pd, row in bars_from_entry.iloc[1:].iterrows():
            bar_ts = bar_ts_pd.to_pydatetime()
            if bar_ts.tzinfo is None:
                bar_ts = bar_ts.replace(tzinfo=timezone.utc)

            idx = bars.index.get_loc(bar_ts_pd)
            atr = _estimate_atr(bars, idx)
            close = float(row["close"])
            r_mul = (close - entry_price) * side_float / risk_per_share

            obs = PositionObservation(
                timestamp=bar_ts, symbol=symbol, side=side_float, qty=qty,
                entry_price=entry_price, current_price=close,
                high=float(row["high"]), low=float(row["low"]), close=close,
                volume=float(row["volume"]), atr=atr,
                initial_stop=initial_stop, risk_per_share=risk_per_share,
                current_stop=current_stop, take_profit=take_profit,
                r_multiple=r_mul, news_sentiment_score=news_sentiment
            )

            decision = brain.observe(obs)
            bars_held += 1

            if decision.target_stop_loss and decision.target_stop_loss != current_stop:
                current_stop = decision.target_stop_loss

            action = decision.action
            should_exit = action in (ExitAction.FULL_EXIT, ExitAction.PARTIAL_EXIT)
            if should_exit:
                new_exit_at    = bar_ts
                new_exit_price = close
                reasons = [r.value if hasattr(r, "value") else str(r) for r in decision.reason_codes]
                new_exit_reason = ",".join(reasons)
                break

            # Hard stop
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
            last = bars_from_entry.iloc[-1]
            new_exit_at    = bars_from_entry.index[-1].to_pydatetime()
            new_exit_price = float(last["close"])
            new_exit_reason = "EOD_NO_PIS_EXIT"
            bars_held = len(bars_from_entry) - 1

        new_pnl = round((new_exit_price - entry_price) * side_float * qty, 2)

        # Duration comparison
        legacy_mins = int((exit_at - entry_at).total_seconds() / 60)
        new_exit_dt = new_exit_at if isinstance(new_exit_at, datetime) else entry_at
        new_mins    = int((new_exit_dt - entry_at).total_seconds() / 60)

        return {
            "status":          "OK",
            "new_exit_at":     new_exit_at,
            "new_exit_price":  round(new_exit_price, 4),
            "new_pnl":         new_pnl,
            "new_exit_reason": new_exit_reason,
            "bars_held":       bars_held,
            "legacy_mins":     legacy_mins,
            "new_mins":        new_mins,
            "news_sentiment":  news_sentiment,
        }

    except Exception as exc:
        return {"status": f"ERROR:{exc}", "news_sentiment": news_sentiment}


# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------
def main():
    print("=" * 100)
    print("  Aug 03, 2026 – Targeted PIS Replay for 12 Specific Trades")
    print("  Entry: exact from DB  |  Exit: determined by new PIS code  |  News sentiment: included")
    print("=" * 100)

    mconn  = sqlite3.connect(str(MASTER_ALPHADESK))
    bconn  = sqlite3.connect(str(QA_BARS_DB))

    # Pull all Aug 03 paper strategy trades from DB
    df_raw = pd.read_sql(
        "SELECT id, mode, symbol, pnl, exit_at, payload FROM trades "
        "WHERE exit_at LIKE '2026-08-03%' AND mode='paper' ORDER BY exit_at ASC;",
        mconn
    )
    print(f"\nFound {len(df_raw)} completed paper trades on Aug 03 in Master DB")

    # Parse payloads
    records = []
    for _, row in df_raw.iterrows():
        p = json.loads(row["payload"]) if isinstance(row["payload"], str) else {}
        records.append({
            "trade_id":    row["id"],
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
            "legacy_pnl":  float(row["pnl"]),
            "legacy_reason": p.get("exit_reason", ""),
        })

    # Match the 12 target trades
    matched = []
    for (sym, side, strat, entry_prefix) in TARGET_TRADES:
        for r in records:
            if (r["symbol"] == sym
                    and r["side"].lower() == side.lower()
                    and r["strategy"].lower() == strat.lower()
                    and str(r.get("entry_at", "")).startswith(entry_prefix)
                    and r["entry_price"] and r["exit_price"]
                    and float(r["qty"]) > 0):
                # Avoid duplicates
                if not any(m["trade_id"] == r["trade_id"] for m in matched):
                    matched.append(r)
                    break

    print(f"Matched {len(matched)}/12 target trades from DB\n")

    if len(matched) < 12:
        unmatched = []
        for i, (sym, side, strat, prefix) in enumerate(TARGET_TRADES):
            found = any(
                m["symbol"] == sym and m["side"].lower() == side.lower()
                and m["strategy"].lower() == strat.lower()
                for m in matched
            )
            if not found:
                unmatched.append(f"  {sym} {side} {strat} ({prefix})")
        print("WARNING – unmatched trades:")
        for u in unmatched:
            print(u)
        print()

    # Print header
    print(f"\n{'#':>2} {'SYM':<5} {'SIDE':<5} {'QTY':>4} {'STRAT':<15} {'SENT':>5} "
          f"{'ENTRY_AT (ET)':>15} {'ENT_PRICE':>9} "
          f"{'LEG_EXIT (ET)':>15} {'LEG_PRICE':>9} {'LEG_PNL':>8} {'LEG_REASON':<25} "
          f"{'NEW_EXIT (ET)':>15} {'NEW_PRICE':>9} {'NEW_PNL':>8} "
          f"{'DELTA':>7} {'NEW_REASON'}")
    print("-" * 195)

    results = []
    for i, trade in enumerate(matched, 1):
        sym   = trade["symbol"]
        strat = trade["strategy"]
        print(f"  [{i:2d}/12] Replaying {sym:<5} {strat:<15} {str(trade['entry_at'])[:19]} ...",
              end="", flush=True)

        replay = replay_one_trade(trade, bconn, mconn)

        entry_at_utc = datetime.fromisoformat(trade["entry_at"].replace("Z", "+00:00"))
        exit_at_utc  = datetime.fromisoformat(trade["exit_at"].replace("Z", "+00:00"))

        # Convert to ET (UTC-4) for display
        from datetime import timedelta
        ET_OFFSET = timedelta(hours=-4)
        def to_et(dt):
            if dt is None:
                return "N/A"
            if hasattr(dt, "tzinfo") and dt.tzinfo:
                return (dt + ET_OFFSET).strftime("%H:%M:%S")
            return dt.strftime("%H:%M:%S")

        leg_et  = to_et(exit_at_utc)
        new_et  = to_et(replay.get("new_exit_at"))
        ent_et  = to_et(entry_at_utc)

        leg_pnl   = trade["legacy_pnl"]
        new_pnl   = replay.get("new_pnl", 0.0) or 0.0
        pnl_delta = round(new_pnl - leg_pnl, 2)
        sentiment = replay.get("news_sentiment", 0.0)

        results.append({
            "symbol":          sym,
            "side":            trade["side"],
            "qty":             trade["qty"],
            "strategy":        strat,
            "news_sentiment":  sentiment,
            "entry_at_et":     ent_et,
            "entry_price":     trade["entry_price"],
            "legacy_exit_et":  leg_et,
            "legacy_price":    trade["exit_price"],
            "legacy_pnl":      leg_pnl,
            "legacy_reason":   trade["legacy_reason"],
            "new_exit_et":     new_et,
            "new_price":       replay.get("new_exit_price"),
            "new_pnl":         new_pnl,
            "pnl_delta":       pnl_delta,
            "new_reason":      replay.get("new_exit_reason", replay.get("status", "ERROR")),
            "legacy_hold_min": replay.get("legacy_mins", 0),
            "new_hold_min":    replay.get("new_mins", 0),
        })

        status_str = f"legacy={leg_pnl:+.2f}  new={new_pnl:+.2f}  delta={pnl_delta:+.2f}"
        print(f"  {status_str}")

    bconn.close()
    mconn.close()

    # Final table
    print("\n" + "=" * 175)
    print(f"{'#':>2} {'SYM':<5} {'SIDE':<5} {'QTY':>4} {'STRAT':<15} {'SENT':>5} "
          f"{'ENT_ET':>8} {'ENT_$':>9} "
          f"{'LEG_ET':>8} {'LEG_$':>9} {'LEG_PNL':>8} {'LEG_REASON':<30} "
          f"{'NEW_ET':>8} {'NEW_$':>9} {'NEW_PNL':>8} "
          f"{'DELTA':>7} NEW_REASON")
    print("-" * 175)

    total_leg = 0.0
    total_new = 0.0

    for i, r in enumerate(results, 1):
        leg_pnl = float(r["legacy_pnl"])
        new_pnl = float(r["new_pnl"])
        total_leg += leg_pnl
        total_new += new_pnl

        delta_sign = "▲" if r["pnl_delta"] > 0 else ("▼" if r["pnl_delta"] < 0 else "=")

        print(f"{i:>2} {r['symbol']:<5} {r['side']:<5} {int(float(r['qty'])):>4} {r['strategy']:<15} {r['news_sentiment']:>+5.2f} "
              f"{r['entry_at_et']:>8} {float(r['entry_price']):>9.4f} "
              f"{r['legacy_exit_et']:>8} {float(r['legacy_price']):>9.4f} {leg_pnl:>8.2f} {str(r['legacy_reason'])[:30]:<30} "
              f"{str(r['new_exit_et']):>8} {str(r['new_price'] or 'N/A'):>9} {new_pnl:>8.2f} "
              f"{delta_sign}{r['pnl_delta']:>+6.2f} {str(r['new_reason'])[:35]}")

    print("=" * 175)
    print(f"\n  LEGACY TOTAL P&L (these 12 trades) : ${total_leg:>10,.2f}")
    print(f"  NEW PIS TOTAL P&L (same 12 trades) : ${total_new:>10,.2f}")
    print(f"  NET IMPROVEMENT                     : ${total_new - total_leg:>+10,.2f}")
    print(f"\n  Note: Exit times in Eastern Time (ET). New exit determined by PIS code, not hardcoded.")

    # Save CSV
    pd.DataFrame(results).to_csv(str(OUTPUT_CSV), index=False)
    print(f"\n  Full CSV: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
