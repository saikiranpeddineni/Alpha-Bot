"""Causal adaptive profit-harvesting policy for replay and shadow evaluation.

V2 is intentionally runner-first. Reaching an MFE threshold alone never tightens
risk. Profit protection activates only after a completed bar confirms meaningful
giveback from a sufficiently mature excursion. Decisions use prior completed-bar
state only; the ambiguous fill bar remains excluded from replay.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HarvestConfig:
    """Runner-first, giveback-confirmed R-based harvesting policy."""

    # V1's unconditional 0.75R breakeven promotion harmed 73% of evidenced
    # historical winners. V2 deliberately leaves early winners untouched.
    activation_mfe_r: float = 1.50
    runner_mfe_r: float = 2.50
    exceptional_mfe_r: float = 4.00
    activation_giveback_r: float = 0.75
    runner_giveback_r: float = 1.00
    exceptional_giveback_r: float = 1.25
    lock_floor_r: float = 0.25
    runner_floor_r: float = 0.75
    exceptional_floor_r: float = 1.50
    lock_atr_mult: float = 2.75
    runner_atr_mult: float = 3.25
    exceptional_atr_mult: float = 3.50


@dataclass(frozen=True)
class HarvestDecision:
    state: str
    stop_price: float
    prior_mfe_r: float
    prior_giveback_r: float
    locked_r: float


def _validate_initial_risk(side: str, entry_price: float, initial_stop: float) -> float:
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    if side == "long" and initial_stop >= entry_price:
        raise ValueError("long initial_stop must be below entry_price")
    if side == "short" and initial_stop <= entry_price:
        raise ValueError("short initial_stop must be above entry_price")
    return abs(entry_price - initial_stop)


def adaptive_stop(*, direction: str, entry_price: float, initial_stop: float,
                  prior_peak: float, atr: float, prior_close: float | None = None,
                  current_stop: float | None = None,
                  config: HarvestConfig | None = None) -> HarvestDecision:
    """Return a monotonic V2 stop using prior completed-bar state only.

    A high-water mark by itself is not a reason to harvest. Protection is armed
    only when the prior completed close has surrendered enough R from that peak.
    This prevents ordinary pullbacks on developing runners from causing the V1
    0.05R/0.75R choke observed in empirical validation.
    """
    cfg = config or HarvestConfig()
    side = direction.lower()
    if side not in {"long", "short"}:
        raise ValueError("direction must be 'long' or 'short'")
    if atr < 0:
        raise ValueError("atr cannot be negative")
    risk = _validate_initial_risk(side, entry_price, initial_stop)

    if side == "long":
        favourable = max(0.0, prior_peak - entry_price)
        base_stop = max(initial_stop, current_stop if current_stop is not None else initial_stop)
        giveback = max(0.0, prior_peak - (prior_close if prior_close is not None else prior_peak))
    else:
        favourable = max(0.0, entry_price - prior_peak)
        base_stop = min(initial_stop, current_stop if current_stop is not None else initial_stop)
        giveback = max(0.0, (prior_close if prior_close is not None else prior_peak) - prior_peak)

    mfe_r = favourable / risk
    giveback_r = giveback / risk
    state = "runner_preserve"
    floor_r = None
    atr_mult = None

    if mfe_r >= cfg.exceptional_mfe_r and giveback_r >= cfg.exceptional_giveback_r:
        state, floor_r, atr_mult = "exceptional_giveback", cfg.exceptional_floor_r, cfg.exceptional_atr_mult
    elif mfe_r >= cfg.runner_mfe_r and giveback_r >= cfg.runner_giveback_r:
        state, floor_r, atr_mult = "runner_giveback", cfg.runner_floor_r, cfg.runner_atr_mult
    elif mfe_r >= cfg.activation_mfe_r and giveback_r >= cfg.activation_giveback_r:
        state, floor_r, atr_mult = "confirmed_giveback", cfg.lock_floor_r, cfg.lock_atr_mult

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
    return HarvestDecision(state, float(stop), float(mfe_r), float(giveback_r), float(locked_r))


def replay_adaptive_harvest(df: Any, *, entry_idx: int, direction: str,
                            entry_price: float, initial_stop: float,
                            max_bars: int = 60,
                            config: HarvestConfig | None = None) -> dict[str, Any]:
    """Causally replay V2 over complete post-entry OHLC bars.

    ``entry_idx`` is the fill-containing bar and is excluded because OHLC cannot
    establish whether its extremes occurred before or after the fill. Each stop
    decision uses only peak/close/ATR information from completed prior bars.
    """
    if entry_idx < 0 or entry_idx >= len(df):
        return {}
    side = direction.lower()
    if side not in {"long", "short"}:
        return {"replay_valid": False, "failure_reason": "invalid_direction"}
    try:
        risk = _validate_initial_risk(side, entry_price, initial_stop)
    except ValueError as exc:
        return {"replay_valid": False, "failure_reason": str(exc)}

    start_idx = entry_idx + 1
    bars = df.iloc[start_idx: start_idx + max_bars]
    if bars.empty:
        return {"replay_valid": False, "failure_reason": "no_complete_post_entry_bars"}

    peak = entry_price
    prior_close = entry_price
    prior_atr = 0.0
    mfe_price = entry_price
    mae_price = entry_price
    current_stop = initial_stop
    exit_price = None
    exit_reason = "time"
    exit_ts = None
    state = "runner_preserve"
    bars_held = 0

    for step, (bar_ts, bar) in enumerate(bars.iterrows()):
        bars_held = step + 1
        high, low = float(bar["high"]), float(bar["low"])
        open_price = float(bar["open"])

        # Strict causality: even ATR is taken from the previous completed bar.
        decision = adaptive_stop(
            direction=side, entry_price=entry_price, initial_stop=initial_stop,
            prior_peak=peak, prior_close=prior_close, atr=max(0.0, prior_atr),
            current_stop=current_stop, config=config,
        )
        current_stop, state = decision.stop_price, decision.state

        stop_hit = low <= current_stop if side == "long" else high >= current_stop
        if stop_hit:
            if side == "long":
                exit_price = open_price if open_price < current_stop else current_stop
                mae_price = min(mae_price, exit_price)
                mfe_price = max(mfe_price, open_price)
            else:
                exit_price = open_price if open_price > current_stop else current_stop
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
        prior_close = float(bar["close"])
        prior_atr = float(bar.get("atr14", 0.0) or 0.0)

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
        "policy_version": "v2_giveback_confirmed",
        "entry_price": round(entry_price, 4), "exit_price": round(exit_price, 4),
        "exit_reason": exit_reason, "exit_timestamp": str(exit_ts), "bars_held": bars_held,
        "harvest_state": state, "final_stop": round(current_stop, 4),
        "realized_r": round(realized_r, 4), "mfe_r": round(mfe_r, 4), "mae_r": round(mae_r, 4),
        "winner_capture_ratio": round(capture, 4), "gave_back_r": round(giveback, 4),
        "pnl_pct": round((pnl / entry_price) * 100.0, 4),
    }
