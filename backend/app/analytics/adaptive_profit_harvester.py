"""Causal adaptive profit-harvesting policy for replay and shadow evaluation.

This module does not gate entries or increase initial risk.  It converts the
prior-bar maximum favourable excursion (MFE) into a monotonic protective stop.
All decisions are based only on information available before the current bar,
so callers can use it in replay without look-ahead bias.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class HarvestConfig:
    """R-based profit states with volatility-aware runner protection."""

    breakeven_trigger_r: float = 0.75
    lock_trigger_r: float = 1.50
    runner_trigger_r: float = 2.50
    exceptional_trigger_r: float = 4.00
    breakeven_lock_r: float = 0.05
    lock_floor_r: float = 0.75
    runner_floor_r: float = 1.50
    exceptional_floor_r: float = 2.75
    lock_atr_mult: float = 2.25
    runner_atr_mult: float = 2.75
    exceptional_atr_mult: float = 2.25


@dataclass(frozen=True)
class HarvestDecision:
    state: str
    stop_price: float
    prior_mfe_r: float
    locked_r: float


def adaptive_stop(
    *,
    direction: str,
    entry_price: float,
    initial_stop: float,
    prior_peak: float,
    atr: float,
    current_stop: float | None = None,
    config: HarvestConfig | None = None,
) -> HarvestDecision:
    """Return a monotonic stop using only prior-bar state.

    The original stop is never widened.  Before +0.75R there is deliberately no
    extra protection, allowing normal price discovery.  As MFE expands, a
    progressively protected floor is combined with a volatility-aware trail.
    The runner trail widens at +2.5R so strong trends are not choked, then tightens
    only after an exceptional +4R excursion.
    """
    cfg = config or HarvestConfig()
    side = direction.lower()
    if side not in {"long", "short"}:
        raise ValueError("direction must be 'long' or 'short'")
    if entry_price <= 0 or atr < 0:
        raise ValueError("entry_price must be positive and atr cannot be negative")

    risk = abs(entry_price - initial_stop)
    if risk <= 1e-12:
        raise ValueError("initial_stop must define non-zero risk")

    if side == "long":
        favourable = max(0.0, prior_peak - entry_price)
        base_stop = max(initial_stop, current_stop if current_stop is not None else initial_stop)
    else:
        favourable = max(0.0, entry_price - prior_peak)
        base_stop = min(initial_stop, current_stop if current_stop is not None else initial_stop)
    mfe_r = favourable / risk

    state = "initial_risk"
    floor_r: float | None = None
    atr_mult: float | None = None

    if mfe_r >= cfg.exceptional_trigger_r:
        state = "exceptional_runner"
        floor_r = cfg.exceptional_floor_r
        atr_mult = cfg.exceptional_atr_mult
    elif mfe_r >= cfg.runner_trigger_r:
        state = "runner"
        floor_r = cfg.runner_floor_r
        atr_mult = cfg.runner_atr_mult
    elif mfe_r >= cfg.lock_trigger_r:
        state = "profit_lock"
        floor_r = cfg.lock_floor_r
        atr_mult = cfg.lock_atr_mult
    elif mfe_r >= cfg.breakeven_trigger_r:
        state = "breakeven"
        floor_r = cfg.breakeven_lock_r

    if floor_r is None:
        stop = base_stop
    else:
        if side == "long":
            floor_price = entry_price + floor_r * risk
            stop = max(base_stop, floor_price)
            if atr_mult is not None and atr > 0:
                stop = max(stop, prior_peak - atr_mult * atr)
        else:
            floor_price = entry_price - floor_r * risk
            stop = min(base_stop, floor_price)
            if atr_mult is not None and atr > 0:
                stop = min(stop, prior_peak + atr_mult * atr)

    locked_r = ((stop - entry_price) / risk) if side == "long" else ((entry_price - stop) / risk)
    return HarvestDecision(
        state=state,
        stop_price=float(stop),
        prior_mfe_r=float(mfe_r),
        locked_r=float(locked_r),
    )
