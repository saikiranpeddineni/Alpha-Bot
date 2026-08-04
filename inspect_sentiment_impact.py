import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(r"C:\Users\peddi\Desktop\Bots\Bot_QA")
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.pis.models import PositionObservation, PISConfig, ExitAction
from app.pis.position_brain import PositionBrain

MASTER_DB = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\alphadesk.db")
MASTER_BARS = Path(r"C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\data\bars_cache.db")
SENTIMENTS = {'INTC': 0.09, 'CRWV': -0.02, 'NBIS': 0.0, 'NOW': 0.0, 'SPCX': 0.0, 'META': 0.32, 'TQQQ': 0.0, 'AMZN': -0.16, 'BABA': -0.23, 'NVDA': 0.0}

from run_detailed_replay_report import fetch_completed_trades, load_bars, estimate_atr

trades = fetch_completed_trades()
bars_conn = sqlite3.connect(str(MASTER_BARS))

print("=" * 110)
print("  NEWS SENTIMENT IMPACT ANALYSIS ON ALL 12 TRADES (2026-08-03)")
print("=" * 110)

for idx, t in enumerate(trades, 1):
    sym = t["symbol"]
    score = SENTIMENTS.get(sym, 0.0)
    print(f"\n[{idx:02d}] {sym:<5} ({t['strategy']:<15} {t['side'].upper():<5}) | News Sentiment Score: {score:>+5.2f}")
    
    # Replay with sentiment vs without sentiment (score=0.0)
    for use_sent, label in [(False, "WITHOUT Sentiment (0.00)"), (True, f"WITH Sentiment ({score:>+5.2f})")]:
        sent = score if use_sent else 0.0
        
        # Load and run
        entry_at = datetime.fromisoformat(t["entry_at"].replace("Z", "+00:00"))
        bars = load_bars(sym, entry_at.timestamp(), bars_conn)
        entry_ts_pd = pd.Timestamp(entry_at)
        bars_from_entry = bars[bars.index >= entry_ts_pd]
        if bars_from_entry.empty:
            bars_from_entry = bars[bars.index >= (entry_ts_pd - pd.Timedelta(minutes=1))]
            
        row0 = bars_from_entry.iloc[0]
        ts0 = bars_from_entry.index[0].to_pydatetime()
        if ts0.tzinfo is None: ts0 = ts0.replace(tzinfo=timezone.utc)
        
        side_float = 1.0 if t["side"] == "long" else -1.0
        obs0 = PositionObservation(
            timestamp=ts0, symbol=sym, side=side_float, qty=float(t["qty"]),
            entry_price=float(t["entry_price"]), current_price=float(row0["close"]),
            high=float(row0["high"]), low=float(row0["low"]), close=float(row0["close"]),
            volume=float(row0["volume"]), atr=1.0, initial_stop=float(t["stop_loss"]),
            risk_per_share=float(t["initial_risk"]), current_stop=float(t["stop_loss"]),
            take_profit=float(t["take_profit"]) if t["take_profit"] else None,
            r_multiple=0.0, news_sentiment_score=sent
        )
        brain = PositionBrain(sym, obs0, PISConfig())
        
        exit_bar = None
        exit_price = None
        exit_reason = "EOD"
        current_stop = float(t["stop_loss"])
        
        for i, (bar_ts_pd, row) in enumerate(bars_from_entry.iterrows()):
            bar_ts = bar_ts_pd.to_pydatetime()
            if bar_ts.tzinfo is None: bar_ts = bar_ts.replace(tzinfo=timezone.utc)
            
            close = float(row["close"])
            high = float(row["high"])
            low = float(row["low"])
            vol = float(row["volume"])
            r_mul = (close - float(t["entry_price"])) * side_float / float(t["initial_risk"])
            
            if i > 0:
                if side_float == 1.0 and low <= current_stop:
                    exit_bar = i + 1
                    exit_price = current_stop
                    exit_reason = "STOP_BREACH"
                    break
                elif side_float == -1.0 and high >= current_stop:
                    exit_bar = i + 1
                    exit_price = current_stop
                    exit_reason = "STOP_BREACH"
                    break
                    
            obs = PositionObservation(
                timestamp=bar_ts, symbol=sym, side=side_float, qty=float(t["qty"]),
                entry_price=float(t["entry_price"]), current_price=close,
                high=high, low=low, close=close, volume=vol, atr=1.0,
                initial_stop=float(t["stop_loss"]), risk_per_share=float(t["initial_risk"]),
                current_stop=current_stop,
                take_profit=float(t["take_profit"]) if t["take_profit"] else None,
                r_multiple=r_mul, news_sentiment_score=sent
            )
            decision = brain.observe(obs)
            
            if decision.action in (ExitAction.FULL_EXIT, ExitAction.PARTIAL_EXIT):
                exit_bar = i + 1
                reasons = [r.value if hasattr(r, "value") else str(r) for r in decision.reason_codes]
                exit_reason = f"{decision.action.value} ({','.join(reasons)})"
                exit_price = close
                break
                
            if decision.target_stop_loss is not None and decision.target_stop_loss > 0:
                current_stop = decision.target_stop_loss
                
        pnl = (exit_price - float(t["entry_price"])) * side_float * float(t["qty"]) if exit_price else 0.0
        print(f"     {label:<30} -> Exit Bar: {exit_bar} | Exit Px: ${exit_price:.2f} | PnL: ${pnl:>7.2f} | Reason: {exit_reason}")
        brain.close()

bars_conn.close()
