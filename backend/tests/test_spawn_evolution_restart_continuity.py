from datetime import UTC, datetime, timedelta

from app.research.spawn.spawn_config import ChildPolicyConfig
from app.research.spawn.spawn_evolution import EvolutionSettings, SpawnEvolutionEngine
from app.research.spawn.spawn_executor import ChildPolicyExecutor
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
    config = ChildPolicyConfig(policy_id="spawn_g17_child", parent_policy_id="spawn_g16_parent", policy_version="g17", spawned_at=datetime(2026, 7, 28, 14, 30, tzinfo=UTC), seed_capital=2500.0)
    assert SpawnShadowCoordinator._generation_from_config(config) == 17


def test_behavior_fingerprint_ignores_lineage_identity():
    ts = datetime(2026, 7, 28, 14, 30, tzinfo=UTC)
    a = ChildPolicyConfig(policy_id="a", parent_policy_id="p1", policy_version="g1", spawned_at=ts, seed_capital=2500.0, ml_overrides={"ml_threshold": 0.5})
    b = ChildPolicyConfig(policy_id="b", parent_policy_id="p2", policy_version="g9", spawned_at=ts, seed_capital=2500.0, ml_overrides={"ml_threshold": 0.5})
    engine = SpawnEvolutionEngine()
    assert a.compute_fingerprint() != b.compute_fingerprint()
    assert engine.behavior_config_fingerprint(a) == engine.behavior_config_fingerprint(b)


def _trade(ts, r):
    return {"entry_at": (ts - timedelta(minutes=10)).isoformat(), "exit_at": ts.isoformat(), "realized_r": r, "pnl": r * 10.0, "initial_dollar_risk": 10.0, "entry_notional": 500.0, "symbol": "SPY", "side": "long", "exit_reason": "TEST"}


def test_mutation_learning_uses_only_matched_post_birth_evidence():
    settings = EvolutionSettings(min_trades_for_selection=2, confidence_trade_count=2, max_population=6, elite_count=2)
    evo = SpawnEvolutionEngine(settings)
    birth = datetime(2026, 7, 28, 14, 30, tzinfo=UTC)
    parent_cfg = ChildPolicyConfig(policy_id="parent", spawned_at=birth - timedelta(days=5), seed_capital=2500.0)
    child_cfg = ChildPolicyConfig(policy_id="child", parent_policy_id="parent", policy_version="g1", spawned_at=birth, seed_capital=2500.0, ml_overrides={"ml_threshold": .52}, spawn_reason="EVOLUTION_EDGE_DENSITY_VELOCITY:ml.ml_threshold:0.48->0.52:1")
    parent = ChildPolicyExecutor(parent_cfg); child = ChildPolicyExecutor(child_cfg)
    # Huge pre-birth parent history must not influence the mutation verdict.
    parent.portfolio.closed_trades = [_trade(birth - timedelta(days=1), 50.0), _trade(birth - timedelta(hours=1), 50.0), _trade(birth + timedelta(minutes=20), -1.0), _trade(birth + timedelta(minutes=40), -1.0)]
    child.portfolio.closed_trades = [_trade(birth + timedelta(minutes=20), 1.0), _trade(birth + timedelta(minutes=40), 1.0)]
    parent.portfolio.trade_count = len(parent.portfolio.closed_trades); child.portfolio.trade_count = len(child.portfolio.closed_trades)
    evo.learn_mutation_outcomes({"parent": parent, "child": child})
    prefs = {(p.dimension, p.direction): p for p in evo.mutation_intelligence.preferences()}
    assert prefs[("ml.ml_threshold", 1)].mean_delta > 0


def test_retired_children_do_not_form_clone_groups():
    settings = EvolutionSettings(min_trades_for_selection=1, confidence_trade_count=1, max_population=6, elite_count=2)
    evo = SpawnEvolutionEngine(settings)
    ts = datetime(2026, 7, 28, 14, 30, tzinfo=UTC)
    a = ChildPolicyExecutor(ChildPolicyConfig(policy_id="a", spawned_at=ts - timedelta(hours=1)))
    b = ChildPolicyExecutor(ChildPolicyConfig(policy_id="b", spawned_at=ts - timedelta(hours=1), ml_overrides={"ml_threshold": .51}))
    for ex in (a, b):
        ex.portfolio.closed_trades = [_trade(ts, 1.0)]; ex.portfolio.trade_count = 1; ex.portfolio.realized_r = 1.0
    b.status = "RETIRED"
    assert evo.behavior_clone_groups({"a": a, "b": b}, 1) == {}
