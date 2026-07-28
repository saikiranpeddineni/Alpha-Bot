import random

from app.research.spawn.spawn_mutation_intelligence import SpawnMutationIntelligence


def test_positive_directional_evidence_biases_mutation_without_removing_exploration():
    memory = SpawnMutationIntelligence(min_samples=2, prior_strength=2.0, exploration_floor=0.05)
    for _ in range(12):
        memory.record("ml.ml_threshold", -1, 2.0, 20)
        memory.record("ml.ml_threshold", 1, -1.0, 20)
    rng = random.Random(17)
    choices = [memory.choose([("ml", "ml_threshold")], rng)[1] for _ in range(300)]
    assert choices.count(-1) > choices.count(1)
    assert choices.count(1) > 0


def test_invalid_evidence_is_ignored():
    memory = SpawnMutationIntelligence()
    memory.record("", 1, 1.0, 10)
    memory.record("risk.max_position_pct", 0, 1.0, 10)
    memory.record("risk.max_position_pct", 1, float("nan"), 10)
    assert memory.preferences() == []
