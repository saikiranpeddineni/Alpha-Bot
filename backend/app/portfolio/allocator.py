"""Small production portfolio gate for AlphaDesk 007.

Portfolio has one responsibility: decide whether an already-approved signal
can coexist with current exposure. It does not score signals, size positions,
or run Kelly. RiskManager is the sole production sizing authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
    sym = symbol.upper()
    return _SECTOR_MAP.get(sym, f"UNKNOWN:{sym}")


class PortfolioAIGate:
    """Hard portfolio eligibility checks only."""

    def __init__(self, max_correlation: float = 0.75, max_positions_per_sector: int = 5) -> None:
        self.max_correlation = max_correlation
        self.max_positions_per_sector = max_positions_per_sector
        self._corr: pd.DataFrame | None = None

    def update_correlation_matrix(self, frames: dict[str, pd.DataFrame]) -> None:
        if not frames:
            self._corr = None
            return
        returns: dict[str, pd.Series] = {}
        for sym, df in frames.items():
            if df is None or df.empty or "close" not in df.columns:
                continue
            close = df["close"].dropna()
            if len(close) >= 20:
                returns[sym.upper()] = np.log(close / close.shift(1)).dropna()
        self._corr = pd.DataFrame(returns).corr(min_periods=20) if len(returns) >= 2 else None

    def _max_corr(self, symbol: str, positions: list[Position]) -> float:
        if self._corr is None or symbol.upper() not in self._corr.columns:
            return 0.0
        return max(
            (
                abs(float(self._corr.loc[p.symbol.upper(), symbol.upper()]))
                for p in positions
                if p.symbol.upper() in self._corr.index
                and not np.isnan(self._corr.loc[p.symbol.upper(), symbol.upper()])
            ),
            default=0.0,
        )

    def gate(
        self,
        symbol: str,
        open_positions: list[Position],
        sector_override: str | None = None,
        market_state: Any = None,
    ) -> tuple[bool, str]:
        sym = symbol.upper()
        max_corr = self._max_corr(sym, open_positions)
        if max_corr > self.max_correlation:
            return False, f"{sym} correlation {max_corr:.2f} > {self.max_correlation:.2f}"

        state = str(getattr(market_state, "value", market_state or "NORMAL")).upper()
        sector_limit = {"NORMAL": 5, "CAUTION": 3, "STRESS": 1, "PANIC": 1}.get(state, self.max_positions_per_sector)
        sector = sector_override or _sector(sym)
        count = sum(1 for p in open_positions if _sector(p.symbol) == sector)
        if count >= sector_limit:
            return False, f"{sym} sector limit {sector_limit} reached for {sector}"
        return True, "ok"


@dataclass
class AllocationResult:
    """Compatibility record. Production no longer uses Kelly sizing."""
    symbol: str
    score: float
    kelly_fraction: float = 0.0
    allocated_dollars: float = 0.0
    allocated_pct: float = 0.0


class CapitalAllocationOptimizer:
    """Compatibility shell for older callers; never sizes production trades."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    @staticmethod
    def fractional_kelly(*args, **kwargs) -> float:
        # Research-only compatibility. Production sizing is RiskManager.
        return 0.0

    def allocate(self, signals: list[Signal], available_capital: float, equity: float, **kwargs) -> list[AllocationResult]:
        return [AllocationResult(symbol=s.symbol, score=float(s.score or 0.0)) for s in signals]


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
    market_state: Any = None,
) -> PortfolioDecision:
    """Rank by signal strength, then apply portfolio eligibility once.

    No capital is allocated here. RiskManager computes quantity from the
    structural stop and account risk budget later in the execution path.
    """
    approved: list[Signal] = []
    gated: list[tuple[Signal, str]] = []
    effective_positions = list(positions)

    # Highest-confidence signal first so candidate order cannot starve a better
    # opportunity. Same-cycle reservations are represented only for eligibility.
    ordered = sorted(signals, key=lambda s: float(s.ml_probability or s.score or 0.0), reverse=True)
    for sig in ordered:
        if portfolio_gate is None:
            approved.append(sig)
            continue
        ok, reason = portfolio_gate.gate(sig.symbol, effective_positions, market_state=market_state)
        if not ok:
            gated.append((sig, reason))
            continue
        approved.append(sig)
        reference = float(sig.price or 1.0)
        effective_positions.append(
            Position(
                symbol=sig.symbol.upper(),
                qty=-1.0 if sig.action == SignalAction.ENTER_SHORT else 1.0,
                avg_entry_price=reference,
                market_price=reference,
                strategy=sig.strategy,
            )
        )

    applog.debug("portfolio", f"007 portfolio eligibility: {len(approved)} approved, {len(gated)} gated")
    return PortfolioDecision(approved_entries=[(sig, None) for sig in approved], gated_entries=gated)
