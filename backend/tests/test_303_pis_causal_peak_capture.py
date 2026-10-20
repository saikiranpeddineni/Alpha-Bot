"""Regression tests for causal peak-R runner harvesting.

The production decision stream only knows completed bars observed so far.  These
scenarios deliberately leave the runner above its existing wide protection floor
so a reversal/giveback decision, rather than a hindsight EOD peak or a stop
breach, is what harvests the remaining quantity.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.pis.models import ExitAction, PISConfig, PositionObservation, ReasonCode
from app.pis.position_brain import PositionBrain


_START = datetime(2026, 10, 17, 13, 30, tzinfo=UTC)


def _obs(
    *,
    current: float,
    high: float,
    low: float,
    close: float,
    minute: int,
    side: float = 1.0,
    entry: float = 100.0,
    initial_stop: float = 95.0,
    atr: float = 2.0,
    qty: float = 100.0,
) -> PositionObservation:
    risk = abs(entry - initial_stop)
    return PositionObservation(
        timestamp=_START + timedelta(minutes=minute),
        symbol="TEST",
        side=side,
        qty=qty,
        entry_price=entry,
        current_price=current,
        high=high,
        low=low,
        close=close,
        volume=10_000.0,
        atr=atr,
        initial_stop=initial_stop,
        risk_per_share=risk,
        current_stop=initial_stop,
        r_multiple=(current - entry) / risk * side,
    )


def _long_brain(*, config: PISConfig | None = None) -> PositionBrain:
    return PositionBrain(
        "TEST",
        _obs(current=100.0, high=100.0, low=99.0, close=100.0, minute=0),
        config or PISConfig(scale_out_target_r=1.0),
    )


def _scale_out_and_reversal(brain: PositionBrain):
    """Scale out on a 2R completed-bar high, then print two adverse closes."""
    scale_out = brain.observe(
        _obs(current=109.0, high=110.0, low=99.5, close=109.0, minute=1)
    )
    first_reversal = brain.observe(
        _obs(current=108.0, high=109.0, low=107.0, close=108.0, minute=2, qty=50.0)
    )
    confirmed_reversal = brain.observe(
        _obs(current=107.5, high=108.5, low=107.0, close=107.5, minute=3, qty=50.0)
    )
    return scale_out, first_reversal, confirmed_reversal


def test_causal_peak_capture_harvests_remaining_runner_after_confirmation() -> None:
    """Two completed adverse closes harvest a runner before its wide stop is hit."""
    brain = _long_brain()

    scale_out, first_reversal, confirmed_reversal = _scale_out_and_reversal(brain)

    assert scale_out.action == ExitAction.PARTIAL_EXIT
    assert brain.has_scaled_out is True
    # The old runner path held while price remained above its wide ATR floor.
    assert first_reversal.action in (ExitAction.HOLD, ExitAction.UPDATE_PROTECTION)
    assert confirmed_reversal.action == ExitAction.FULL_EXIT
    assert ReasonCode.CAUSAL_PEAK_REVERSAL in confirmed_reversal.reason_codes
    assert confirmed_reversal.metadata["causal_peak_capture"] is True
    assert confirmed_reversal.peak_r == 2.0


def test_one_adverse_completed_bar_does_not_harvest_runner() -> None:
    """A single reversal print is noise; confirmation must be causal and bar-based."""
    brain = _long_brain()
    scale_out = brain.observe(
        _obs(current=109.0, high=110.0, low=99.5, close=109.0, minute=1)
    )
    one_reversal = brain.observe(
        _obs(current=108.0, high=109.0, low=107.0, close=108.0, minute=2, qty=50.0)
    )

    assert scale_out.action == ExitAction.PARTIAL_EXIT
    assert one_reversal.action in (ExitAction.HOLD, ExitAction.UPDATE_PROTECTION)
    assert ReasonCode.CAUSAL_PEAK_REVERSAL not in one_reversal.reason_codes


def test_disabling_causal_peak_capture_preserves_old_runner_hold() -> None:
    """The kill-switch makes the pre-feature runner behavior explicit."""
    config = PISConfig(scale_out_target_r=1.0, enable_causal_peak_capture=False)
    brain = _long_brain(config=config)

    _, _, decision = _scale_out_and_reversal(brain)

    assert decision.action in (ExitAction.HOLD, ExitAction.UPDATE_PROTECTION)
    assert ReasonCode.CAUSAL_PEAK_REVERSAL not in decision.reason_codes


def test_peak_r_ignores_fresher_quote_outside_completed_bar() -> None:
    """A quote beyond the latest bar cannot become a replay peak-R value."""
    brain = _long_brain()

    decision = brain.observe(
        _obs(
            current=130.0,
            high=110.0,
            low=99.5,
            close=109.0,
            minute=1,
        )
    )

    assert decision.peak_r == 2.0
    assert brain.peak_r == 2.0


def test_confirmed_peak_reversal_harvests_a_short_runner() -> None:
    """The same causal rule is directional and works for a short runner."""
    def short_obs(
        *, current: float, high: float, low: float, close: float, minute: int, qty: float = 100.0
    ) -> PositionObservation:
        return _obs(
            current=current,
            high=high,
            low=low,
            close=close,
            minute=minute,
            side=-1.0,
            initial_stop=105.0,
            qty=qty,
        )

    brain = PositionBrain(
        "TEST",
        short_obs(current=100.0, high=101.0, low=100.0, close=100.0, minute=0),
        PISConfig(scale_out_target_r=1.0),
    )
    scale_out = brain.observe(
        short_obs(current=91.0, high=100.5, low=90.0, close=91.0, minute=1)
    )
    first_reversal = brain.observe(
        short_obs(current=92.0, high=93.0, low=91.5, close=92.0, minute=2, qty=50.0)
    )
    confirmed_reversal = brain.observe(
        short_obs(current=92.5, high=93.5, low=92.0, close=92.5, minute=3, qty=50.0)
    )

    assert scale_out.action == ExitAction.PARTIAL_EXIT
    assert first_reversal.action in (ExitAction.HOLD, ExitAction.UPDATE_PROTECTION)
    assert confirmed_reversal.action == ExitAction.FULL_EXIT
    assert ReasonCode.CAUSAL_PEAK_REVERSAL in confirmed_reversal.reason_codes
    assert confirmed_reversal.peak_r == 2.0


def test_causal_capture_only_ratchets_protection_and_hard_stop_still_wins() -> None:
    """Causal harvesting cannot weaken a floor or replace an actual stop breach."""
    brain = _long_brain()
    scale_out, first_reversal, confirmed_reversal = _scale_out_and_reversal(brain)

    assert scale_out.target_stop_loss is not None
    assert confirmed_reversal.target_stop_loss >= scale_out.target_stop_loss

    hard_stop_brain = _long_brain()
    hard_stop_brain.observe(
        _obs(current=109.0, high=110.0, low=99.5, close=109.0, minute=1)
    )
    hard_stop_decision = hard_stop_brain.observe(
        _obs(current=106.5, high=107.0, low=105.5, close=106.5, minute=2, qty=50.0)
    )

    assert hard_stop_decision.action == ExitAction.FULL_EXIT
    assert ReasonCode.PROTECTION_BREACH in hard_stop_decision.reason_codes
