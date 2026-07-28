from datetime import UTC, datetime, timedelta

from app.research.spawn.spawn_config import ChildPolicyConfig
from app.research.spawn.spawn_executor import ChildPolicyExecutor


def _executor(**risk):
    now = datetime(2026, 7, 29, 14, 30, tzinfo=UTC)
    cfg = ChildPolicyConfig(
        policy_id="spawn_test_child",
        spawned_at=now - timedelta(days=1),
        risk_overrides=risk,
    )
    return ChildPolicyExecutor(cfg), now


def _obs(now, **overrides):
    obs = {
        "symbol": "SPY",
        "strategy": "momentum",
        "direction": "long",
        "price": 100.0,
        "stop_loss": 99.0,
        "take_profit": 102.0,
        "ts": now,
        "champion_candidate": {
            "id": "cand-spy-1",
            "ml_probability": 0.70,
            "meta_probability": 0.70,
            "trade_score": 80.0,
            "final_decision": "accepted",
        },
    }
    obs.update(overrides)
    return obs


def test_missing_stop_is_not_synthesized_and_cannot_open():
    executor, now = _executor()
    obs = _obs(now)
    obs.pop("stop_loss")
    record = executor.evaluate_observation(obs)
    assert record is not None
    assert record["stop_loss"] is None
    assert record["risk_decision"] == "REJECT_RISK_STOP_UNAVAILABLE"
    assert record["hypothetical_final_decision"] == "REJECTED"
    assert "SPY" not in executor.portfolio.positions


def test_missing_meta_evidence_cannot_be_inferred_from_production_final_decision():
    executor, now = _executor()
    obs = _obs(now)
    obs["champion_candidate"].pop("meta_probability")
    record = executor.evaluate_observation(obs)
    assert record["meta_decision"] == "UNAVAILABLE"
    assert record["hypothetical_final_decision"] == "REJECTED"
    assert "SPY" not in executor.portfolio.positions


def test_duplicate_symbol_cannot_rotate_out_unrelated_incumbent():
    executor, now = _executor(rotation_enabled=True, max_open_positions=2)
    executor.portfolio.open_virtual_position("SPY", "long", 1.0, 100.0, 99.0, 102.0, now - timedelta(minutes=30))
    executor.portfolio.open_virtual_position("QQQ", "long", 1.0, 200.0, 198.0, 204.0, now - timedelta(minutes=30))
    # No OHLC is supplied, so the existing SPY remains open and the incoming SPY
    # must be rejected before the allocator can sacrifice QQQ.
    record = executor.evaluate_observation(_obs(now))
    # Existing positions require complete OHLC for causal state advancement; no
    # mutation is allowed when that evidence is incomplete.
    assert record is None
    assert set(executor.portfolio.positions) == {"SPY", "QQQ"}


def test_disagreement_is_computed_after_final_execution_outcome():
    executor, now = _executor(max_open_positions=0)
    record = executor.evaluate_observation(_obs(now))
    assert record["hypothetical_final_decision"] == "REJECTED"
    assert record["disagreement_category"] == "CHAMPION_ONLY"
