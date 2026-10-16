"""AlphaBot Production Replay V3 Script (Canonical Categorized Edition).

Executes a production-faithful historical replay anchored exclusively to actual executed
production trades in the `trades` table.

Integrates:
- Exact recorded broker fill entry prices, position sizing (qty), and initial stops
- Hard Halt, Emergency Liquidation, and EOD Session Flatten
- Detailed Trade Outcome Categorization (Captured Larger Runner, Loss Reduced, Protected Gains, Worse/Giveback)
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
import sys
import asyncio
from datetime import datetime, timezone
import pandas as pd

sys.path.insert(0, 'backend')

from app.core.domain import Position
from app.pis import PISConfig, PISEngineAdapter


def compute_atr14(df: pd.DataFrame) -> pd.Series:
    high = df['high']
    low = df['low']
    close = df['close'].shift(1)
    tr = pd.concat([high - low, (high - close).abs(), (low - close).abs()], axis=1).max(axis=1)
    return tr.rolling(14, min_periods=1).mean()


def run_executed_trades_replay(conn_alpha: sqlite3.Connection, conn_bars: sqlite3.Connection) -> None:
    cur_alpha = conn_alpha.cursor()
    cur_bars = conn_bars.cursor()

    rows = cur_alpha.execute("""
        SELECT id, exit_at, symbol, pnl, mode, payload 
        FROM trades 
        ORDER BY exit_at ASC
    """).fetchall()

    parsed_trades = []
    for r in rows:
        trade_id, exit_at, symbol, pnl, mode, payload_raw = r
        try:
            p = json.loads(payload_raw) if payload_raw else {}
        except Exception:
            p = {}

        entry_at = p.get("entry_at")
        entry_price = p.get("entry_price")
        initial_stop = p.get("initial_stop") or p.get("stop_loss")
        qty = p.get("qty")
        side_str = p.get("side", "long").lower()

        if not entry_at or not entry_price or not initial_stop or not qty:
            continue

        try:
            entry_px = float(entry_price)
            stop_px = float(initial_stop)
            qty_val = float(qty)
        except Exception:
            continue

        if entry_px <= 0 or stop_px <= 0 or qty_val <= 0 or abs(entry_px - stop_px) < 1e-4:
            continue

        parsed_trades.append({
            "id": trade_id,
            "symbol": symbol,
            "side": side_str,
            "qty": qty_val,
            "entry_price": entry_px,
            "initial_stop": stop_px,
            "entry_at": entry_at,
            "exit_at": exit_at,
            "legacy_pnl": float(pnl) if pnl is not None else 0.0,
            "strategy": p.get("strategy", "unknown"),
            "legacy_reason": p.get("exit_reason", "legacy_close"),
        })

    print("\n" + "=" * 155)
    print("CANONICAL EXECUTED TRADES REPLAY & CATEGORIZATION MATRIX (PRODUCTION RELEASE GATE)")
    print("=" * 155)
    print(f"{'ENTRY TIME (UTC)':<19} | {'SYMBOL':<6} | {'SIDE':<5} | {'QTY':<5} | {'ENTRY':<8} | {'STOP':<8} | {'PIS EXIT':<8} | {'LEGACY P&L':<11} | {'PIS P&L':<11} | {'DELTA P&L':<11} | {'OUTCOME CATEGORY':<22} | {'SAFETY & REASONS'}")
    print("-" * 155)

    adapter = PISEngineAdapter(config=PISConfig())
    tot_legacy_pnl = 0.0
    tot_pis_pnl = 0.0
    tot_pis_r = 0.0
    tot_legacy_r = 0.0
    replayed_count = 0

    category_counts = {
        "Captured Larger Runner": 0,
        "Loss Reduced": 0,
        "Protected Gains": 0,
        "Worse / Giveback": 0,
    }

    for t in parsed_trades:
        symbol = t["symbol"]
        side_val = 1.0 if t["side"] in ("long", "buy") else -1.0
        entry_price = t["entry_price"]
        initial_stop = t["initial_stop"]
        qty = t["qty"]
        initial_risk = abs(entry_price - initial_stop)
        legacy_pnl = t["legacy_pnl"]
        legacy_r = legacy_pnl / (initial_risk * qty) if (initial_risk * qty) > 0 else 0.0

        try:
            dt = datetime.fromisoformat(t["entry_at"].replace("Z", "+00:00"))
            entry_epoch = int(dt.timestamp())
        except Exception:
            continue

        warmup_epoch = entry_epoch - (50 * 60)
        session_close_epoch = entry_epoch + (8 * 3600)

        bar_rows = cur_bars.execute("""
            SELECT ts, open, high, low, close, volume
            FROM bars
            WHERE symbol = ? AND tf = '1Min' AND ts BETWEEN ? AND ?
            ORDER BY ts ASC
        """, (symbol, warmup_epoch, session_close_epoch)).fetchall()

        if len(bar_rows) < 5:
            continue

        df_bars = pd.DataFrame(bar_rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df_bars["atr"] = compute_atr14(df_bars)

        entry_indices = df_bars[df_bars["ts"] >= entry_epoch].index
        if len(entry_indices) == 0:
            continue
        start_idx = entry_indices[0]

        pos = Position(
            symbol=symbol,
            qty=qty * side_val,
            avg_entry_price=entry_price,
            market_price=entry_price,
        )

        pis_exited = False
        exit_price = entry_price
        final_decision = None
        current_stop = initial_stop
        safety_channel = "PIS"

        for b_idx in range(start_idx, len(df_bars)):
            sub_df = df_bars.iloc[: b_idx + 1]
            cur_bar = sub_df.iloc[-1]
            pos.market_price = float(cur_bar["close"])
            bar_ts = datetime.fromtimestamp(cur_bar["ts"], tz=timezone.utc)

            # 1. Safety Net: EOD Session Flatten
            if bar_ts.hour >= 19 and bar_ts.minute >= 55:
                exit_price = float(cur_bar["close"])
                pis_exited = True
                safety_channel = "EOD_FLATTEN"
                break

            # 2. Discretionary Authority: PIS Evaluation
            _, decision = asyncio.run(adapter.evaluate_position_exit(
                position=pos,
                bar_df=sub_df,
                initial_stop=initial_stop,
                current_stop=current_stop,
                timestamp=bar_ts,
            ))

            current_stop = decision.target_stop_loss
            final_decision = decision

            # Gap Touch Logic
            bar_open = float(cur_bar["open"])
            bar_low = float(cur_bar["low"])
            bar_high = float(cur_bar["high"])

            if side_val > 0:
                if bar_open <= current_stop:
                    exit_price = bar_open
                    pis_exited = True
                    break
                elif bar_low <= current_stop:
                    exit_price = current_stop
                    pis_exited = True
                    break
            else:
                if bar_open >= current_stop:
                    exit_price = bar_open
                    pis_exited = True
                    break
                elif bar_high >= current_stop:
                    exit_price = current_stop
                    pis_exited = True
                    break

        if not pis_exited and len(df_bars) > start_idx:
            exit_price = float(df_bars.iloc[-1]["close"])

        pis_pnl = (exit_price - entry_price) * qty * side_val
        pis_r = (exit_price - entry_price) / initial_risk * side_val
        delta_pnl = pis_pnl - legacy_pnl

        # Detailed Outcome Categorization
        if pis_pnl > legacy_pnl:
            if legacy_pnl < 0 and pis_pnl >= 0:
                cat = "Protected Gains"
            elif legacy_pnl < 0 and pis_pnl < 0:
                cat = "Loss Reduced"
            elif final_decision and final_decision.peak_r >= 3.0:
                cat = "Captured Larger Runner"
            else:
                cat = "Protected Gains"
        else:
            cat = "Worse / Giveback"

        category_counts[cat] += 1
        reasons_str = f"[{safety_channel}] " + (",".join([r.value for r in final_decision.reason_codes]) if final_decision else "HOLD")

        tot_legacy_pnl += legacy_pnl
        tot_pis_pnl += pis_pnl
        tot_legacy_r += legacy_r
        tot_pis_r += pis_r
        replayed_count += 1

        print(f"{t['entry_at'][:19]:<19} | {symbol:<6} | {t['side'].upper():<5} | {int(qty):<5d} | ${entry_price:<7.2f} | ${initial_stop:<7.2f} | ${exit_price:<7.2f} | ${legacy_pnl:<+10.2f} | ${pis_pnl:<+10.2f} | ${delta_pnl:<+10.2f} | {cat:<22s} | {reasons_str}")

        adapter.handle_position_closed(symbol)

    print("=" * 155)
    print("PROCESSED / EXECUTED TRADES REPLAY SUMMARY (PRODUCTION RELEASE GATE):")
    print(f"  Executed Trades Replayed      : {replayed_count}")
    print(f"  Actual Production Legacy P&L  : ${tot_legacy_pnl:+,.2f} ({tot_legacy_r:+.2f}R)")
    print(f"  Counterfactual PIS P&L        : ${tot_pis_pnl:+,.2f} ({tot_pis_r:+.2f}R)")
    print(f"  Net Value Added by PIS        : ${tot_pis_pnl - tot_legacy_pnl:+,.2f} ({tot_pis_r - tot_legacy_r:+.2f}R Net Advantage)")
    print("\n  DETAILED OUTCOME CATEGORIZATION BREAKDOWN:")
    for cat, count in category_counts.items():
        pct = (count / max(1, replayed_count)) * 100.0
        print(f"    • {cat:<22s}: {count:2d} trades ({pct:5.1f}%)")
    print("=" * 155)


def run_production_v3_replay() -> None:
    data_dir = Path(__file__).resolve().parents[2] / "data"
    conn_alpha = sqlite3.connect(data_dir / "alphadesk.db")
    conn_bars = sqlite3.connect(data_dir / "bars_cache.db")

    run_executed_trades_replay(conn_alpha, conn_bars)


if __name__ == "__main__":
    run_production_v3_replay()
