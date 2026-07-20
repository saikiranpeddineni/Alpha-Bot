"""CR-047 — Confidence-aware multi-signal market risk regime.

Replaces the SPY-only boolean/tri-state panic gate with a typed
``MarketRiskState`` that aggregates five independent signal families:

  1. broad_market   — SPY ATR/vol percentile
  2. qqq            — QQQ ATR/vol percentile (independent confirmation)
  3. breadth        — advancing/declining ratio across the live universe
  4. universe_stress — fraction of universe symbols showing extreme vol
  5. data_health    — overall provider health derived from family error rates

Aggregation rules
-----------------
* Each family casts a ``SignalVote`` with a state and a ``SignalHealth``
  (healthy flag, confidence 0..1, source timestamp, reason).
* A family that raises or returns unhealthy data casts ``UNKNOWN`` with
  confidence 0.  It can NEVER cast ``NORMAL``.
* The aggregator weights votes by ``health.confidence``.
* ``UNKNOWN`` is returned when:
    - fewer than 2 families are healthy, OR
    - weighted confidence < 0.40.
* Deterministic: same inputs at timestamp T always produce same output.
* No future-data leakage: every history fetch is bounded by ``as_of``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

import inspect
import pandas as pd


async def _safe_history(
    data: Any, symbol: str, timeframe: str, start: datetime, end: datetime, prefer_cache: bool = True
) -> pd.DataFrame:
    if data is None:
        return pd.DataFrame()
    try:
        fn = getattr(data, "history", None) or getattr(data, "frame", None)
        if fn is None:
            return pd.DataFrame()
        try:
            res = fn(symbol, timeframe, start, end, prefer_cache=prefer_cache)
        except TypeError:
            try:
                res = fn(symbol, timeframe, start, end)
            except TypeError:
                res = fn(symbol, timeframe)
        if inspect.isawaitable(res):
            res = await res
        return res if (isinstance(res, pd.DataFrame) or hasattr(res, "index")) else pd.DataFrame()
    except Exception as exc:
        applog.debug("market_regime", f"_safe_history failed for {symbol}: {exc}")
        return pd.DataFrame()

from app.core.logging import applog


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class MarketRiskState(Enum):
    """Typed market risk classification.

    NORMAL  -- sufficient authoritative evidence of normal conditions.
    CAUTION -- moderate deterioration; entries allowed but risk scaled.
    STRESS  -- material broad/cross-sectional deterioration; deploy defensives.
    PANIC   -- confirmed extreme systemic conditions; deploy defensives.
    UNKNOWN -- insufficient authoritative evidence; fail closed for new entries.
               Missing evidence of stress is NOT evidence of safety.
    """
    NORMAL  = "NORMAL"
    CAUTION = "CAUTION"
    STRESS  = "STRESS"
    PANIC   = "PANIC"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SignalHealth:
    """Health/confidence metadata for a single signal family."""
    healthy: bool
    reason: str
    source_ts: datetime | None = None
    confidence: float = 1.0  # 0..1; 0 = completely unreliable

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence", max(0.0, min(1.0, float(self.confidence))))


@dataclass(frozen=True)
class SignalVote:
    """A single family's classification vote."""
    family: str
    state: MarketRiskState
    health: SignalHealth
    reason: str


# ---------------------------------------------------------------------------
# ATR percentile helper (shared by SPY and QQQ families)
# ---------------------------------------------------------------------------

def _atr_percentile(df_min: pd.DataFrame) -> tuple[float | None, str]:
    """Compute the current ATR% percentile from 1-min bars.

    Returns (percentile 0..1, reason_str) or (None, reason_str) on failure.
    """
    if df_min.empty or len(df_min) < 200:
        return None, f"insufficient 1-min bars ({len(df_min)})"

    df_h = df_min.resample("1h").agg(
        {"open": "first", "high": "max", "low": "min",
         "close": "last", "volume": "sum"}
    ).dropna()

    if len(df_h) < 20:
        return None, f"insufficient hourly bars ({len(df_h)})"

    prev = df_h["close"].shift(1)
    tr = pd.concat(
        [df_h["high"] - df_h["low"],
         (df_h["high"] - prev).abs(),
         (df_h["low"] - prev).abs()],
        axis=1,
    ).max(axis=1)
    atr_pct = tr.ewm(alpha=1.0 / 14, adjust=False).mean() / df_h["close"] * 100.0

    last_val = atr_pct.iloc[-1]
    if not math.isfinite(last_val):
        return None, "non-finite ATR value"

    std = atr_pct.std()
    if std < 1e-6:
        pct = 0.0
    else:
        pct = float((atr_pct.tail(200) <= last_val).mean())
    return pct, f"ATR percentile={pct:.3f}"


# ---------------------------------------------------------------------------
# MarketRiskAggregator
# ---------------------------------------------------------------------------

_MIN_HEALTHY_FAMILIES = 2
_MIN_CONFIDENCE = 0.40

# Thresholds — ATR percentile
_PANIC_THRESHOLD  = 0.90
_STRESS_THRESHOLD = 0.80
_CAUTION_THRESHOLD = 0.70

# Breadth thresholds (advancing / total ratio)
_BREADTH_STRESS_BELOW  = 0.35  # < 35% advancing → stress
_BREADTH_CAUTION_BELOW = 0.45  # < 45% advancing → caution

# Universe stress threshold (fraction of symbols with extreme intraday vol)
_UNIVERSE_STRESS_FRAC = 0.40   # >= 40% of symbols in extreme vol → stress
_UNIVERSE_CAUTION_FRAC = 0.25  # >= 25% → caution
_EXTREME_VOL_THRESHOLD = 4.0   # ATR% > 4 = extreme for a single symbol

# Breadth family: minimum symbols for a healthy vote and lookback window.
# 5-day lookback survives weekends + single holidays without starving history.
# Minimum 3 symbols (reduced from 5) allows graceful degradation with lower
# confidence instead of hard UNKNOWN on thin coverage days.
_BREADTH_LOOKBACK_DAYS = 5
_BREADTH_MIN_SYMBOLS = 3
_BREADTH_THIN_COVERAGE_CONFIDENCE = 0.55   # < full confidence when 3-4 symbols
_BREADTH_FULL_COVERAGE_CONFIDENCE = 0.85   # full confidence when >= 5 symbols


class MarketRiskAggregator:
    """Aggregate multi-family market risk signals into a single ``MarketRiskState``.

    Parameters
    ----------
    data_manager:
        Provides ``async history(symbol, timeframe, start, end)``.
    universe_symbols:
        The live eligible universe used for breadth/cross-sectional families.
        Defaults to a minimal representative set when not provided.
    """

    def __init__(
        self,
        data_manager: Any,
        universe_symbols: list[str] | None = None,
    ) -> None:
        self._data = data_manager
        self._universe = universe_symbols or [
            "AAPL", "MSFT", "NVDA", "AMZN", "META",
            "JPM", "BAC", "XLK", "XLF", "QQQ", "SPY",
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def assess(
        self, as_of: datetime | None = None
    ) -> tuple[MarketRiskState, float, float, list[str], dict[str, SignalHealth]]:
        """Run all signal families and return an aggregated risk classification.

        Returns
        -------
        state : MarketRiskState
        confidence : float  (0..1 — weighted average of healthy family confidence)
        coverage : float    (0..1 — fraction of families that were healthy)
        reason_codes : list[str]
        input_healths : dict[family_name, SignalHealth]
        """
        T = as_of or datetime.now(UTC)

        votes: list[SignalVote] = []
        votes.append(await self._family_broad_market("SPY", "broad_market", T))
        votes.append(await self._family_broad_market("QQQ", "qqq", T))
        votes.append(await self._family_breadth(T))
        votes.append(await self._family_universe_stress(T))
        votes.append(self._family_data_health(votes))  # derived — must be last

        healthy_votes = [v for v in votes if v.health.healthy]
        coverage = len(healthy_votes) / len(votes)
        input_healths = {v.family: v.health for v in votes}

        if len(healthy_votes) < _MIN_HEALTHY_FAMILIES:
            reason_codes = ["INSUFFICIENT_HEALTHY_FAMILIES"] + [
                f"{v.family}:UNHEALTHY" for v in votes if not v.health.healthy
            ]
            applog.warning(
                "market_regime",
                f"MarketRiskState=UNKNOWN: only {len(healthy_votes)}/{len(votes)} families healthy. "
                f"coverage={coverage:.2f} reasons={reason_codes}",
            )
            return MarketRiskState.UNKNOWN, 0.0, coverage, reason_codes, input_healths

        total_weight = sum(v.health.confidence for v in healthy_votes)
        weighted_confidence = total_weight / len(healthy_votes) if healthy_votes else 0.0

        if weighted_confidence < _MIN_CONFIDENCE:
            reason_codes = [f"LOW_CONFIDENCE({weighted_confidence:.2f})"]
            applog.warning(
                "market_regime",
                f"MarketRiskState=UNKNOWN: weighted confidence {weighted_confidence:.2f} < {_MIN_CONFIDENCE}. "
                f"coverage={coverage:.2f}",
            )
            return MarketRiskState.UNKNOWN, weighted_confidence, coverage, reason_codes, input_healths

        # Weighted vote aggregation
        state_weights: dict[MarketRiskState, float] = {}
        for v in healthy_votes:
            state_weights[v.state] = state_weights.get(v.state, 0.0) + v.health.confidence

        # Escalate to highest state with meaningful weight
        state = MarketRiskState.NORMAL
        for s in (MarketRiskState.PANIC, MarketRiskState.STRESS, MarketRiskState.CAUTION):
            if state_weights.get(s, 0.0) > 0:
                state = s
                break

        reason_codes = [
            f"{v.family}:{v.state.value}({v.health.confidence:.2f})" for v in healthy_votes
        ]

        applog.info(
            "market_regime",
            f"MarketRiskState={state.value} confidence={weighted_confidence:.2f} "
            f"coverage={coverage:.2f} reasons={reason_codes}",
        )
        return state, weighted_confidence, coverage, reason_codes, input_healths

    # ------------------------------------------------------------------
    # Signal family implementations
    # ------------------------------------------------------------------

    async def _family_broad_market(
        self, symbol: str, family: str, T: datetime
    ) -> SignalVote:
        """SPY or QQQ ATR/vol percentile family."""
        try:
            start = T - timedelta(days=20)
            df = await _safe_history(self._data, symbol, "1Min", start, T, prefer_cache=True)
            pct, reason = _atr_percentile(df)
            if pct is None:
                return SignalVote(
                    family=family,
                    state=MarketRiskState.UNKNOWN,
                    health=SignalHealth(healthy=False, reason=reason, source_ts=T, confidence=0.0),
                    reason=reason,
                )
            if pct >= _PANIC_THRESHOLD:
                state = MarketRiskState.PANIC
            elif pct >= _STRESS_THRESHOLD:
                state = MarketRiskState.STRESS
            elif pct >= _CAUTION_THRESHOLD:
                state = MarketRiskState.CAUTION
            else:
                state = MarketRiskState.NORMAL
            source_ts = df.index[-1].to_pydatetime() if hasattr(df.index[-1], "to_pydatetime") else T
            return SignalVote(
                family=family,
                state=state,
                health=SignalHealth(healthy=True, reason=reason, source_ts=source_ts, confidence=0.9),
                reason=reason,
            )
        except Exception as exc:
            reason = f"{exc.__class__.__name__}: {exc}"
            applog.warning("market_regime", f"{family} family error: {reason} — UNKNOWN vote")
            return SignalVote(
                family=family,
                state=MarketRiskState.UNKNOWN,
                health=SignalHealth(healthy=False, reason=reason, source_ts=T, confidence=0.0),
                reason=reason,
            )

    async def _family_breadth(self, T: datetime) -> SignalVote:
        """Advancing/declining breadth across the live universe.

        Fix (#140): widened lookback from 2d -> 5d so weekends and single
        holidays don't starve the 2-bar history requirement. Minimum healthy
        symbol count relaxed from 5 -> 3 with proportionally reduced confidence
        (0.55) to allow graceful degradation instead of hard UNKNOWN on thin
        coverage pre-market days.
        """
        family = "breadth"
        try:
            start = T - timedelta(days=_BREADTH_LOOKBACK_DAYS)  # was days=2
            advancing = 0
            total = 0
            errors = 0
            for sym in self._universe[:20]:  # cap at 20 for latency
                try:
                    df = await _safe_history(self._data, sym, "1Day", start, T, prefer_cache=True)
                    if len(df) >= 2:
                        advancing += int(float(df["close"].iloc[-1]) >= float(df["close"].iloc[-2]))
                        total += 1
                except Exception:
                    errors += 1

            if total < _BREADTH_MIN_SYMBOLS:  # was total < 5
                return SignalVote(
                    family=family,
                    state=MarketRiskState.UNKNOWN,
                    health=SignalHealth(
                        healthy=False,
                        reason=f"insufficient breadth data ({total} symbols, {errors} errors)",
                        source_ts=T, confidence=0.0,
                    ),
                    reason="insufficient breadth coverage",
                )

            adv_ratio = advancing / total
            error_rate = errors / max(len(self._universe[:20]), 1)

            # Degrade confidence proportionally when coverage is thin (3-4 symbols)
            # vs full coverage (>=5 symbols). High error rate further reduces confidence.
            base_confidence = (
                _BREADTH_FULL_COVERAGE_CONFIDENCE
                if total >= 5
                else _BREADTH_THIN_COVERAGE_CONFIDENCE
            )
            confidence = max(0.3, base_confidence - error_rate)

            if total < 5:
                applog.warning(
                    "market_regime",
                    f"breadth family: thin coverage ({total} symbols) — "
                    f"confidence degraded to {confidence:.2f}. adv_ratio={adv_ratio:.2f}",
                )

            if adv_ratio < _BREADTH_STRESS_BELOW:
                state = MarketRiskState.STRESS
            elif adv_ratio < _BREADTH_CAUTION_BELOW:
                state = MarketRiskState.CAUTION
            else:
                state = MarketRiskState.NORMAL

            reason = f"adv_ratio={adv_ratio:.2f} ({advancing}/{total} advancing)"
            return SignalVote(
                family=family,
                state=state,
                health=SignalHealth(healthy=True, reason=reason, source_ts=T, confidence=confidence),
                reason=reason,
            )
        except Exception as exc:
            reason = f"{exc.__class__.__name__}: {exc}"
            applog.warning("market_regime", f"{family} family error: {reason} — UNKNOWN vote")
            return SignalVote(
                family=family,
                state=MarketRiskState.UNKNOWN,
                health=SignalHealth(healthy=False, reason=reason, source_ts=T, confidence=0.0),
                reason=reason,
            )

    async def _family_universe_stress(
        self, T: datetime
    ) -> SignalVote:
        """Cross-sectional stress: fraction of universe with extreme intraday vol."""
        family = "universe_stress"
        try:
            start = T - timedelta(days=2)
            extreme = 0
            total = 0
            errors = 0
            for sym in self._universe[:20]:
                try:
                    df = await _safe_history(self._data, sym, "1Day", start, T, prefer_cache=True)
                    if not df.empty:
                        atr_pct = float(
                            ((df["high"] - df["low"]) / df["close"] * 100.0).iloc[-1]
                        )
                        if not math.isfinite(atr_pct):
                            errors += 1
                        else:
                            extreme += int(atr_pct >= _EXTREME_VOL_THRESHOLD)
                            total += 1
                except Exception:
                    errors += 1

            if total < 5:
                return SignalVote(
                    family=family,
                    state=MarketRiskState.UNKNOWN,
                    health=SignalHealth(
                        healthy=False,
                        reason=f"insufficient universe data ({total} symbols, {errors} errors)",
                        source_ts=T, confidence=0.0,
                    ),
                    reason="insufficient universe coverage",
                )

            stress_frac = extreme / total
            error_rate = errors / max(len(self._universe[:20]), 1)
            confidence = max(0.3, 0.85 - error_rate)

            if stress_frac >= _UNIVERSE_STRESS_FRAC:
                state = MarketRiskState.STRESS
            elif stress_frac >= _UNIVERSE_CAUTION_FRAC:
                state = MarketRiskState.CAUTION
            else:
                state = MarketRiskState.NORMAL

            reason = f"extreme_vol_frac={stress_frac:.2f} ({extreme}/{total} symbols)"
            return SignalVote(
                family=family,
                state=state,
                health=SignalHealth(healthy=True, reason=reason, source_ts=T, confidence=confidence),
                reason=reason,
            )
        except Exception as exc:
            reason = f"{exc.__class__.__name__}: {exc}"
            applog.warning("market_regime", f"{family} family error: {reason} — UNKNOWN vote")
            return SignalVote(
                family=family,
                state=MarketRiskState.UNKNOWN,
                health=SignalHealth(healthy=False, reason=reason, source_ts=T, confidence=0.0),
                reason=reason,
            )

    @staticmethod
    def _family_data_health(prior_votes: list[SignalVote]) -> SignalVote:
        """Synthetic family: overall provider health derived from prior family votes."""
        family = "data_health"
        total = len(prior_votes)
        unhealthy = sum(1 for v in prior_votes if not v.health.healthy)
        health_rate = (total - unhealthy) / max(total, 1)

        if health_rate < 0.5:
            return SignalVote(
                family=family,
                state=MarketRiskState.UNKNOWN,
                health=SignalHealth(
                    healthy=False,
                    reason=f"provider health_rate={health_rate:.2f} ({unhealthy}/{total} families unhealthy)",
                    confidence=0.0,
                ),
                reason="degraded provider health",
            )
        confidence = health_rate
        # Data health alone doesn't know market direction — vote NORMAL with
        # degraded confidence when provider is partially healthy.
        return SignalVote(
            family=family,
            state=MarketRiskState.NORMAL,
            health=SignalHealth(
                healthy=True,
                reason=f"provider health_rate={health_rate:.2f}",
                confidence=confidence,
            ),
            reason=f"provider health_rate={health_rate:.2f}",
        )
