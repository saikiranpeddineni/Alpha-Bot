"""Portfolio gating and capital-allocation orchestration.

Rank candidate trades, assign Kelly fractions based on ML probability, and ensure 
new entries don't breach sector concentration or correlation limits.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.core.domain import Position, Signal, SignalAction
from app.core.applog import applog

_SECTOR_MAP: dict[str, str] = {
    "AAPL": "TECH", "MSFT": "TECH", "NVDA": "TECH", "AMD": "TECH",
    "INTC": "TECH", "META": "TECH", "GOOGL": "TECH", "GOOG": "TECH",
    "AMZN": "CONSUMER", "TSLA": "CONSUMER", "PLTR": "TECH", "UBER": "INDUSTRIAL",
    "NET": "TECH", "SNOW": "TECH", "CRM": "TECH", "NOW": "TECH",
    "BAC": "FINANCE", "JPM": "FINANCE", "GS": "FINANCE", "WFC": "FINANCE",
    "C": "FINANCE", "MS": "FINANCE", "XOM": "ENERGY", "CVX": "ENERGY",
    "COP": "ENERGY", "EOG": "ENERGY", "KO": "CONSUMER", "PEP": "CONSUMER",
    "WMT": "CONSUMER", "COST": "CONSUMER", "MCD": "CONSUMER", "NKE": "CONSUMER",
    "JNJ": "HEALTHCARE", "PFE": "HEALTHCARE", "MRK": "HEALTHCARE",
    "ABBV": "HEALTHCARE", "LLY": "HEALTHCARE", "UNH": "HEALTHCARE",
    "SPY": "ETF", "QQQ": "ETF", "IWM": "ETF", "DIA": "ETF",
}


def _sector(symbol: str) -> str:
    # Unknown symbols must not all collapse into one synthetic sector.  Until a
    # metadata-backed taxonomy is available, isolate each unknown ticker so the
    # sector gate does not create false concentration rejections.
    sym = symbol.upper()
    return _SECTOR_MAP.get(sym, f"UNKNOWN:{sym}")


@dataclass
class AllocationResult:
    symbol: str
    score: float
    kelly_fraction: float
    allocated_dollars: float
    allocated_pct: float


class PortfolioAIGate:
    """Correlation and sector-concentration guard."""

    def __init__(self, max_correlation: float = 0.75, max_positions_per_sector: int = 2) -> None:
        self.max_correlation = max_correlation
        self.max_positions_per_sector = max_positions_per_sector
        self._corr: pd.DataFrame | None = None

    def update_correlation_matrix(self, frames: dict[str, pd.DataFrame]) -> None:
        if not frames:
            return
        returns: dict[str, pd.Series] = {}
        for sym, df in frames.items():
            if df is None or df.empty or "close" not in df.columns:
                continue
            close = df["close"].dropna()
            if len(close) < 20:
                continue
            returns[sym] = np.log(close / close.shift(1)).dropna()
        if len(returns) < 2:
            self._corr = None
            return
        self._corr = pd.DataFrame(returns).corr(min_periods=20)
        applog.info("portfolio.gate", f"Correlation matrix updated: {len(returns)} symbols")

    def _max_corr_with_positions(self, symbol: str, open_positions: list[Position]) -> float:
        if self._corr is None:
            return 0.0
        sym = symbol.upper()
        if sym not in self._corr.columns:
            return 0.0
        max_c = 0.0
        for pos in open_positions:
            pos_sym = pos.symbol.upper()
            if pos_sym in self._corr.index:
                c = self._corr.loc[pos_sym, sym]
                if not np.isnan(c):
                    max_c = max(max_c, abs(float(c)))
        return max_c

    def gate(self, symbol: str, open_positions: list[Position], sector_override: str | None = None) -> tuple[bool, str]:
        sym = symbol.upper()
        max_c = self._max_corr_with_positions(sym, open_positions)
        if max_c > self.max_correlation:
            correlated_with = [
                p.symbol for p in open_positions
                if self._corr is not None
                and p.symbol.upper() in self._corr.index
                and sym in self._corr.columns
                and not np.isnan(self._corr.loc[p.symbol.upper(), sym])
                and abs(float(self._corr.loc[p.symbol.upper(), sym])) > self.max_correlation
            ]
            return False, f"{sym} correlation {max_c:.2f} > {self.max_correlation:.2f} with {correlated_with}"

        sector = sector_override or _sector(sym)
        sector_count = sum(1 for p in open_positions if _sector(p.symbol) == sector)
        if sector_count >= self.max_positions_per_sector:
            return False, (
                f"{sym} would exceed sector limit {self.max_positions_per_sector} "
                f"in {sector} (currently {sector_count})"
            )
        return True, "ok"


class CapitalAllocationOptimizer:
    """Rank candidates by Trade Score and compute Kelly sizing fractions.

    Calculates optimal Kelly risk constraints based on Machine Learning
    probabilities (`p_win`) and structural payoff geometry (`reward_risk`).
    """

    def __init__(self, kelly_fraction_cap: float = 0.15) -> None:
        self.kelly_fraction_cap = min(max(kelly_fraction_cap, 0.01), 0.50)

    @staticmethod
    def fractional_kelly(p_win: float, reward_risk: float, fraction: float = 0.50, cap: float = 0.15) -> float:
        """Computes a conservative (fractional) Kelly criterion for dynamic position sizing."""
        p_win = float(np.clip(p_win, 0.0, 1.0))
        reward_risk = max(float(reward_risk), 0.1)
        raw_kelly = p_win - (1.0 - p_win) / reward_risk
        return float(np.clip(fraction * raw_kelly, 0.0, cap))

    def _reward_risk(self, signal: Signal) -> float:
        price, sl, tp = signal.price, signal.stop_loss, signal.take_profit
        if price and sl and tp and price > 0:
            risk = abs(price - sl)
            reward = abs(tp - price)
            if risk > 0:
                return reward / risk
        return 2.0

    def allocate(
        self,
        signals: list[Signal],
        available_capital: float,
        equity: float,
        kelly_fraction_cap: float | None = None,
    ) -> list[AllocationResult]:
        if not signals or available_capital <= 0 or equity <= 0:
            return []

        # Trade Score is the validated relative ranking metric.  Give every
        # approved candidate a positive weight so allocation cannot become a
        # second, undocumented signal gate.
        weighted = [(sig, max(float(sig.score or 0.0), 1e-6)) for sig in signals]
        total_weight = sum(weight for _, weight in weighted)
        remaining = available_capital
        results: list[AllocationResult] = []

        for index, (sig, weight) in enumerate(sorted(weighted, key=lambda item: item[1], reverse=True)):
            candidates_left = len(weighted) - index
            share = weight / total_weight if total_weight > 0 else 1.0 / len(weighted)
            requested = available_capital * share
            # Keep the historical cap as a budget ceiling only.  RiskManager
            # will normally request materially less based on stop distance.
            hard_max = equity * self.kelly_fraction_cap
            dollars = min(requested, hard_max, remaining)
            if candidates_left == 1:
                dollars = min(dollars, remaining)
            dollars = round(max(dollars, 0.0), 2)
            remaining -= dollars

            p_win = getattr(sig, "ml_probability", None)
            if p_win is None:
                p_win = 0.50
            rr = self._reward_risk(sig)
            kelly_val = self.fractional_kelly(p_win, rr, cap=self.kelly_fraction_cap)

            results.append(AllocationResult(
                symbol=sig.symbol,
                score=float(sig.score or 0.0),
                kelly_fraction=kelly_val,
                allocated_dollars=dollars,
                allocated_pct=round(dollars / equity * 100.0, 3),
            ))

        applog.info(
            "portfolio.alloc",
            f"Allocation complete: budget_ceiling=${sum(r.allocated_dollars for r in results):,.0f} "
            f"across {len(results)} signals; sizing_authority=RiskManager; kelly=ENABLED",
        )
        return results


@dataclass
class PortfolioDecision:
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
    approved_candidates: list[Signal] = []
    gated_entries: list[tuple[Signal, str]] = []

    # The broker snapshot is authoritative at cycle start, but every candidate
    # approved in this batch is potential same-cycle exposure.  Gate later
    # candidates against those prior approvals too, otherwise two correlated or
    # same-sector signals can both pass against the same stale snapshot and only
    # become visible after broker submission.  Quantity is intentionally a
    # placeholder: this ledger exists only for symbol/sector/correlation checks;
    # RiskManager remains the sole sizing authority.
    effective_positions = list(positions)
    for sig in signals:
        approved, reason = portfolio_gate.gate(sig.symbol, effective_positions) if portfolio_gate is not None else (True, "ok")
        if approved:
            approved_candidates.append(sig)
            if portfolio_gate is not None:
                reference_price = float(sig.price) if sig.price is not None and float(sig.price) > 0 else 1.0
                signed_qty = -1.0 if sig.action == SignalAction.ENTER_SHORT else 1.0
                effective_positions.append(Position(
                    symbol=sig.symbol.upper(),
                    qty=signed_qty,
                    avg_entry_price=reference_price,
                    market_price=reference_price,
                    strategy=sig.strategy,
                ))
        else:
            gated_entries.append((sig, reason))

    kelly_map = {}
    if portfolio_allocator is not None and approved_candidates:
        # Allocation ranking and Kelly sizing fractions. RiskManager uses these as
        # the kelly_override when sizing each entry — the hard stop-distance-based
        # position size is still the binding constraint, but Kelly provides the
        # equity fraction budget ceiling.
        # F-03 FIX: was always mapping to None, making the entire Kelly computation dead code.
        alloc_results = portfolio_allocator.allocate(approved_candidates, available_capital, equity)
        for res in alloc_results:
            kelly_map[res.symbol] = res.kelly_fraction  # now flows to RiskManager.evaluate_entry()


    approved_entries = [(sig, kelly_map.get(sig.symbol, None)) for sig in approved_candidates]
    return PortfolioDecision(approved_entries=approved_entries, gated_entries=gated_entries)
