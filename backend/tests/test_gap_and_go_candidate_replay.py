"""Regression: candidate replay must inject scanner-equivalent mover context.

Live, the engine populates StrategyContext.mover_change_pct (% change vs prior
session close) from scanner state. Offline candidate replay had no equivalent,
so gap_and_go -- gated entirely on that context -- emitted ZERO training
candidates forever and its mandatory meta-model could never train (fail-closed
PAUSED since 2026-08-24). The replay now derives % change vs the PRIOR
SESSION's closing price from the bars themselves.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from app.indicators.ta import enrich
from app.strategies.base import REGISTRY, Strategy, StrategyContext

SESSION_MINUTES = 40  # 09:30-10:09 ET, inside gap_and_go's morning drive window


def _multi_session_gapped_frame() -> tuple[pd.DataFrame, dict]:
    """Three consecutive sessions; S2 gaps +6%, S3 gaps +3% over their prior close."""
    days = [datetime(2026, 1, 6, 14, 30, tzinfo=UTC), datetime(2026, 1, 7, 14, 30, tzinfo=UTC),
            datetime(2026, 1, 8, 14, 30, tzinfo=UTC)]  # 09:30 ET, EST == UTC-5
    rows = []
    prev_close = 100.0
    ramps = [0.0, 6.0, 3.0]
    expected_first_bar_chg = {}
    marks = []
    for di, day_start in enumerate(days):
        if ramps[di]:
            prev_close = prev_close * (1.0 + ramps[di] / 100.0)
        expected_first_bar_chg[f"S{di + 1}"] = ramps[di]
        for b in range(SESSION_MINUTES):
            close = prev_close * (1.0 + 0.0004 * b)  # steady intraday rally
            opn = close - 0.01                       # every bar green
            hi = max(opn, close) + 0.05
            lo = min(opn, close) - 0.05
            vol = 1000.0 if b % 5 != 4 else 4000.0   # vol_z hits +2 on every 5th bar
            ts = day_start.timestamp() + 60 * b
            marks.append(ts)
            rows.append((opn, hi, lo, close, vol))
            prev_close = close
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"],
                      index=pd.to_datetime(marks, unit="s", utc=True))
    df.index.name = "ts"
    return df, expected_first_bar_chg


class _MoverProbe(Strategy):
    name = "_test_mover_probe"
    description = "records mover_change_pct seen during candidate replay"
    SEEN: list = []  # class-level: create_strategies builds its own instances

    def reset(self) -> None:
        pass

    def on_bar(self, ctx: StrategyContext, symbol: str, df: pd.DataFrame):
        _MoverProbe.SEEN.append((df.index[-1], ctx.mover_change_pct))
        return None


def test_candidate_replay_derives_prior_session_close_change():
    df, exp = _multi_session_gapped_frame()
    enriched = enrich(df)
    probe = _MoverProbe()
    REGISTRY[probe.name] = _MoverProbe
    try:
        _MoverProbe.SEEN = []
        from app.ai.prediction import _compute_candidates_for_symbol, clear_candidate_cache
        clear_candidate_cache()
        _compute_candidates_for_symbol("TEST", enriched.copy())
        seen = {ts: chg for ts, chg in _MoverProbe.SEEN}
    finally:
        REGISTRY.pop(probe.name, None)

    assert len(seen) == 3 * SESSION_MINUTES - 1  # replay loop starts at bar 1
    idx = enriched.index
    # First session has no prior day -> neutral 0 change
    first_day_chgs = [chg for ts, chg in seen.items() if ts.date() == idx[0].date()]
    assert all(abs(c) < 1e-12 for c in first_day_chgs)
    # First bar of each later session reflects its exact overnight gap
    second_day_first_ts = next(ts for ts in idx if ts.date() == idx[SESSION_MINUTES].date())
    third_day_first_ts = next(
        ts for ts in idx if ts.date() == idx[2 * SESSION_MINUTES].date()
    )
    assert abs(seen[second_day_first_ts] - exp["S2"]) < 1e-6
    assert abs(seen[third_day_first_ts] - exp["S3"]) < 1e-6


def test_gap_and_go_emits_candidates_on_gapped_rally_data():
    df, _ = _multi_session_gapped_frame()
    enriched = enrich(df)
    from app.ai.prediction import _compute_candidates_for_symbol

    res = _compute_candidates_for_symbol("TEST", enriched.copy())
    gg = res.get("gap_and_go")
    assert gg is not None
    # Was literally zero pre-fix (strategy hard-gated on missing mover context)
    assert len(gg["long"]) >= 3
