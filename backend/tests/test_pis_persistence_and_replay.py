from datetime import datetime, timezone

from app.pis.models import LifecycleState
from app.pis.persistence import PISStateStore
from app.pis.position_brain import PositionBrain
from app.pis.replay_metrics import score_exit_quality


class MemoryDB:
    def __init__(self): self.values = {}
    def get_setting(self, key): return self.values.get(key)
    def set_setting(self, key, value): self.values[key] = value


def test_restart_restores_peak_r_and_entry_thesis_memory():
    db = MemoryDB()
    store = PISStateStore(db)
    brain = PositionBrain("NVDA", "long", 100.0, 98.0, datetime.now(timezone.utc), strategy="momentum")
    brain.entry_health = 74.0
    brain.peak_r = 5.25
    brain.trough_r = -0.35
    brain.lifecycle = LifecycleState.RUNNER
    store.save_brain(brain)

    restored = store.restore("NVDA")
    assert restored is not None
    assert restored.entry_health == 74.0
    assert restored.peak_r == 5.25
    assert restored.trough_r == -0.35
    assert restored.lifecycle is LifecycleState.RUNNER


def test_plus_one_exit_that_later_reaches_five_r_is_flagged():
    quality = score_exit_quality(realized_r=1.0, future_mfe_r=5.5)
    assert quality.premature_exit
    assert quality.winner_became_large_winner
    assert quality.missed_r == 4.5


def test_good_exit_captures_most_available_mfe():
    quality = score_exit_quality(realized_r=4.5, future_mfe_r=5.0)
    assert quality.captured_mfe_pct == 90.0
    assert not quality.premature_exit
