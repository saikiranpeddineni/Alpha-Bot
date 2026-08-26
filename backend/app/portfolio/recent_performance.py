"""Recent Performance Governor -- evidence-driven entry-risk scaling.

Reads the platform's OWN closed trades and converts measured recent expectancy
into a conservative sizing multiplier per symbol / strategy / session-hour.

Design contract (non-negotiable):
    * The governor can only TIGHTEN risk (multiplier <= 1.0). It never widens
      thresholds, never disables strategies, never blocks a signal outright --
      a losing context trades smaller until its measured expectancy recovers.
    * Expectancy is Bayesian-shrunk toward 0 so thin samples cannot slam the
      brakes on one bad trade (nor celebrate one lucky winner).
    * Everything adapts from live data. There are no hardcoded hours, symbols
      or strategies: if mornings start performing, the morning factor recovers
      on its own at the next refresh.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.applog import applog

ET = ZoneInfo("America/New_York")


class RecentPerformanceGovernor:
    """Computes conservative per-context sizing multipliers from realized R."""

    def __init__(self, db, cfg_provider) -> None:
        self._db = db
        # cfg_provider() returns the CURRENT RiskConfig (it is replaced on
        # runtime settings updates), so knob changes apply without restart.
        self._cfg_provider = cfg_provider
        self._factors: dict[str, float] = {}
        self._computed_at: datetime | None = None

    # ------------------------------------------------------------------
    def refresh_if_stale(self, now: datetime | None = None) -> None:
        """Recompute factors when the cache is older than the configured TTL."""
        now = now or datetime.now(UTC)
        ttl = timedelta(minutes=int(getattr(self._cfg_provider(), "governor_refresh_minutes", 15)))
        if self._computed_at is not None and (now - self._computed_at) < ttl:
            return
        try:
            rows = self._db.get_recent_trade_contexts(int(getattr(self._cfg_provider(), "governor_lookback_days", 14)))
        except Exception as exc:
            applog.warning("governor", f"Trade-context read failed; keeping last factors: {exc}")
            self._computed_at = now
            return
        self._factors = self._compute_factors(rows or [], now)
        self._computed_at = now
        losers = sorted((k, round(v, 2)) for k, v in self._factors.items() if v < 0.999)
        if losers:
            applog.info("governor", f"Risk scaling active on {len(losers)} context(s): {losers[:8]}")

    # ------------------------------------------------------------------
    def _compute_factors(self, rows: list[dict], now: datetime) -> dict[str, float]:
        k = float(getattr(self._cfg_provider(), "governor_shrink_k", 8.0))
        floor = float(getattr(self._cfg_provider(), "governor_floor", 0.25))
        min_trades = int(getattr(self._cfg_provider(), "governor_min_trades", 6))

        sector_of = self._sector_lookup()

        buckets: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            rmult = r.get("r_multiple")
            if rmult is None:
                continue
            try:
                rm = float(rmult)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(rm):
                continue
            sym = str(r.get("symbol", "")).upper()
            strat = str(r.get("strategy", ""))
            hour_key = ""
            ts = r.get("exit_at_ts")
            if isinstance(ts, datetime):
                hour_key = f"hour:{ts.astimezone(ET).hour}"
            if sym:
                buckets[f"sym:{sym}"].append(rm)
                # Sector bucket: lessons transfer across correlated names
                # (TradingAgents' "cross-ticker lessons", made deterministic).
                sec = sector_of.get(sym)
                if sec:
                    buckets[f"sector:{sec}"].append(rm)
            if strat:
                buckets[f"strat:{strat}"].append(rm)
            if hour_key:
                buckets[hour_key].append(rm)

        factors: dict[str, float] = {}
        for key, rs in buckets.items():
            n = len(rs)
            if n < min_trades:
                continue
            mean_r = sum(rs) / n
            # Shrinkage toward 0: (n * mean) / (n + K). A hot/cold streak must
            # persist before it moves sizing.
            shrunk = (n * mean_r) / (n + k)
            if shrunk >= 0:
                continue  # neutral-or-positive context: no intervention
            # Monotone map: -0.1R shrunk -> mild trim, -0.4R+ -> near floor.
            scale = math.exp(max(shrunk, -1.0) * float(getattr(self._cfg_provider(), "governor_loss_scale", 2.2)))
            factors[key] = max(floor, min(1.0, scale))
        return factors

    def _sector_lookup(self) -> dict[str, str]:
        """Symbol -> sector map from the scanner universe (lazy, fail-soft)."""
        cached = getattr(self, "_sector_map", None)
        if cached is not None:
            return cached
        try:
            from app.core.scanner import SYMBOL_SECTOR
            self._sector_map = {k.upper(): v for k, v in SYMBOL_SECTOR.items()}
        except Exception:
            self._sector_map = {}
        return self._sector_map

    # ------------------------------------------------------------------
    def factor_for(self, symbol: str, strategy: str, now: datetime) -> float:
        """Combined multiplier (<= 1.0) for this candidate's contexts."""
        self.refresh_if_stale(now)
        if not self._factors:
            return 1.0
        f = 1.0
        sym_u = symbol.upper()
        sf = self._factors.get(f"sym:{sym_u}")
        if sf is not None:
            f *= sf
        stf = self._factors.get(f"strat:{strategy}")
        if stf is not None:
            f *= stf
        hf = self._factors.get(f"hour:{now.astimezone(ET).hour}")
        if hf is not None:
            f *= hf
        sec = self._sector_lookup().get(sym_u)
        if sec:
            secf = self._factors.get(f"sector:{sec}")
            if secf is not None:
                f *= secf
        floor = float(getattr(self._cfg_provider(), "governor_floor", 0.25))
        return max(floor, min(1.0, f))
