from datetime import UTC, datetime, timedelta

from app.research.outcome_joiner import CanonicalOutcomeJoiner
from app.research.shadow_event import ShadowEventType, TradingObservationEvent


T0 = datetime(2026, 7, 24, 14, 30, tzinfo=UTC)


def candidate(*, stop=99.0):
    return TradingObservationEvent(
        event_type=ShadowEventType.CANDIDATE,
        event_id="evt-candidate",
        candidate_id="cand-1",
        ts=T0,
        decision_bar_timestamp_utc=T0 - timedelta(minutes=1),
        symbol="NVDA",
        side="long",
        strategy="momentum",
        price=100.0,
        stop_loss=stop,
        champion_final_decision="accepted",
    )


def bar(minutes, *, open_=100.0, high=101.0, low=99.5, close=100.5):
    return TradingObservationEvent(
        event_type=ShadowEventType.MARKET_BAR,
        event_id=f"bar-{minutes}",
        ts=T0 + timedelta(minutes=minutes),
        symbol="NVDA",
        open=open_, high=high, low=low, close=close,
    )


def test_outcomes_join_same_candidate_id_without_future_data():
    emitted = []
    joiner = CanonicalOutcomeJoiner(lambda event, horizon, outcome: emitted.append((event.candidate_id, horizon, outcome)))
    joiner.consume(candidate())
    joiner.consume(bar(1, high=101.2, low=99.7, close=100.8))
    assert len(emitted) == 1
    candidate_id, horizon, outcome = emitted[0]
    assert candidate_id == "cand-1"
    assert horizon == "1m"
    assert outcome["mfe"] == 1.2
    assert outcome["mae"] == 0.3
    assert outcome["realized_r"] == 0.8


def test_out_of_order_bar_is_ignored():
    emitted = []
    joiner = CanonicalOutcomeJoiner(lambda *args: emitted.append(args))
    joiner.consume(candidate())
    joiner.consume(TradingObservationEvent(
        event_type=ShadowEventType.MARKET_BAR,
        event_id="old-bar", ts=T0, symbol="NVDA", open=100.0, high=110.0, low=90.0, close=105.0,
    ))
    assert emitted == []
    assert joiner.out_of_order_bars == 1


def test_stop_hit_uses_conservative_intrabar_mfe():
    emitted = []
    joiner = CanonicalOutcomeJoiner(lambda event, horizon, outcome: emitted.append(outcome))
    joiner.consume(candidate(stop=99.0))
    joiner.consume(bar(1, open_=100.2, high=105.0, low=98.5, close=104.0))
    assert emitted[0]["stopped"] is True
    assert emitted[0]["realized_r"] == -1.0
    assert emitted[0]["mfe"] == 0.2


def test_missing_stop_never_fabricates_r_multiple():
    emitted = []
    joiner = CanonicalOutcomeJoiner(lambda event, horizon, outcome: emitted.append(outcome))
    joiner.consume(candidate(stop=None))
    joiner.consume(bar(1, high=101.0, low=99.0, close=100.5))
    assert emitted[0]["stop_is_synthetic"] is True
    assert emitted[0]["mfe_r"] is None
    assert emitted[0]["mae_r"] is None
    assert emitted[0]["realized_r"] is None


def test_wrong_side_stop_is_unavailable_and_never_triggers_shadow_exit():
    emitted = []
    joiner = CanonicalOutcomeJoiner(lambda event, horizon, outcome: emitted.append(outcome))
    joiner.consume(candidate(stop=101.0))
    joiner.consume(bar(1, open_=100.0, high=101.5, low=99.0, close=100.5))
    assert emitted[0]["stop_is_synthetic"] is True
    assert emitted[0]["stopped"] is False
    assert emitted[0]["realized_r"] is None
    assert emitted[0]["fwd_return"] == 0.005


def test_horizons_emit_once_each():
    emitted = []
    joiner = CanonicalOutcomeJoiner(lambda event, horizon, outcome: emitted.append(horizon))
    joiner.consume(candidate())
    for minute in (1, 2, 5, 15, 30, 60, 61):
        joiner.consume(bar(minute))
    assert emitted == ["1m", "5m", "15m", "30m", "60m"]
    assert joiner.telemetry()["active_candidates"] == 0
