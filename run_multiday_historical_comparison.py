import json
import sqlite3
import sys
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(r"C:\Users\peddi\Desktop\Bots\Bot_QA")
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.pis.models import PositionObservation, PISConfig, ExitAction, LifecycleState, TradeHealth
from app.pis.position_brain import PositionBrain

MASTER_DB = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db")
MASTER_BARS = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db")
ET = timezone(timedelta(hours=-4))

def load_bars(symbol: str, entry_ts: float, conn: sqlite3.Connection) -> pd.DataFrame:
    start_ts = entry_ts - 7200  # 2 hours lookback
    end_ts = entry_ts + 28800   # 8 hours forward
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
    if isinstance(current_idx, (slice, list)):
        current_idx = 0
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

class IntelligentTradeHealthEvaluator:
    def evaluate(self, obs: PositionObservation, peak_r: float, trough_r: float, avg_volume: float = 0.0) -> TradeHealth:
        current_r = obs.r_multiple
        if math.isnan(current_r) or math.isinf(current_r): current_r = 0.0
        
        # 1. Structural Trend Retention Intelligence
        if peak_r >= 0.60:
            retention = max(0.0, min(1.0, current_r / peak_r))
            trend_score = retention
        else:
            trend_score = max(0.0, min(1.0, 0.60 + (current_r * 0.50)))

        # 2. Bar Momentum & Wick Rejection Intelligence
        bar_range = obs.high - obs.low
        if bar_range <= 1e-8 or math.isnan(bar_range) or math.isinf(bar_range):
            momentum_score = 0.50
        else:
            if obs.side > 0:
                pos_in_bar = (obs.close - obs.low) / bar_range
            else:
                pos_in_bar = (obs.high - obs.close) / bar_range
            momentum_score = max(0.0, min(1.0, pos_in_bar))

        # 3. Volume-Weighted Distribution Intelligence (RVOL)
        rvol = (obs.volume / avg_volume) if avg_volume > 0 else 1.0
        is_adverse_bar = (obs.side > 0 and obs.close < obs.high) or (obs.side < 0 and obs.close > obs.low)
        
        if is_adverse_bar and rvol > 1.5:
            volume_score = 0.20
        elif is_adverse_bar and rvol < 0.8:
            volume_score = 0.80
        else:
            volume_score = 0.60

        # 4. Volatility Expansion / Contraction
        atr = max(1e-6, obs.atr)
        vol_ratio = bar_range / atr
        if vol_ratio > 3.0:
            volatility_score = 0.30
        elif vol_ratio > 1.8:
            volatility_score = 0.60
        else:
            volatility_score = 0.85

        sentiment_bias = obs.news_sentiment_score * 0.20
        overall = (
            trend_score * 0.45
            + momentum_score * 0.25
            + volume_score * 0.15
            + volatility_score * 0.15
        )
        overall = max(0.0, min(1.0, overall + sentiment_bias))

        return TradeHealth(
            overall=overall,
            trend_score=trend_score,
            momentum_score=momentum_score,
            volatility_score=volatility_score,
            volume_score=volume_score,
            news_sentiment=obs.news_sentiment_score
        )

def run_multiday_benchmark():
    conn_db = sqlite3.connect(str(MASTER_DB))
    df_raw = pd.read_sql("SELECT id, symbol, pnl, exit_at, payload FROM trades ORDER BY exit_at DESC", conn_db)
    conn_db.close()

    conn_bars = sqlite3.connect(str(MASTER_BARS))
    evaluator = IntelligentTradeHealthEvaluator()

    # Parse trades
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
            "strategy": p.get("strategy", "unknown"),
            "date": row["exit_at"][:10] if row["exit_at"] else "unknown"
        })

    df_all = pd.DataFrame(parsed_trades)
    target_dates = sorted([d for d, c in df_all["date"].value_counts().items() if c >= 4 and d != "unknown"], reverse=True)

    results = []

    for target_date in target_dates:
        trades_for_day = df_all[df_all["date"] == target_date]
        day_master_pnl = 0.0
        day_intel_pnl = 0.0
        day_old_pnl = 0.0
        valid_trades_count = 0

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

            valid_trades_count += 1
            avg_vol = float(bars["volume"].mean()) if not bars.empty else 10000.0

            # --- 1. SIMULATE OLD BRITTLE PIS ---
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
            brain_old = PositionBrain(sym, obs0, config)
            stop_old = initial_stop
            exit_old = None

            for i, (bar_ts_pd, row) in enumerate(bars_from_entry.iterrows()):
                bar_ts = bar_ts_pd.to_pydatetime()
                if bar_ts.tzinfo is None: bar_ts = bar_ts.replace(tzinfo=timezone.utc)
                idx_b = i
                atr_b = estimate_atr(bars_from_entry, idx_b)
                close = float(row["close"])
                high = float(row["high"])
                low = float(row["low"])
                r_mul = (close - entry_price) * side_float / risk_per_share

                if i > 0:
                    if side_float == 1.0 and low <= stop_old: exit_old = stop_old; break
                    elif side_float == -1.0 and high >= stop_old: exit_old = stop_old; break

                obs_b = PositionObservation(
                    timestamp=bar_ts, symbol=sym, side=side_float, qty=qty, entry_price=entry_price,
                    current_price=close, high=high, low=low, close=close, volume=float(row["volume"]), atr=atr_b,
                    initial_stop=initial_stop, risk_per_share=risk_per_share, current_stop=stop_old,
                    take_profit=target_tp, r_multiple=r_mul
                )
                dec_b = brain_old.observe(obs_b)
                if dec_b.target_stop_loss is not None and dec_b.target_stop_loss > 0:
                    if side_float > 0 and dec_b.target_stop_loss > stop_old: stop_old = dec_b.target_stop_loss
                    elif side_float < 0 and dec_b.target_stop_loss < stop_old: stop_old = dec_b.target_stop_loss

                if dec_b.action in (ExitAction.FULL_EXIT, ExitAction.PARTIAL_EXIT):
                    reasons = [r.value if hasattr(r, "value") else str(r) for r in dec_b.reason_codes]
                    exit_old = close if "PROTECTION_BREACH" not in reasons else stop_old
                    break
            brain_old.close()
            if exit_old is None: exit_old = float(bars_from_entry.iloc[-1]["close"])
            pnl_old = (exit_old - entry_price) * side_float * qty

            # --- 2. SIMULATE INTELLIGENT PIS ---
            brain_i = PositionBrain(sym, obs0, config)
            stop_i = initial_stop
            exit_i = None
            peak_r = 0.0
            smoothed_health = 0.60

            for i, (bar_ts_pd, row) in enumerate(bars_from_entry.iterrows()):
                bar_ts = bar_ts_pd.to_pydatetime()
                if bar_ts.tzinfo is None: bar_ts = bar_ts.replace(tzinfo=timezone.utc)
                idx_i = i
                atr_i = estimate_atr(bars_from_entry, idx_i)
                close = float(row["close"])
                high = float(row["high"])
                low = float(row["low"])
                r_mul = (close - entry_price) * side_float / risk_per_share
                if r_mul > peak_r: peak_r = r_mul

                if target_tp is not None:
                    if side_float == 1.0 and high >= target_tp: exit_i = target_tp; break
                    elif side_float == -1.0 and low <= target_tp: exit_i = target_tp; break

                if i > 0:
                    if side_float == 1.0 and low <= stop_i: exit_i = stop_i; break
                    elif side_float == -1.0 and high >= stop_i: exit_i = stop_i; break

                obs_i = PositionObservation(
                    timestamp=bar_ts, symbol=sym, side=side_float, qty=qty, entry_price=entry_price,
                    current_price=close, high=high, low=low, close=close, volume=float(row["volume"]), atr=atr_i,
                    initial_stop=initial_stop, risk_per_share=risk_per_share, current_stop=stop_i,
                    take_profit=target_tp, r_multiple=r_mul
                )
                raw_h = evaluator.evaluate(obs_i, peak_r, 0.0, avg_vol)
                smoothed_health = (0.60 * raw_h.overall) + (0.40 * smoothed_health)

                if smoothed_health < 0.28 and r_mul <= -0.25:
                    exit_i = close
                    break

                dec_i = brain_i.observe(obs_i)
                if dec_i.target_stop_loss is not None and dec_i.target_stop_loss > 0:
                    if side_float > 0 and dec_i.target_stop_loss > stop_i: stop_i = dec_i.target_stop_loss
                    elif side_float < 0 and dec_i.target_stop_loss < stop_i: stop_i = dec_i.target_stop_loss
            brain_i.close()
            if exit_i is None: exit_i = float(bars_from_entry.iloc[-1]["close"])
            pnl_intel = (exit_i - entry_price) * side_float * qty

            day_master_pnl += tr["legacy_pnl"]
            day_old_pnl += pnl_old
            day_intel_pnl += pnl_intel

        if valid_trades_count > 0:
            results.append({
                "date": target_date,
                "trades": valid_trades_count,
                "master_pnl": round(day_master_pnl, 2),
                "old_pis_pnl": round(day_old_pnl, 2),
                "intel_pis_pnl": round(day_intel_pnl, 2),
                "delta": round(day_intel_pnl - day_master_pnl, 2)
            })

    conn_bars.close()

    df_res = pd.DataFrame(results)
    print("\n" + "=" * 110)
    print("                 MULTI-DAY HISTORICAL OUT-OF-SAMPLE REPLAY BENCHMARK")
    print("=" * 110)
    print(f"{'DATE':<12} {'TRADES':<8} | {'MASTER LIVE P&L':<18} | {'OLD BRITTLE PIS':<18} | {'INTELLIGENT PIS':<18} | {'DELTA (INTEL vs MASTER)'}")
    print("-" * 110)
    for _, r in df_res.iterrows():
        print(f"{r['date']:<12} {int(r['trades']):<8} | ${r['master_pnl']:>15.2f}  | ${r['old_pis_pnl']:>15.2f}  | ${r['intel_pis_pnl']:>15.2f}  | {r['delta']:>+15.2f}")
    print("=" * 110)
    tot_m = df_res['master_pnl'].sum()
    tot_o = df_res['old_pis_pnl'].sum()
    tot_i = df_res['intel_pis_pnl'].sum()
    print(f"{'CUMULATIVE TOTAL:':<21} | ${tot_m:>15.2f}  | ${tot_o:>15.2f}  | ${tot_i:>15.2f}  | {tot_i - tot_m:>+15.2f}")
    print("=" * 110)

if __name__ == "__main__":
    run_multiday_benchmark()
