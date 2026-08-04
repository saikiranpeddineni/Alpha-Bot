import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(r"C:\Users\peddi\Desktop\Bots\Bot_QA")
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.pis.position_brain import PositionBrain
from app.pis.models import PositionObservation, PISConfig, ExitAction

config = PISConfig()
ts0 = datetime(2026, 8, 3, 13, 54, tzinfo=timezone.utc)
obs0 = PositionObservation(
    timestamp=ts0, symbol="CRWV", side=1.0, qty=260, entry_price=78.55,
    current_price=78.94, high=79.07, low=78.55, close=78.94,
    volume=1000, atr=0.60, initial_stop=77.63, risk_per_share=0.92,
    current_stop=77.63, take_profit=80.02, r_multiple=0.42
)

brain = PositionBrain("CRWV", obs0, config)

ts1 = datetime(2026, 8, 3, 13, 55, tzinfo=timezone.utc)
obs1 = PositionObservation(
    timestamp=ts1, symbol="CRWV", side=1.0, qty=260, entry_price=78.55,
    current_price=80.08, high=80.08, low=78.94, close=80.08,
    volume=2000, atr=0.60, initial_stop=77.63, risk_per_share=0.92,
    current_stop=77.63, take_profit=80.02, r_multiple=1.66
)

decision = brain.observe(obs1)
print(f"Decision Action: {decision.action}")
print(f"Decision Exit Ratio: {decision.exit_ratio}")
print(f"Target Stop: {decision.target_stop_loss}")
print(f"Reasons: {[r.name for r in decision.reason_codes]}")
