"""Causal adaptive profit-harvesting policy for replay and shadow evaluation.

This module does not gate entries or increase initial risk. It converts the
prior-bar maximum favourable excursion (MFE) into a monotonic protective stop.
All decisions are based only on information available before the current bar.
"""

from dataclasses import dataclass
from typing import Any


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


def adaptive_stop(*, direction: str, entry_price: float, initial_stop: float,
                  prior_peak: float, atr: float, current_stop: float | None = None,
                  config: HarvestConfig | None = None) -> HarvestDecision:
    """Return a monotonic stop using only prior-bar state."""
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
    floor_r = None
    atr_mult = None
    if mfe_r >= cfg.exceptional_trigger_r:
        state, floor_r, atr_mult = "exceptional_runner", cfg.exceptional_floor_r, cfg.exceptional_atr_mult
    elif mfe_r >= cfg.runner_trigger_r:
        state, floor_r, atr_mult = "runner", cfg.runner_floor_r, cfg.runner_atr_mult
    elif mfe_r >= cfg.lock_trigger_r:
        state, floor_r, atr_mult = "profit_lock", cfg.lock_floor_r, cfg.lock_atr_mult
    elif mfe_r >= cfg.breakeven_trigger_r:
        state, floor_r = "breakeven", cfg.breakeven_lock_r

    stop = base_stop
    if floor_r is not None:
        if side == "long":
            stop = max(stop, entry_price + floor_r * risk)
            if atr_mult is not None and atr > 0:
                stop = max(stop, prior_peak - atr_mult * atr)
        else:
            stop = min(stop, entry_price - floor_r * risk)
            if atr_mult is not None and atr > 0:
                stop = min(stop, prior_peak + atr_mult * atr)

    locked_r = ((stop - entry_price) / risk) if side == "long" else ((entry_price - stop) / risk)
    return HarvestDecision(state, float(stop), float(mfe_r), float(locked_r))


def replay_adaptive_harvest(df: Any, *, entry_idx: int, direction: str,
                            entry_price: float, initial_stop: float,
                            max_bars: int = 60,
                            config: HarvestConfig | None = None) -> dict[str, Any]:
    """Causally replay adaptive harvesting over OHLC bars.

    Stop for each bar is calculated from the *previous* peak. Intrabar ambiguity
    is resolved conservatively: if the original stop and favourable excursion
    could both occur on the same bar, the stop is credited first. No fixed TP is
    used, intentionally preserving right-tail participation.
    """
    if entry_idx < 0 or entry_idx >= len(df):
        return {}
    side = direction.lower()
    risk = abs(entry_price - initial_stop)
    if risk <= 1e-12:
        return {"replay_valid": False, "failure_reason": "zero_initial_risk"}

    bars = df.iloc[entry_idx: entry_idx + max_bars]
    if bars.empty:
        return {}

    peak = entry_price
    mfe_price = entry_price
    mae_price = entry_price
    current_stop = initial_stop
    exit_price = None
    exit_reason = "time"
    exit_ts = None
    state = "initial_risk"
    bars_held = 0

    for step, (bar_ts, bar) in enumerate(bars.iterrows()):
        bars_held = step + 1
        high, low = float(bar["high"]), float(bar["low"])
        open_price, close = float(bar["open"]), float(bar["close"])
        atr = float(bar.get("atr14", 0.0) or 0.0)
        decision = adaptive_stop(direction=side, entry_price=entry_price,
                                 initial_stop=initial_stop, prior_peak=peak, atr=max(0.0, atr),
                                 current_stop=current_stop, config=config)
        current_stop, state = decision.stop_price, decision.state

        stop_hit = low <= current_stop if side == "long" else high >= current_stop
        if stop_hit:
            # Gap-through stops cannot receive a better fill than the bar open.
            if side == "long":
                exit_price = min(open_price, current_stop) if open_price < current_stop else current_stop
                mae_price = min(mae_price, exit_price)
                mfe_price = max(mfe_price, open_price)
            else:
                exit_price = max(open_price, current_stop) if open_price > current_stop else current_stop
                mae_price = max(mae_price, exit_price)
                mfe_price = min(mfe_price, open_price)
            exit_reason, exit_ts = "adaptive_stop", bar_ts
            break

        if side == "long":
            mfe_price, mae_price = max(mfe_price, high), min(mae_price, low)
            peak = max(peak, high)
        else:
            mfe_price, mae_price = min(mfe_price, low), max(mae_price, high)
            peak = min(peak, low)

    if exit_price is None:
        exit_price = float(bars["close"].iloc[-1])
        exit_ts = bars.index[-1]

    pnl = exit_price - entry_price if side == "long" else entry_price - exit_price
    mfe = mfe_price - entry_price if side == "long" else entry_price - mfe_price
    mae = entry_price - mae_price if side == "long" else mae_price - entry_price
    realized_r, mfe_r, mae_r = pnl / risk, mfe / risk, mae / risk
    capture = realized_r / mfe_r if realized_r > 0 and mfe_r > 0 else 0.0
    giveback = mfe_r - realized_r
    return {
        "replay_valid": True,
        "entry_price": round(entry_price, 4), "exit_price": round(exit_price, 4),
        "exit_reason": exit_reason, "exit_timestamp": str(exit_ts), "bars_held": bars_held,
        "harvest_state": state, "final_stop": round(current_stop, 4),
        "realized_r": round(realized_r, 4), "mfe_r": round(mfe_r, 4), "mae_r": round(mae_r, 4),
        "winner_capture_ratio": round(capture, 4), "gave_back_r": round(giveback, 4),
        "pnl_pct": round((pnl / entry_price) * 100.0, 4),
    }
