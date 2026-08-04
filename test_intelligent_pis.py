import math
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(r"C:\Users\peddi\Desktop\Bots\Bot_QA")
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.pis.models import PositionObservation, PISConfig, ExitAction, LifecycleState, TradeHealth, ThesisDrift, PISDecision
from app.pis.position_brain import PositionBrain

MASTER_BARS = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db")
ET = timezone(timedelta(hours=-4))
SENTIMENTS = {'INTC': 0.09, 'CRWV': -0.02, 'NBIS': 0.0, 'NOW': 0.0, 'SPCX': 0.0, 'META': 0.32, 'TQQQ': 0.0, 'AMZN': -0.16, 'BABA': -0.23, 'NVDA': 0.0}

from run_detailed_replay_report import fetch_completed_trades, load_bars, estimate_atr

class IntelligentTradeHealthEvaluator:
    """
    Intelligent Market Structure Evaluator:
    1. Trend Health distinguishes between Early Base Building (Peak R < 0.5R) vs Developed Trends (Peak R >= 0.5R).
    2. Momentum distinguishes between High-Volume Distribution vs Low-Volume Normal Pullbacks.
    3. Respects News Sentiment as structural alpha bias.
    """
    def evaluate(self, obs: PositionObservation, peak_r: float, trough_r: float, avg_volume: float = 0.0) -> TradeHealth:
        current_r = obs.r_multiple
        if math.isnan(current_r) or math.isinf(current_r): current_r = 0.0
        
        # 1. Structural Trend Retention Intelligence
        if peak_r >= 0.60:
            # Established Trend: measure how much of the developed gain is preserved
            retention = max(0.0, min(1.0, current_r / peak_r))
            trend_score = retention
        else:
            # Base Building / Discovery: measure safety relative to initial risk boundary (-1.0R to +0.6R)
            # A trade at 0.0R (breakeven) is 0.60 healthy, at -0.3R is 0.40 healthy, at -0.8R is 0.15 healthy
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
        # If volume is light on a down-bar, it's low-volume absorption (healthy), NOT distribution!
        # True distribution requires heavy volume selling.
        rvol = (obs.volume / avg_volume) if avg_volume > 0 else 1.0
        is_adverse_bar = (obs.side > 0 and obs.close < obs.high) or (obs.side < 0 and obs.close > obs.low)
        
        if is_adverse_bar and rvol > 1.5:
            # Heavy volume against position: distribution confirmed
            volume_score = 0.20
        elif is_adverse_bar and rvol < 0.8:
            # Light volume pullback: healthy consolidation
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

        # Weighted Composite
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

def run_intelligent_pis_simulation():
    trades = fetch_completed_trades()
    bars_conn = sqlite3.connect(str(MASTER_BARS))

    print("=" * 145)
    print("  INTELLIGENT PIS SIMULATION (NO ARBITRARY TIMERS, FULL MARKET-STRUCTURE AWARENESS)")
    print("=" * 145)
    print(f"{'#':<3} {'SYM':<5} {'STRAT':<14} {'SIDE':<5} | {'MODEL A (MASTER)':<18} | {'INTELLIGENT PIS':<26} | {'DELTA':<10} | {'EXIT REASON & TIMING'}")
    print("-" * 145)

    tot_master = 0.0
    tot_intel = 0.0
    wins_m = wins_i = 0
    evaluator = IntelligentTradeHealthEvaluator()

    for idx, trade in enumerate(trades, 1):
        sym = trade["symbol"]
        side_str = trade["side"].upper()
        side_float = 1.0 if trade["side"] == "long" else -1.0
        qty = float(trade["qty"])
        entry_price = float(trade["entry_price"])
        entry_at = datetime.fromisoformat(trade["entry_at"].replace("Z", "+00:00"))
        initial_stop = float(trade["stop_loss"])
        target_tp = float(trade["take_profit"]) if trade["take_profit"] is not None else None
        risk_per_share = float(trade["initial_risk"])
        sentiment_score = SENTIMENTS.get(sym, 0.0)

        bars = load_bars(sym, entry_at.timestamp(), bars_conn)
        entry_ts_pd = pd.Timestamp(entry_at)
        bars_from_entry = bars[bars.index >= entry_ts_pd]
        if bars_from_entry.empty:
            bars_from_entry = bars[bars.index >= (entry_ts_pd - pd.Timedelta(minutes=1))]

        # Compute rolling average volume
        avg_vol = float(bars["volume"].mean()) if not bars.empty else 10000.0

        config = PISConfig()
        row0 = bars_from_entry.iloc[0]
        ts0 = bars_from_entry.index[0].to_pydatetime()
        if ts0.tzinfo is None: ts0 = ts0.replace(tzinfo=timezone.utc)

        obs0 = PositionObservation(
            timestamp=ts0, symbol=sym, side=side_float, qty=qty, entry_price=entry_price,
            current_price=float(row0["close"]), high=float(row0["high"]), low=float(row0["low"]), close=float(row0["close"]),
            volume=float(row0["volume"]), atr=1.0, initial_stop=initial_stop, risk_per_share=risk_per_share,
            current_stop=initial_stop, take_profit=target_tp, r_multiple=0.0, news_sentiment_score=sentiment_score
        )

        brain = PositionBrain(sym, obs0, config)
        current_stop = initial_stop
        exit_px = None
        exit_at = None
        exit_reason = None
        peak_r = 0.0
        smoothed_health = 0.60

        for i, (bar_ts_pd, row) in enumerate(bars_from_entry.iterrows()):
            bar_ts = bar_ts_pd.to_pydatetime()
            if bar_ts.tzinfo is None: bar_ts = bar_ts.replace(tzinfo=timezone.utc)
            bar_idx = bars.index.get_loc(bar_ts_pd)
            atr = estimate_atr(bars, bar_idx)
            close = float(row["close"])
            high = float(row["high"])
            low = float(row["low"])
            r_mul = (close - entry_price) * side_float / risk_per_share
            if r_mul > peak_r: peak_r = r_mul

            # 1. Passive limit fill at target
            if target_tp is not None:
                if side_float == 1.0 and high >= target_tp:
                    exit_px = target_tp
                    exit_at = bar_ts
                    exit_reason = "TARGET_LIMIT_FILL (2R Reached)"
                    break
                elif side_float == -1.0 and low <= target_tp:
                    exit_px = target_tp
                    exit_at = bar_ts
                    exit_reason = "TARGET_LIMIT_FILL (2R Reached)"
                    break

            # 2. Hard broker stop or dynamic ratchet hit
            if i > 0:
                if side_float == 1.0 and low <= current_stop:
                    exit_px = current_stop
                    exit_at = bar_ts
                    exit_reason = f"STOP_OR_RATCHET_BREACH (${current_stop:.2f})"
                    break
                elif side_float == -1.0 and high >= current_stop:
                    exit_px = current_stop
                    exit_at = bar_ts
                    exit_reason = f"STOP_OR_RATCHET_BREACH (${current_stop:.2f})"
                    break

            obs = PositionObservation(
                timestamp=bar_ts, symbol=sym, side=side_float, qty=qty, entry_price=entry_price,
                current_price=close, high=high, low=low, close=close, volume=float(row["volume"]), atr=atr,
                initial_stop=initial_stop, risk_per_share=risk_per_share, current_stop=current_stop,
                take_profit=target_tp, r_multiple=r_mul, news_sentiment_score=sentiment_score
            )

            # Evaluate intelligent health
            raw_health = evaluator.evaluate(obs, peak_r, 0.0, avg_vol)
            # EMA Smoothing (Alpha = 0.60: fast response to sustained shifts, filter out 1-tick anomalies)
            smoothed_health = (0.60 * raw_health.overall) + (0.40 * smoothed_health)

            # Check for structural distribution exit
            if smoothed_health < 0.28 and r_mul <= -0.25:
                exit_px = close
                exit_at = bar_ts
                exit_reason = f"STRUCTURAL_DISTRIBUTION_CUT (Health={smoothed_health:.2f}, R={r_mul:+.2f}R)"
                break

            # Ratchet protection dynamically
            dec = brain.observe(obs)
            if dec.target_stop_loss is not None and dec.target_stop_loss > 0:
                if side_float > 0 and dec.target_stop_loss > current_stop:
                    current_stop = dec.target_stop_loss
                elif side_float < 0 and dec.target_stop_loss < current_stop:
                    current_stop = dec.target_stop_loss

        brain.close()
        if exit_px is None:
            exit_px = float(bars_from_entry.iloc[-1]["close"])
            exit_at = bars_from_entry.index[-1].to_pydatetime()
            exit_reason = "EOD_CLOSE"

        pnl_intel = round((exit_px - entry_price) * side_float * qty, 2)
        pnl_master = trade["legacy_pnl"]
        delta = pnl_intel - pnl_master

        tot_master += pnl_master
        tot_intel += pnl_intel
        if pnl_master > 0: wins_m += 1
        if pnl_intel > 0: wins_i += 1

        exit_time_str = exit_at.astimezone(ET).strftime("%H:%M:%S") if exit_at else "N/A"
        print(f"{idx:<3} {sym:<5} {trade['strategy']:<14} {side_str:<5} | ${pnl_master:>8.2f} ({trade['exit_price']:.2f}) | ${pnl_intel:>8.2f} ({exit_px:.2f})        | {delta:>+8.2f}  | {exit_time_str} ET - {exit_reason}")

    bars_conn.close()

    print("-" * 145)
    print(f"{'TOTAL REALIZED P&L:':<30} | ${tot_master:>16.2f} | ${tot_intel:>24.2f} | ${tot_intel - tot_master:>+8.2f}")
    print(f"{'WIN RATE:':<30} | {f'{wins_m}/12 ({wins_m/12*100:.1f}%)':>18} | {f'{wins_i}/12 ({wins_i/12*100:.1f}%)':>26} |")
    print("=" * 145)

if __name__ == "__main__":
    run_intelligent_pis_simulation()
