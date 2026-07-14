"""Portfolio AI Gate and Capital Allocation Optimizer — Phase 3.

This module sits between the Trade Score gate and the risk manager:

  Trade Score Gate
        |
        v
  PortfolioAIGate      ← rejects correlated / sector-concentrated entries
        |
        v
  CapitalAllocationOptimizer  ← distributes capital by relative Trade Score
        |
        v
  Fractional Kelly sizing  ← DISABLED: ml_probability (P(15-min price move > 0.10%) ≠ P(trade wins via SL/TP path);
  #                           passing ml_probability as Kelly p_win with SL/TP-derived b is mathematically
  #                           invalid because p and b describe different events. Kelly override = None;
  #                           RiskManager uses validated fixed risk_per_trade_pct instead.
        |
        v
  RiskManager              ← final check and order construction

Design principles:
- PortfolioAIGate never approves a trade that would push portfolio correlation
  above the configured threshold or breach the sector concentration limit.
- CapitalAllocationOptimizer distributes available capital proportionally to
  relative Trade Scores, then sizes each position via Fractional Kelly.
- Kelly fraction is ALWAYS capped (default 0.15). Full Kelly is never used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from app.core.domain import Signal
from app.core.logging import applog

if TYPE_CHECKING:
    from app.core.domain import Position

# ---------------------------------------------------------------------------
# Sector map  (extend as needed)
# ---------------------------------------------------------------------------

_SECTOR_MAP: dict[str, str] = {
    # Technology
    "AAPL": "TECH", "MSFT": "TECH", "NVDA": "TECH", "AMD": "TECH",
    "INTC": "TECH", "META": "TECH", "GOOGL": "TECH", "GOOG": "TECH",
    "AMZN": "TECH", "TSLA": "TECH", "PLTR": "TECH", "UBER": "TECH",
    "NET": "TECH", "SNOW": "TECH", "CRM": "TECH", "NOW": "TECH",
    # Finance
    "BAC": "FINANCE", "JPM": "FINANCE", "GS": "FINANCE",
    "WFC": "FINANCE", "C": "FINANCE", "MS": "FINANCE",
    # Energy
    "XOM": "ENERGY", "CVX": "ENERGY", "COP": "ENERGY", "EOG": "ENERGY",
    # Consumer
    "KO": "CONSUMER", "PEP": "CONSUMER", "WMT": "CONSUMER",
    "COST": "CONSUMER", "MCD": "CONSUMER", "NKE": "CONSUMER",
    # Healthcare
    "JNJ": "HEALTHCARE", "PFE": "HEALTHCARE", "MRK": "HEALTHCARE",
    "ABBV": "HEALTHCARE", "LLY": "HEALTHCARE", "UNH": "HEALTHCARE",
    # Broad market ETFs
    "SPY": "ETF", "QQQ": "ETF", "IWM": "ETF", "DIA": "ETF",
}

_SECTOR_OTHER = "OTHER"


def _sector(symbol: str) -> str:
    return _SECTOR_MAP.get(symbol.upper(), _SECTOR_OTHER)


# ---------------------------------------------------------------------------
# 1. AllocationResult
# ---------------------------------------------------------------------------


@dataclass
class AllocationResult:
    """Capital allocation output for one candidate signal."""

    symbol: str
    score: float          # 0-1 Trade Score from signal
    kelly_fraction: float # fractional Kelly (after cap)
    allocated_dollars: float
    allocated_pct: float  # as % of equity


# ---------------------------------------------------------------------------
# 2. PortfolioAIGate
# ---------------------------------------------------------------------------


class PortfolioAIGate:
    """Correlation and sector concentration guard.

    Rejects new entries that would either:
    - Increase portfolio correlation above ``max_correlation`` with any
      existing position; or
    - Push the sector count above ``max_positions_per_sector``.

    The correlation matrix is computed from close-price returns of the
    bar frames passed to ``update_correlation_matrix()``. The matrix is
    updated whenever new historical data is available (typically daily).
    """

    def __init__(
        self,
        max_correlation: float = 0.75,
        max_positions_per_sector: int = 2,
    ) -> None:
        self.max_correlation = max_correlation
        self.max_positions_per_sector = max_positions_per_sector
        self._corr: pd.DataFrame | None = None  # symbol x symbol

    # ------------------------------------------------------------------
    def update_correlation_matrix(
        self, frames: dict[str, pd.DataFrame]
    ) -> None:
        """Recompute correlation matrix from close-price log-returns.

        Uses the last 20 trading days of data for each symbol.
        Only pairs with at least 20 overlapping bars are included.
        """
        if not frames:
            return

        returns: dict[str, pd.Series] = {}
        for sym, df in frames.items():
            if df is None or df.empty or "close" not in df.columns:
                continue
            close = df["close"].dropna()
            if len(close) < 20:
                continue
            ret = np.log(close / close.shift(1)).dropna()
            returns[sym] = ret

        if len(returns) < 2:
            self._corr = None
            return

        ret_df = pd.DataFrame(returns)
        # Require at least 20 overlapping observations for each pair
        self._corr = ret_df.corr(min_periods=20)
        applog.info(
            "portfolio.gate",
            f"Correlation matrix updated: {len(returns)} symbols",
        )

    def _max_corr_with_positions(
        self, symbol: str, open_positions: list["Position"]
    ) -> float:
        """Return the highest correlation between symbol and any open position."""
        if self._corr is None:
            return 0.0
        sym = symbol.upper()
        if sym not in self._corr.columns:
            return 0.0
        max_c = 0.0
        for pos in open_positions:
            pos_sym = pos.symbol.upper()
            if pos_sym in self._corr.index and sym in self._corr.columns:
                c = self._corr.loc[pos_sym, sym]
                if not np.isnan(c):
                    max_c = max(max_c, abs(float(c)))
        return max_c

    def gate(
        self,
        symbol: str,
        open_positions: list["Position"],
        sector_override: str | None = None,
    ) -> tuple[bool, str]:
        """Evaluate whether adding ``symbol`` is acceptable.

        Parameters
        ----------
        symbol:
            Candidate ticker.
        open_positions:
            Currently open positions (from broker or portfolio).
        sector_override:
            Optional manual sector string for unknown tickers.

        Returns
        -------
        (approved, reason)
        """
        sym = symbol.upper()

        # -- Correlation check --
        max_c = self._max_corr_with_positions(sym, open_positions)
        if max_c > self.max_correlation:
            correlated_with = [
                p.symbol for p in open_positions
                if self._corr is not None
                and p.symbol.upper() in self._corr.index
                and sym in self._corr.columns
                and abs(float(self._corr.loc[p.symbol.upper(), sym])) > self.max_correlation
            ]
            return (
                False,
                f"{sym} correlation {max_c:.2f} > {self.max_correlation:.2f} "
                f"with {correlated_with}",
            )

        # -- Sector concentration check --
        sector = sector_override or _sector(sym)
        sector_count = sum(
            1 for p in open_positions if _sector(p.symbol.upper()) == sector
        )
        if sector_count >= self.max_positions_per_sector:
            return (
                False,
                f"{sym} would exceed sector limit {self.max_positions_per_sector} "
                f"in {sector} (currently {sector_count})",
            )

        return True, "ok"


# ---------------------------------------------------------------------------
# 3. CapitalAllocationOptimizer
# ---------------------------------------------------------------------------


class CapitalAllocationOptimizer:
    """Distributes available capital across candidate signals.

    Ranking logic:
    1. Compute fractional Kelly for each signal using its Trade Score as p_win.
    2. Scale allocations proportionally to the Kelly fractions so the total
       never exceeds ``available_capital``.
    3. Apply a hard cap of ``kelly_fraction_cap × equity`` per position.

    This ensures:
    - Signals with higher Trade Scores receive proportionally more capital.
    - No single position receives more than the Kelly cap.
    - Total allocated capital never exceeds what is available.
    """

    def __init__(self, kelly_fraction_cap: float = 0.15) -> None:
        self.kelly_fraction_cap = min(max(kelly_fraction_cap, 0.01), 0.50)

    # ------------------------------------------------------------------
    @staticmethod
    def fractional_kelly(
        p_win: float,
        reward_risk: float,
        fraction: float = 0.50,
        cap: float = 0.15,
    ) -> float:
        """Compute Fractional Kelly position size as fraction of equity.

        Formula: f* = fraction × (p_win - (1 - p_win) / reward_risk)

        Parameters
        ----------
        p_win:
            Probability of a winning trade (0-1). Use Trade Score as a proxy.
        reward_risk:
            Expected reward divided by expected risk (R/R ratio). Must be > 0.
        fraction:
            Kelly fraction divisor. Default 0.5 (half-Kelly). Use 0.25 for
            quarter-Kelly in volatile regimes.
        cap:
            Hard maximum. The result is clamped to [0, cap].

        Returns
        -------
        Fraction of equity to allocate (0 to ``cap``).
        """
        p_win = float(np.clip(p_win, 0.0, 1.0))
        reward_risk = max(float(reward_risk), 0.1)  # avoid division by zero
        raw_kelly = p_win - (1.0 - p_win) / reward_risk
        fractional = fraction * raw_kelly
        return float(np.clip(fractional, 0.0, cap))

    # ------------------------------------------------------------------
    def _reward_risk(self, signal: Signal) -> float:
        """Estimate R/R from signal stop_loss and take_profit; default to 2.0."""
        price = signal.price
        sl = signal.stop_loss
        tp = signal.take_profit
        if price and sl and tp and price > 0:
            risk = abs(price - sl)
            reward = abs(tp - price)
            if risk > 0:
                return reward / risk
        return 2.0  # conservative default

    def allocate(
        self,
        signals: list[Signal],
        available_capital: float,
        equity: float,
        kelly_fraction_cap: float | None = None,
    ) -> list[AllocationResult]:
        """Allocate capital across candidate signals.

        Parameters
        ----------
        signals:
            Candidate entry signals, each with `.score` (0-1 Trade Score).
        available_capital:
            Maximum dollars available to deploy (e.g. free cash).
        equity:
            Total account equity (used for Kelly sizing).
        kelly_fraction_cap:
            Override the instance cap for this allocation run.

        Returns
        -------
        Sorted list of ``AllocationResult``, highest allocation first.
        Signals with zero Kelly fraction are excluded.
        """
        cap = kelly_fraction_cap or self.kelly_fraction_cap
        if not signals or available_capital <= 0 or equity <= 0:
            return []

        results: list[AllocationResult] = []
        for sig in signals:
            # Mathematical constraint: Kelly requires that p_win and b (reward_risk) describe the
            # SAME event. ml_probability = P(15-min price change > threshold), while b is derived
            # from the trade's SL/TP geometry, which is a different event (the full trade path
            # including time exit, trailing stop, etc.). These are incompatible. Ranking by
            # ml_probability for relative ordering is still valid; Kelly sizing from it is not.
            p_win = float(sig.ml_probability) if sig.ml_probability is not None else 0.5
            rr = self._reward_risk(sig)
            kf = self.fractional_kelly(p_win, rr, fraction=0.50, cap=cap)
            raw_kelly = p_win - (1.0 - p_win) / rr
            if kf > 0:
                results.append(AllocationResult(
                    symbol=sig.symbol,
                    score=p_win,
                    kelly_fraction=kf,
                    allocated_dollars=0.0,  # computed below
                    allocated_pct=0.0,
                ))
            else:
                applog.info(
                    "portfolio.alloc",
                    f"[kelly_reject] {sig.symbol} {sig.strategy} "
                    f"p_win={p_win:.4f} rr={rr:.4f} "
                    f"raw_kelly={raw_kelly:.4f} capped_kelly={kf:.4f} "
                    f"decision=REJECT_NON_POSITIVE_KELLY",
                )

        if not results:
            return []

        # Proportional distribution capped by available capital
        total_kelly = sum(r.kelly_fraction for r in results)
        max_deploy = min(available_capital, equity * cap * len(results))

        remaining = min(available_capital, max_deploy)
        for r in results:
            share = r.kelly_fraction / total_kelly if total_kelly > 0 else 1.0 / len(results)
            hard_max = equity * cap  # per-position hard cap
            r.allocated_dollars = round(min(share * remaining, hard_max), 2)
            r.allocated_pct = round(r.allocated_dollars / equity * 100.0, 3)

        # Ensure total doesn't exceed available
        total_alloc = sum(r.allocated_dollars for r in results)
        if total_alloc > available_capital:
            scale = available_capital / total_alloc
            for r in results:
                r.allocated_dollars = round(r.allocated_dollars * scale, 2)
                r.allocated_pct = round(r.allocated_dollars / equity * 100.0, 3)

        results.sort(key=lambda r: r.allocated_dollars, reverse=True)
        total_allocated = sum(r.allocated_dollars for r in results)
        applog.info(
            "portfolio.alloc",
            f"Allocation complete: total_budget_ceiling=${total_allocated:,.0f} across {len(results)} signals "
            f"(available_capital=${available_capital:,.0f} equity=${equity:,.0f} cap={cap:.2f})",
        )
        for r in results:
            # kelly_target = equity × kelly_fraction — this is what RiskManager uses
            kelly_target_dollars = round(equity * r.kelly_fraction, 2)
            kelly_target_pct = round(r.kelly_fraction * 100.0, 3)
            applog.info(
                "portfolio.alloc",
                f"  [{r.symbol}] "
                f"kelly_fraction={r.kelly_fraction:.4f} "
                f"kelly_target_dollars=${kelly_target_dollars:,.0f} "
                f"kelly_target_pct={kelly_target_pct:.3f}% "
                f"budget_ceiling=${r.allocated_dollars:,.0f} "
                f"kelly_override_passed={r.kelly_fraction:.4f} "
                f"| RiskManager will compute: equity x kelly_fraction = ${kelly_target_dollars:,.0f}",
            )
        return results



@dataclass
class PortfolioDecision:
    """Shared output representation for portfolio gating and capital allocation."""
    approved_entries: list[tuple[Signal, float | None]]
    gated_entries: list[tuple[Signal, str]]


def process_portfolio_allocations(
    signals: list[Signal],
    positions: list[Position],
    available_capital: float,
    equity: float,
    portfolio_gate: PortfolioAIGate | None = None,
    portfolio_allocator: CapitalAllocationOptimizer | None = None,
) -> PortfolioDecision:
    """Shared orchestration logic for Portfolio AI Gate and Capital Allocation Optimizer."""
    approved_candidates: list[Signal] = []
    gated_entries: list[tuple[Signal, str]] = []

    for sig in signals:
        if portfolio_gate is not None:
            approved, reason = portfolio_gate.gate(sig.symbol, positions)
        else:
            approved, reason = True, "ok"

        if not approved:
            gated_entries.append((sig, reason))
        else:
            approved_candidates.append(sig)

    approved_entries: list[tuple[Signal, float | None]] = []
    
    if portfolio_allocator is not None and approved_candidates:
        alloc_results = portfolio_allocator.allocate(approved_candidates, available_capital, equity)
        allocated_symbols = {r.symbol: r for r in alloc_results}
        for sig in approved_candidates:
            alloc_res = allocated_symbols.get(sig.symbol)
            if alloc_res:
                # Kelly mathematical constraint: ml_probability and the SL/TP-derived reward_risk
                # describe different events, making Kelly(ml_probability, reward_risk) invalid.
                # Pass None so RiskManager uses its validated fixed risk_per_trade_pct.
                # Portfolio gate ordering (by kelly_fraction rank) remains intact — only sizing is suppressed.
                approved_entries.append((sig, None))
            else:
                # Zero or negative Kelly: signal has no positive expected edge at current R:R and ML confidence
                gated_entries.append((sig, f"Portfolio Allocator: Rejected — Kelly fraction <= 0 (negative expected edge at p_win={sig.ml_probability:.3f} vs R:R={self._reward_risk(sig) if portfolio_allocator else 2.0:.2f})" if portfolio_allocator else "Portfolio Allocator: Rejected — Kelly <= 0"))
    else:
        for sig in approved_candidates:
            approved_entries.append((sig, None))

    return PortfolioDecision(approved_entries=approved_entries, gated_entries=gated_entries)

