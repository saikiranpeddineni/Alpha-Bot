from __future__ import annotations

import collections
import json
from datetime import UTC, datetime
from app.db.database import Database
from app.core.logging import applog


def get_rvol_bucket(rvol: float | None) -> str:
    """Classifies RVOL into discrete buckets."""
    if rvol is None:
        return "normal"
    if rvol < 0.5:
        return "low"
    elif rvol <= 1.0:
        return "normal"
    elif rvol <= 1.8:
        return "above_normal"
    else:
        return "high"


def get_vol_bucket(spy_vol: float | None) -> str:
    """Classifies market/SPY volatility into discrete buckets."""
    if spy_vol is None:
        return "normal"
    if spy_vol < 0.05:
        return "low"
    elif spy_vol <= 0.12:
        return "normal"
    else:
        return "high"


def calculate_shrunk_expectancy(
    local_r: float, local_n: int, global_r: float, global_n: int, K: float = 10.0
) -> float:
    """Applies Bayesian Shrinkage to local expectancy using global strategy baseline as prior.
    
    Expectancy = realized_r / count.
    Formula:
    Expectancy_shrunk = (local_r + K * (global_r / global_n)) / (local_n + K)
    """
    global_expectancy = 0.0
    if global_n > 0:
        global_expectancy = global_r / global_n

    return (local_r + K * global_expectancy) / (local_n + K)


def refresh_contextual_memory(db: Database) -> None:
    """Queries completed shadow ledger records and compiles Level 1 aggregate statistics with shrinkage."""
    try:
        # Load last 5000 shadow signals to aggregate over a wide window
        records = db.get_shadow_signals(limit=5000)
    except Exception as exc:
        applog.error("memory", f"Failed to retrieve shadow signals for EOD refresh: {exc}")
        return

    # 1. Calculate global aggregates per (strategy, direction) to serve as shrinkage priors
    # Key: (strategy, direction) -> (total_r, count)
    global_stats = collections.defaultdict(lambda: [0.0, 0])
    
    # 2. Group records by Level 1 context keys
    # Key: (strategy, direction, regime, clock_phase, rvol_bucket, vol_bucket) -> (total_r, count, wins)
    local_stats = collections.defaultdict(lambda: [0.0, 0, 0])

    for r in records:
        # We only aggregate completed setups that have forward outcomes calculated
        outcomes = r.get("outcomes")
        if not outcomes:
            continue

        strategy = r.get("strategy")
        direction = r.get("direction")
        regime = r.get("regime") or "RANGE"
        clock_phase = r.get("clock_phase") or "midday"
        
        # Extract RVOL and Volatility from payload to bucket
        rvol = r.get("slot_rvol")
        
        # Look for spy_vol in the payload dictionary
        spy_vol = r.get("spy_vol")

        rvol_b = get_rvol_bucket(rvol)
        vol_b = get_vol_bucket(spy_vol)

        # Retrieve realized R from 15m outcome or fallback to close realized R
        realized_r = 0.0
        if "15m" in outcomes:
            realized_r = outcomes["15m"].get("realized_r", 0.0)
        elif "close" in outcomes:
            realized_r = outcomes["close"].get("realized_r", 0.0)

        is_win = 1 if realized_r > 0.0 else 0

        # Accumulate global
        g_key = (strategy, direction)
        global_stats[g_key][0] += realized_r
        global_stats[g_key][1] += 1

        # Accumulate local
        l_key = (strategy, direction, regime, clock_phase, rvol_b, vol_b)
        local_stats[l_key][0] += realized_r
        local_stats[l_key][1] += 1
        local_stats[l_key][2] += is_win

    # 3. Calculate shrunk expectancy for each local context and upsert to database
    rows_to_save = []
    for l_key, vals in local_stats.items():
        strategy, direction, regime, clock_phase, rvol_b, vol_b = l_key
        local_r, local_n, local_wins = vals
        
        g_key = (strategy, direction)
        global_r, global_n = global_stats[g_key]

        shrunk_exp = calculate_shrunk_expectancy(local_r, local_n, global_r, global_n, K=10.0)

        rows_to_save.append({
            "strategy": strategy,
            "direction": direction,
            "regime": regime,
            "clock_phase": clock_phase,
            "rvol_bucket": rvol_b,
            "vol_bucket": vol_b,
            "trades_count": local_n,
            "wins_count": local_wins,
            "total_realized_r": round(local_r, 4),
            "shrunk_expectancy": round(shrunk_exp, 4),
        })

    if rows_to_save:
        try:
            # Clear old memory aggregates first to refresh cleanly
            db.clear_context_aggregates()
            db.save_context_aggregates(rows_to_save)
            applog.info("memory", f"Contextual aggregates refresh complete: upserted {len(rows_to_save)} contexts.")
        except Exception as exc:
            applog.error("memory", f"Failed to save contextual aggregate memory: {exc}")


def get_contextual_expectancy(
    db: Database,
    strategy: str,
    direction: str,
    regime: str,
    clock_phase: str,
    rvol: float | None,
    spy_vol: float | None,
) -> dict:
    """Retrieves Contextual Performance expectancy, falling back to neutral/global if samples are low."""
    if db is None:
        return {
            "trades_count": 0,
            "wins_count": 0,
            "expectancy": 0.0,
            "shrunk_expectancy": 0.0,
            "source": "default_prior",
        }

    rvol_b = get_rvol_bucket(rvol)
    vol_b = get_vol_bucket(spy_vol)

    key = {
        "strategy": strategy,
        "direction": direction,
        "regime": regime,
        "clock_phase": clock_phase,
        "rvol_bucket": rvol_b,
        "vol_bucket": vol_b,
    }

    try:
        agg = db.get_context_aggregate(key)
        if agg:
            return {
                "trades_count": agg["trades_count"],
                "wins_count": agg["wins_count"],
                "expectancy": round(agg["total_realized_r"] / agg["trades_count"], 4) if agg["trades_count"] > 0 else 0.0,
                "shrunk_expectancy": agg["shrunk_expectancy"],
                "source": "contextual_memory",
            }
    except Exception as exc:
        applog.error("memory", f"Failed to retrieve context aggregate for {key}: {exc}")

    # Fallback to zero/neutral expectancy
    return {
        "trades_count": 0,
        "wins_count": 0,
        "expectancy": 0.0,
        "shrunk_expectancy": 0.0,
        "source": "default_prior",
    }


def get_episodic_nearest_neighbors(
    db: Database | None,
    current_features: dict,
    k: int = 5,
) -> list[dict]:
    """Retrieves the k nearest historical candidates using Euclidean distance over continuous features.
    
    Features compared: slot_rvol, spy_vol, vwap_distance_atr, ml_confidence.
    """
    if db is None:
        return []

    import math
    try:
        # Load past candidate records
        records = db.get_shadow_signals(limit=2000)
    except Exception as exc:
        applog.error("memory", f"Failed to load shadow signals for Level 2 episodic search: {exc}")
        return []

    def clean(val: float | None, default: float) -> float:
        try:
            return float(val) if val is not None else default
        except Exception:
            return default

    t_rvol = clean(current_features.get("slot_rvol"), 1.0)
    t_vol = clean(current_features.get("spy_vol"), 0.08)
    t_vwap = clean(current_features.get("vwap_distance_atr"), 0.0)
    t_ml = clean(current_features.get("ml_confidence"), 0.5)

    target_vector = [t_rvol, t_vol, t_vwap, t_ml]

    scored_neighbors = []
    for r in records:
        outcomes = r.get("outcomes")
        if not outcomes:
            continue

        rvol = clean(r.get("slot_rvol"), 1.0)
        vol = clean(r.get("spy_vol"), 0.08)
        vwap = clean(r.get("vwap_distance_atr"), 0.0)
        ml = clean(r.get("ml_confidence"), 0.5)

        vector = [rvol, vol, vwap, ml]
        dist = math.sqrt(sum((x - y) ** 2 for x, y in zip(target_vector, vector)))

        # Retrieve outcomes
        realized_r = 0.0
        if "15m" in outcomes:
            realized_r = outcomes["15m"].get("realized_r", 0.0)
        elif "close" in outcomes:
            realized_r = outcomes["close"].get("realized_r", 0.0)

        scored_neighbors.append({
            "candidate_id": r.get("candidate_id"),
            "strategy": r.get("strategy"),
            "direction": r.get("direction"),
            "distance": round(dist, 4),
            "realized_r": realized_r,
            "final_decision": r.get("final_decision"),
            "timestamp": r.get("timestamp"),
        })

    # Sort by distance ascending and take top k
    scored_neighbors.sort(key=lambda x: x["distance"])
    return scored_neighbors[:k]

