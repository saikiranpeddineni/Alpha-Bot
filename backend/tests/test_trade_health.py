import pytest
from datetime import datetime, timezone
from app.pis.models import PositionObservation, PISConfig
from app.pis.trade_health import TradeHealthEvaluator


def create_obs(side, close, low, high, atr, risk, r_mult, volume=1000, news=0.0):
    return PositionObservation(
        timestamp=datetime.now(timezone.utc),
        symbol="TEST",
        side=side,
        qty=100,
        entry_price=100.0,
        current_price=close,
        close=close,
        high=high,
        low=low,
        volume=volume,
        atr=atr,
        initial_stop=100.0 - (side * risk),
        risk_per_share=risk,
        r_multiple=r_mult,
        news_sentiment_score=news
    )


def test_iren_noise_no_premature_ratchet():
    """1. Strong trend -> one normal pullback (ATR < 1.0) -> high health retained."""
    evaluator = TradeHealthEvaluator(PISConfig())
    # IREN context: 0.68R peak, tiny pullback
    # 43.08 peak, 42.94 pullback. ATR = 0.50 (large relative to the 0.14 move)
    # Risk per share is 0.25
    
    # Healthy bar
    obs1 = create_obs(side=1, close=43.08, low=43.00, high=43.10, atr=0.50, risk=0.25, r_mult=0.68)
    health1 = evaluator.evaluate(obs1, peak_r=0.68, trough_r=0.0, history=[obs1])
    assert health1.overall > 0.80

    # Noise pullback bar
    obs2 = create_obs(side=1, close=42.94, low=42.90, high=43.08, atr=0.50, risk=0.25, r_mult=0.08)
    health2 = evaluator.evaluate(obs2, peak_r=0.68, trough_r=0.0, history=[obs1, obs2])
    
    # Crucial: health should not crash below 0.40!
    assert health2.overall > 0.40, f"Health crashed to {health2.overall} on normal noise!"


def test_true_deterioration():
    """2. Strong trend -> 3 consecutive adverse bars -> health degrades."""
    evaluator = TradeHealthEvaluator()
    obs1 = create_obs(1, close=102.0, low=101.5, high=102.2, atr=0.5, risk=0.5, r_mult=4.0)
    
    # Adverse 1 (lower high, lower close, adverse close)
    obs2 = create_obs(1, close=101.5, low=101.4, high=101.8, atr=0.5, risk=0.5, r_mult=3.0, volume=2000)
    # Adverse 2
    obs3 = create_obs(1, close=101.0, low=100.9, high=101.5, atr=0.5, risk=0.5, r_mult=2.0, volume=3000)
    # Adverse 3
    obs4 = create_obs(1, close=100.5, low=100.4, high=101.0, atr=0.5, risk=0.5, r_mult=1.0, volume=4000)
    
    health = evaluator.evaluate(obs4, peak_r=4.0, trough_r=0.0, history=[obs1, obs2, obs3, obs4])
    # Persistent deterioration should cause a sharp drop
    assert health.overall < 0.40, f"Health {health.overall} remained too high during persistent structural failure"


def test_aaoi_immediate_failure():
    """3. AAOI-style failure: entry -> immediate adverse -> EARLY_THESIS_INVALIDATED works."""
    evaluator = TradeHealthEvaluator()
    # Entry at 137.91. Immediate drop.
    obs1 = create_obs(side=1, close=137.30, low=137.20, high=138.00, atr=1.0, risk=1.0, r_mult=-0.69)
    health1 = evaluator.evaluate(obs1, peak_r=0.0, trough_r=-0.69, history=[obs1])
    
    obs2 = create_obs(side=1, close=137.20, low=137.00, high=137.50, atr=1.0, risk=1.0, r_mult=-0.80)
    health2 = evaluator.evaluate(obs2, peak_r=0.0, trough_r=-0.80, history=[obs1, obs2])
    
    assert health2.overall < 0.50, f"Health {health2.overall} too high for immediate failure"


def test_long_short_symmetry():
    """5. Long/short symmetry."""
    evaluator = TradeHealthEvaluator()
    # Long
    long_obs = create_obs(side=1, close=105.0, low=104.0, high=105.5, atr=1.0, risk=1.0, r_mult=5.0)
    h_long = evaluator.evaluate(long_obs, peak_r=5.0, trough_r=0.0, history=[long_obs])
    
    # Short
    short_obs = create_obs(side=-1, close=95.0, low=94.5, high=96.0, atr=1.0, risk=1.0, r_mult=5.0)
    h_short = evaluator.evaluate(short_obs, peak_r=5.0, trough_r=0.0, history=[short_obs])
    
    assert abs(h_long.overall - h_short.overall) < 0.01, f"Long {h_long.overall} vs Short {h_short.overall}"


def test_high_volatility_stock():
    """3. high-volatility stock -> normal pullback does not crash health"""
    evaluator = TradeHealthEvaluator()
    # ATR is huge (e.g. 5.0). Risk per share is 2.0.
    obs1 = create_obs(1, 205.0, 203.0, 206.0, 5.0, 2.0, 2.5) # Peak
    obs2 = create_obs(1, 202.0, 200.0, 205.0, 5.0, 2.0, 1.0) # Pullback of 3.0 pts (0.6 ATR)
    
    health2 = evaluator.evaluate(obs2, peak_r=2.5, trough_r=0.0, history=[obs1, obs2])
    assert health2.overall > 0.40, f"Health crashed {health2.overall} on normal high-volatility pullback"


def test_genuine_reversal():
    """4. genuine reversal -> >3.0 ATR drop in 1 bar -> health degrades instantly"""
    evaluator = TradeHealthEvaluator()
    obs1 = create_obs(1, 105.0, 104.0, 106.0, 1.0, 1.0, 5.0) # Peak
    # Massive drop of 4 pts (4 ATR)
    obs2 = create_obs(1, 101.0, 100.0, 105.0, 1.0, 1.0, 1.0) 
    
    health2 = evaluator.evaluate(obs2, peak_r=5.0, trough_r=0.0, history=[obs1, obs2])
    assert health2.overall < 0.40, f"Health stayed too high {health2.overall} on genuine reversal"


def test_favorable_news():
    """6. Favorable news + adverse bar vs Favorable news + healthy bar"""
    evaluator = TradeHealthEvaluator()
    
    # Healthy bar + bullish news (1.0) for long
    obs_healthy = create_obs(1, 105.0, 104.0, 105.5, 1.0, 1.0, 5.0, news=1.0)
    h_healthy = evaluator.evaluate(obs_healthy, peak_r=5.0, trough_r=0.0, history=[obs_healthy])
    
    # Adverse bar + bullish news
    obs_adverse = create_obs(1, 104.2, 104.0, 105.0, 1.0, 1.0, 4.2, news=1.0)
    h_adverse = evaluator.evaluate(obs_adverse, peak_r=5.0, trough_r=0.0, history=[obs_healthy, obs_adverse])
    
    # Adverse bar without bullish news
    obs_adverse_no_news = create_obs(1, 104.2, 104.0, 105.0, 1.0, 1.0, 4.2, news=0.0)
    h_adverse_no_news = evaluator.evaluate(obs_adverse_no_news, peak_r=5.0, trough_r=0.0, history=[obs_healthy, obs_adverse_no_news])
    
    assert h_adverse.overall > h_adverse_no_news.overall, "News sentiment should boost adverse bar health"


def test_healthy_continuation():
    """7. Healthy continuation (expansion after pullback) restores health"""
    evaluator = TradeHealthEvaluator()
    obs1 = create_obs(1, 105.0, 104.0, 106.0, 1.0, 1.0, 5.0) # Peak
    obs2 = create_obs(1, 104.5, 104.0, 105.0, 1.0, 1.0, 4.5) # Pullback
    h_pullback = evaluator.evaluate(obs2, peak_r=5.0, trough_r=0.0, history=[obs1, obs2])
    
    obs3 = create_obs(1, 105.5, 104.5, 106.0, 1.0, 1.0, 5.5) # Continuation
    h_cont = evaluator.evaluate(obs3, peak_r=5.5, trough_r=0.0, history=[obs1, obs2, obs3])
    
    assert h_cont.overall > h_pullback.overall, "Healthy continuation did not restore health"
    assert h_cont.overall > 0.80, "Health should be highly restored on continuation"
