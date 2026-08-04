"""Strategy Performance Tracker — live per-strategy P&L analytics.

Tracks trades as they close and computes rolling statistics per strategy,
per symbol, and per market regime. This becomes the foundation for the
research dashboard and adaptive capital allocation decisions.

Metrics tracked per strategy:
  - Trade count, win count, loss count
  - Total PnL, gross profit, gross loss
  - Profit Factor = gross_profit / abs(gross_loss)
  - Win Rate = wins / total
  - Average R (PnL / initial_risk per trade)
  - Average hold time (minutes)
  - Expectancy = win_rate * avg_win - loss_rate * abs(avg_loss)
  - Rolling 20-trade Profit Factor (recency-weighted signal)
  - Max consecutive losses (worst streak)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from app.core.domain import TradeRecord
from app.core.applog import applog


@dataclass
class StrategyStats:
    """Accumulated statistics for one strategy."""

    strategy: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0        # stored as negative
    total_r: float = 0.0           # sum of R multiples
    total_hold_minutes: float = 0.0
    max_consec_losses: int = 0
    _current_streak: int = 0
    _recent: deque = field(default_factory=lambda: deque(maxlen=20))  # last 20 PnLs

    # ---- computed properties ----

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades > 0 else 0.0

    @property
    def profit_factor(self) -> float:
        return self.gross_profit / abs(self.gross_loss) if self.gross_loss < 0 else float("inf")

    @property
    def avg_r(self) -> float:
        return self.total_r / self.trades if self.trades > 0 else 0.0

    @property
    def avg_win(self) -> float:
        return self.gross_profit / self.wins if self.wins > 0 else 0.0

    @property
    def avg_loss(self) -> float:
        return self.gross_loss / self.losses if self.losses > 0 else 0.0

    @property
    def expectancy(self) -> float:
        """Expected PnL per trade = win_rate * avg_win + loss_rate * avg_loss."""
        if self.trades == 0:
            return 0.0
        loss_rate = self.losses / self.trades
        return self.win_rate * self.avg_win + loss_rate * self.avg_loss

    @property
    def avg_hold_minutes(self) -> float:
        return self.total_hold_minutes / self.trades if self.trades > 0 else 0.0

    @property
    def rolling_profit_factor(self) -> float:
        """Profit factor over the last 20 trades (recency signal)."""
        recent = list(self._recent)
        if not recent:
            return 0.0
        gp = sum(p for p in recent if p > 0)
        gl = sum(p for p in recent if p < 0)
        return gp / abs(gl) if gl < 0 else float("inf")

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate * 100, 1),
            "total_pnl": round(self.total_pnl, 2),
            "profit_factor": round(self.profit_factor, 3) if self.profit_factor != float("inf") else None,
            "rolling_pf_20": round(self.rolling_profit_factor, 3) if self.rolling_profit_factor != float("inf") else None,
            "avg_r": round(self.avg_r, 3),
            "expectancy": round(self.expectancy, 2),
            "avg_hold_min": round(self.avg_hold_minutes, 1),
            "max_consec_losses": self.max_consec_losses,
        }


class StrategyPerformanceTracker:
    """Real-time per-strategy performance accumulator.

    Call ``update(trade)`` on every closed trade. Access ``leaderboard()``
    for a sorted summary of all strategy stats.

    Usage (from engine._drain_fills):
        self.perf_tracker.update(trade)
    """

    def __init__(self) -> None:
        self._stats: dict[str, StrategyStats] = {}
        self._symbol_pnl: dict[str, float] = {}
        self._regime_pnl: dict[str, float] = {}
        self._hour_pnl: dict[int, list[float]] = {}  # ET hour → list of PnLs

    def _get(self, strategy: str) -> StrategyStats:
        if strategy not in self._stats:
            self._stats[strategy] = StrategyStats(strategy=strategy)
        return self._stats[strategy]

    def update(self, trade: TradeRecord, now: datetime | None = None) -> None:
        """Record a closed trade into the appropriate strategy bucket."""
        strategy = getattr(trade, "strategy", None) or "unknown"
        stats = self._get(strategy)

        pnl = float(trade.pnl)
        stats.trades += 1
        stats.total_pnl += pnl
        stats._recent.append(pnl)

        if pnl > 0:
            stats.wins += 1
            stats.gross_profit += pnl
            stats._current_streak = 0
        else:
            stats.losses += 1
            stats.gross_loss += pnl
            stats._current_streak += 1
            stats.max_consec_losses = max(stats.max_consec_losses, stats._current_streak)

        # R multiple: PnL / initial_risk_per_share * qty
        initial_risk = getattr(trade, "initial_risk_per_share", None)
        qty = getattr(trade, "qty", None)
        if initial_risk and qty and initial_risk > 0:
            r = pnl / (initial_risk * qty)
            stats.total_r += r

        # Hold time
        opened_at = getattr(trade, "opened_at", None)
        closed_at = getattr(trade, "closed_at", None)
        if opened_at and closed_at:
            try:
                diff = (closed_at - opened_at).total_seconds() / 60.0
                stats.total_hold_minutes += diff
            except Exception:
                pass

        # Symbol bucket
        sym = getattr(trade, "symbol", "unknown")
        self._symbol_pnl[sym] = self._symbol_pnl.get(sym, 0.0) + pnl

        # Hour bucket (ET hour of trade close)
        if now is not None:
            try:
                from zoneinfo import ZoneInfo
                et_hour = now.astimezone(ZoneInfo("America/New_York")).hour
                self._hour_pnl.setdefault(et_hour, []).append(pnl)
            except Exception:
                pass

        applog.debug(
            "perf",
            f"{strategy} trade closed: pnl=${pnl:+.2f} | "
            f"WR={stats.win_rate*100:.0f}% PF={stats.profit_factor:.2f} trades={stats.trades}",
        )

    def leaderboard(self) -> list[dict]:
        """Return strategies sorted by profit factor descending."""
        rows = [s.to_dict() for s in self._stats.values()]
        rows.sort(key=lambda r: r["total_pnl"], reverse=True)
        return rows

    def symbol_leaderboard(self) -> list[dict]:
        """Return symbols sorted by total PnL descending."""
        return sorted(
            [{"symbol": sym, "total_pnl": round(pnl, 2)} for sym, pnl in self._symbol_pnl.items()],
            key=lambda r: r["total_pnl"],
            reverse=True,
        )

    def hour_performance(self) -> list[dict]:
        """Return average PnL by hour of day (ET)."""
        rows = []
        for hour in sorted(self._hour_pnl):
            pnls = self._hour_pnl[hour]
            rows.append({
                "hour_et": hour,
                "trades": len(pnls),
                "avg_pnl": round(sum(pnls) / len(pnls), 2),
                "total_pnl": round(sum(pnls), 2),
            })
        return rows

    def summary(self) -> dict:
        """Full performance summary for API/dashboard consumption."""
        return {
            "strategy_leaderboard": self.leaderboard(),
            "symbol_leaderboard": self.symbol_leaderboard()[:10],
            "hour_performance": self.hour_performance(),
        }
