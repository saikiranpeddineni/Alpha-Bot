from __future__ import annotations

import collections
import json
import math
from datetime import UTC, datetime
from app.db.database import Database
from app.core.applog import applog


# Strategy-specific evaluation horizons for canonical realized_r attribution.
# Horizons are tried in priority order; first match in the outcomes dict wins.
# Fallback: "close" if none of the listed horizons are present.
STRATEGY_HORIZON: dict[str, list[str]] = {
    "rsi2_reversion": ["15m", "30m", "close"],
    "vwap_reversion": ["15m", "30m", "close"],
    "orb":            ["60m", "close"],
    "momentum":       ["60m", "close"],
    "bb_squeeze":     ["60m", "close"],
}
_DEFAULT_HORIZON = ["15m", "close"]  # fallback for unknown strategies


def _canonical_realized_r(strategy: str | None, outcomes: dict) -> float | None:
    """Returns the canonical realized R for a candidate using strategy-specific horizon priority (#238).

    Preserves 0.0 as a valid 0R breakeven trade. Returns None if R evidence is missing/invalid.
    """
    from app.core.canonical_evidence import CanonicalOutcomeResolver
    horizons = STRATEGY_HORIZON.get(strategy or "", _DEFAULT_HORIZON)
    for h in horizons:
        if h in outcomes:
            val = outcomes[h].get("realized_r")
            if val is not None:
                return float(val)
    return None


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
        regime = r.get("regime") or r.get("market_regime") or "RANGE"
        clock_phase = r.get("clock_phase") or "midday"
        
        # Extract RVOL and Volatility from payload to bucket
        rvol = r.get("slot_rvol")
        
        # Look for spy_vol in the payload dictionary
        spy_vol = r.get("spy_vol")

        rvol_b = get_rvol_bucket(rvol)
        vol_b = get_vol_bucket(spy_vol)

        # Retrieve canonical realized R using strategy-specific horizon priority (Fix 8)
        realized_r = _canonical_realized_r(strategy, outcomes)
        if realized_r is None:
            continue

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
    """Retrieves the k nearest historical candidates using z-score normalized Euclidean distance.

    Features compared: slot_rvol, spy_vol, vwap_distance_atr, ml_confidence.

    Fix 7 changes vs original:
    - Records are first partitioned by (strategy, direction) to prevent cross-strategy contamination
      (e.g., 'ORB long TREND' cannot be similar to 'vwap_reversion short RANGE').
    - Euclidean distance is computed over z-score normalized features so that RVOL (range 0.3–3.0)
      does not dominate ml_confidence (range 0.45–0.65) purely due to scale difference.
    - Falls back to raw distance if any feature's std dev is zero.
    """
    if db is None:
        return []

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

    # Step 1: Partition by (strategy, direction) — cross-strategy neighbors are meaningless
    target_strategy = current_features.get("strategy")
    target_direction = current_features.get("direction")

    candidate_pool = [
        r for r in records
        if r.get("outcomes")  # must have completed outcomes
        and (target_strategy is None or r.get("strategy") == target_strategy)
        and (target_direction is None or r.get("direction") == target_direction)
    ]

    if not candidate_pool:
        return []

    # Step 2: Collect raw feature vectors from the partitioned pool
    FEATURE_KEYS = ["slot_rvol", "spy_vol", "vwap_distance_atr", "ml_confidence"]
    FEATURE_DEFAULTS = {"slot_rvol": 1.0, "spy_vol": 0.08, "vwap_distance_atr": 0.0, "ml_confidence": 0.5}

    pool_vectors: list[list[float]] = []
    for r in candidate_pool:
        pool_vectors.append([clean(r.get(k), FEATURE_DEFAULTS[k]) for k in FEATURE_KEYS])

    # Step 3: Compute per-feature mean and std across the pool for z-score normalization
    n_pool = len(pool_vectors)
    feat_means = [sum(v[i] for v in pool_vectors) / n_pool for i in range(len(FEATURE_KEYS))]
    feat_stds = [
        math.sqrt(sum((v[i] - feat_means[i]) ** 2 for v in pool_vectors) / n_pool)
        for i in range(len(FEATURE_KEYS))
    ]

    def z_normalize(raw_vector: list[float]) -> list[float]:
        """Z-score normalize; falls back to raw value if std is zero (constant feature)."""
        return [
            (raw_vector[i] - feat_means[i]) / feat_stds[i] if feat_stds[i] > 1e-9 else raw_vector[i]
            for i in range(len(FEATURE_KEYS))
        ]

    # Step 4: Normalize the query vector
    target_raw = [clean(current_features.get(k), FEATURE_DEFAULTS[k]) for k in FEATURE_KEYS]
    target_vector = z_normalize(target_raw)

    # Step 5: Score each candidate using normalized Euclidean distance
    scored_neighbors = []
    for r, raw_vec in zip(candidate_pool, pool_vectors):
        norm_vec = z_normalize(raw_vec)
        dist = math.sqrt(sum((x - y) ** 2 for x, y in zip(target_vector, norm_vec)))
        outcomes = r.get("outcomes", {})
        realized_r = _canonical_realized_r(r.get("strategy"), outcomes)

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

def get_adaptive_ml_threshold(base_threshold: float, exp_info: dict, sctx=None) -> float:
    threshold = base_threshold
    
    # Apply dynamic opening slippage handicap
    if sctx is not None:
        try:
            from zoneinfo import ZoneInfo
            now_et = sctx.now.astimezone(ZoneInfo("America/New_York"))
            mins_et = now_et.hour * 60 + now_et.minute
            mins_since_open = mins_et - (9 * 60 + 30)
            
            if 0 <= mins_since_open < 15:
                # Force AI to have a much higher confidence to survive the violent $4 slippage
                threshold += 0.15
        except Exception:
            pass
            
    return threshold

def get_adaptive_signal_score(base_score: float, exp_info: dict) -> float:
    return base_score
