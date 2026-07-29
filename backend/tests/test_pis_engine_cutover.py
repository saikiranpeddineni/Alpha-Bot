from app.pis.engine_cutover import PISLiveExitController


def test_all_known_legacy_discretionary_exit_sources_are_suppressed():
    controller = PISLiveExitController()
    for source in (
        "strategy_exit",
        "fixed_take_profit",
        "atr_trailing",
        "live_runner",
        "adaptive_harvester",
    ):
        event = controller.suppress_legacy(source, "counterfactual only")
        assert event.source == source


def test_unknown_source_cannot_sneak_through_cutover_controller():
    controller = PISLiveExitController()
    try:
        controller.suppress_legacy("mystery_exit")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown discretionary authority must fail closed")
