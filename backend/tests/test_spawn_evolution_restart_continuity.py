from datetime import UTC, datetime

from app.research.spawn.spawn_config import ChildPolicyConfig
from app.research.spawn.spawn_evolution import SpawnEvolutionEngine
from app.research.spawn.spawn_shadow_coordinator import SpawnShadowCoordinator
from app.research.spawn.spawn_virtual_portfolio import SpawnVirtualPortfolio


def test_closed_trade_evidence_survives_checkpoint_roundtrip():
    p = SpawnVirtualPortfolio("spawn_g2_x", 2500.0, {"allow_fractional": True})
    ts = datetime(2026, 7, 28, 14, 30, tzinfo=UTC)
    p.open_virtual_position("SPY", "long", 10.0, 100.0, 99.0, 102.0, ts)
    p.close_virtual_position("SPY", 102.0, "TAKE_PROFIT", ts)
    checkpoint = p.to_checkpoint_dict()

    restored = SpawnVirtualPortfolio("spawn_g2_x", 2500.0, {"allow_fractional": True})
    restored.restore_from_checkpoint(checkpoint)
    assert restored.trade_count == 1
    assert len(restored.closed_trades) == 1
    assert restored.closed_trades[0]["symbol"] == "SPY"
    assert restored.closed_trades[0]["realized_r"] == 2.0


def test_generation_parser_preserves_persisted_generation_number():
    config = ChildPolicyConfig(
        policy_id="spawn_g17_child",
        parent_policy_id="spawn_g16_parent",
        policy_version="g17",
        spawned_at=datetime(2026, 7, 28, 14, 30, tzinfo=UTC),
        seed_capital=2500.0,
    )
    assert SpawnShadowCoordinator._generation_from_config(config) == 17


def test_behavior_fingerprint_ignores_lineage_identity():
    ts = datetime(2026, 7, 28, 14, 30, tzinfo=UTC)
    a = ChildPolicyConfig(policy_id="a", parent_policy_id="p1", policy_version="g1", spawned_at=ts, seed_capital=2500.0, ml_overrides={"ml_threshold": 0.5})
    b = ChildPolicyConfig(policy_id="b", parent_policy_id="p2", policy_version="g9", spawned_at=ts, seed_capital=2500.0, ml_overrides={"ml_threshold": 0.5})
    engine = SpawnEvolutionEngine()
    assert a.compute_fingerprint() != b.compute_fingerprint()
    assert engine.behavior_config_fingerprint(a) == engine.behavior_config_fingerprint(b)
