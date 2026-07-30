from app.pis.readiness import ReadinessInputs, production_readiness


def test_incomplete_engine_cutover_blocks_production():
    result = production_readiness(ReadinessInputs(
        engine_cutover_complete=False,
        legacy_discretionary_exits_removed=False,
        hard_safety_controls_intact=True,
        restart_recovery_verified=True,
        replay_verified=True,
        shadow_verified=True,
        promotion_gate_passed=True,
        stale_data_fail_safe_verified=True,
        duplicate_close_verified=True,
    ))
    assert not result.ready
    assert "PIS_NOT_READY_ENGINE_CUTOVER_COMPLETE" in result.blockers


def test_every_gate_must_pass_before_live_promotion():
    result = production_readiness(ReadinessInputs(
        engine_cutover_complete=True,
        legacy_discretionary_exits_removed=True,
        hard_safety_controls_intact=True,
        restart_recovery_verified=True,
        replay_verified=True,
        shadow_verified=True,
        promotion_gate_passed=True,
        stale_data_fail_safe_verified=True,
        duplicate_close_verified=True,
    ))
    assert result.ready
    assert not result.blockers
