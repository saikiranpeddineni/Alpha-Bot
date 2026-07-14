"""Performance metrics.

Computed from an equity curve and a trade list; used for both backtest
results and live-session reporting. Ratios annualize from daily resampled
returns; with under two sessions of data the ratios are omitted rather than
reported on noise.
"""

from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pandas as pd

from app.core.domain import TradeRecord

TRADING_DAYS = 252


def _equity_series(curve: list[tuple[datetime, float]]) -> pd.Series:
    if not curve:
        return pd.Series(dtype=float)
    ts, eq = zip(*curve, strict=True)
    return pd.Series(eq, index=pd.DatetimeIndex(ts), dtype=float).sort_index()


def max_drawdown_pct(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    running_max = equity.cummax()
    dd = (equity - running_max) / running_max
    return float(-dd.min() * 100.0)


def compute_metrics(
    curve: list[tuple[datetime, float]],
    trades: list[TradeRecord],
    starting_equity: float,
) -> dict:
    equity = _equity_series(curve)
    final_equity = float(equity.iloc[-1]) if not equity.empty else starting_equity
    total_return_pct = (final_equity / starting_equity - 1.0) * 100.0 if starting_equity else 0.0

    sharpe = sortino = cagr_pct = None
    if not equity.empty:
        daily = equity.resample("1D").last().dropna()
        rets = daily.pct_change().dropna()
        if len(rets) >= 2 and float(rets.std(ddof=0)) > 0:
            mean, std = float(rets.mean()), float(rets.std(ddof=0))
            sharpe = round(mean / std * math.sqrt(TRADING_DAYS), 2)
            downside = rets[rets < 0]
            dstd = float(downside.std(ddof=0)) if len(downside) > 1 else 0.0
            sortino = round(mean / dstd * math.sqrt(TRADING_DAYS), 2) if dstd > 0 else None
        span_days = max((equity.index[-1] - equity.index[0]).total_seconds() / 86400.0, 0.0)
        if span_days >= 1.0 and starting_equity > 0 and final_equity > 0:
            cagr_pct = round(((final_equity / starting_equity) ** (365.0 / span_days) - 1.0) * 100.0, 2)

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    r_values = [t.r_multiple for t in trades if t.r_multiple is not None]

    return {
        "starting_equity": round(starting_equity, 2),
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(total_return_pct, 2),
        "cagr_pct": cagr_pct,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown_pct": round(max_drawdown_pct(equity), 2),
        "total_trades": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100.0, 1) if trades else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "expectancy": round((gross_win - gross_loss) / len(trades), 2) if trades else 0.0,
        "avg_r": round(float(np.mean(r_values)), 2) if r_values else None,
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "best_trade": round(max((t.pnl for t in trades), default=0.0), 2),
        "worst_trade": round(min((t.pnl for t in trades), default=0.0), 2),
    }
