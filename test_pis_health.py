import sys
from pathlib import Path
sys.path.append(str(Path("backend").resolve()))

import datetime
from app.pis.models import PositionObservation, TradeHealth, PISConfig
from app.pis.trade_health import TradeHealthEvaluator
from app.pis.protection import AdaptiveProtectionPlanner

def create_obs(price, high, low, close, volume, timestamp_mins, current_r):
    return PositionObservation(
        timestamp=datetime.datetime(2026, 8, 13, 9, timestamp_mins, tzinfo=datetime.timezone.utc),
        symbol="BABA",
        side=-1, # Short
        qty=154,
        entry_price=123.04,
        current_price=price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        atr=0.45,
        initial_stop=123.49,
        risk_per_share=0.45,
        current_stop=123.49,
        take_profit=122.14,
        r_multiple=current_r,
        news_sentiment_score=0.0
    )

def test_pullback():
    evaluator = TradeHealthEvaluator()
    planner = AdaptiveProtectionPlanner(PISConfig())
    
    # Simulate a healthy pullback for a Short position: 
    # Price moves slightly against us (up), volume is low
    history = [
        create_obs(122.59, 122.70, 122.50, 122.59, 1000, 30, 1.0),
        create_obs(122.65, 122.75, 122.60, 122.65, 500, 31, 0.86),
        create_obs(122.70, 122.80, 122.65, 122.70, 400, 32, 0.75),
        create_obs(122.75, 122.85, 122.70, 122.75, 300, 33, 0.64)
    ]
    
    obs = history[-1]
    health = evaluator.evaluate(obs, peak_r=1.0, trough_r=0.0, history=history)
    print("Pullback Health Score:", health.overall)
    print("Structure Classification:", health.structure_classification)
    
    floor = planner.compute_protection_floor(obs, peak_r=1.0, health=health)
    print("Computed Floor:", floor)
    
def test_breakdown():
    evaluator = TradeHealthEvaluator()
    planner = AdaptiveProtectionPlanner(PISConfig())
    
    # Simulate a structural breakdown for a Short position:
    # Price moves sharply against us (up), high volume, higher highs
    history = [
        create_obs(122.59, 122.70, 122.50, 122.59, 1000, 30, 1.0),
        create_obs(122.80, 122.90, 122.60, 122.80, 2000, 31, 0.53),
        create_obs(122.95, 123.00, 122.80, 122.95, 3000, 32, 0.2),
        create_obs(123.04, 123.10, 122.90, 123.04, 4000, 33, 0.0)
    ]
    
    obs = history[-1]
    health = evaluator.evaluate(obs, peak_r=1.0, trough_r=0.0, history=history)
    print("\nBreakdown Health Score:", health.overall)
    print("Structure Classification:", health.structure_classification)
    
    floor = planner.compute_protection_floor(obs, peak_r=1.0, health=health)
    print("Computed Floor:", floor)

if __name__ == "__main__":
    test_pullback()
    test_breakdown()
