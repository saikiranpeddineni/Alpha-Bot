from datetime import datetime, timezone
import math
from app.pis.models import PositionObservation, PISConfig, LifecycleState
from app.pis.position_brain import PositionBrain

def test_nbis():
    config = PISConfig()
    
    entry = 211.52
    sl = 213.06
    risk = sl - entry # 1.54
    
    print(f"Risk: {risk}")
    
    obs1 = PositionObservation(
        timestamp=datetime.now(timezone.utc),
        symbol="NBIS",
        side=-1.0,
        qty=93.0,
        entry_price=entry,
        current_price=entry,
        high=entry,
        low=entry,
        close=entry,
        volume=1000,
        atr=1.5,
        initial_stop=sl,
        risk_per_share=risk,
        current_stop=sl,
        take_profit=207.38,
        r_multiple=0.0,
        news_sentiment_score=0.0
    )
    
    brain = PositionBrain("NBIS", obs1, config)
    
    prices = [211.52, 211.00, 210.50, 210.00, 209.49]
    for px in prices:
        r = (entry - px) / risk
        obs = PositionObservation(
            timestamp=datetime.now(timezone.utc),
            symbol="NBIS",
            side=-1.0,
            qty=93.0,
            entry_price=entry,
            current_price=px,
            high=px,
            low=px,
            close=px,
            volume=1000,
            atr=1.5,
            initial_stop=sl,
            risk_per_share=risk,
            current_stop=brain.current_stop_floor,
            take_profit=207.38,
            r_multiple=r,
            news_sentiment_score=0.0
        )
        decision = brain.evaluate(obs)
        print(f"Px: {px:.2f} | R: {r:.2f} | PeakR: {brain.peak_r:.2f} | Runner: {brain._runner_qualified} | Stop: {brain.current_stop_floor} | Action: {decision.action.name} | Reasons: {[r.value for r in decision.reason_codes]}")

if __name__ == "__main__":
    test_nbis()
