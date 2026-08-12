"""Decision-quality benchmark for Alpha-Bot.

This is analysis tooling only. It does not participate in the production path.
It runs the existing causal backtest once, then evaluates the same candidate
stream using deliberately simpler selection rules. The purpose is to answer
whether downstream gates add value or destroy opportunity quality.

The benchmark is intentionally explicit about scope:
- Production metrics come from the existing BacktestResult.
- Signal-only / trend-filter / random metrics are opportunity-quality metrics,
  not claims of portfolio-equivalent execution, because they do not model
  portfolio capacity or order interaction.
- All forward outcomes come from ShadowLedger's causal outcomes attached to
  each candidate; no future data is used to make a selection decision.

Usage:
    python -m backend.scripts.decision_quality_benchmark --start 2026-08-11 --end 2026-08-12

For a full production-equivalent comparison, use the existing backtest engine
with separate policy configurations. This script is the first diagnostic,
not a profitability certification.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.backtest.engine import BacktestParams, run_backtest
from app.config import DATA_DIR, Settings
from app.core.shadow_ledger import ShadowLedger
from app.data.base import CachePolicy
from app.data.manager import DataManager
from app.ai.model import SignalModel


class BlockingProvider:
    name = "blocking"

    async def get_bars(self, *args, **kwargs):
        raise RuntimeError("NETWORK BLOCKED: benchmark must use cached data")

    async def get_quotes(self, *args, **kwargs):
        return {}


def _parse_date(value: str, end: bool = False) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if len(value) <= 10:
        dt = dt.replace(hour=16 if end else 9, minute=0 if end else 30, second=0, microsecond=0)
    return dt.astimezone(timezone.utc)


def _outcome(signal: dict, horizon: str = "60m") -> dict | None:
    outcomes = signal.get("outcomes") or {}
    value = outcomes.get(horizon)
    if not isinstance(value, dict):
        return None
    if value.get("fwd_return") is None:
        return None
    return value


def _score(signals: list[dict], horizon: str = "60m") -> dict:
    usable = []
    for s in signals:
        o = _outcome(s, horizon)
        if o is not None:
            usable.append((s, o))
    pnl = sum(float(o.get("fwd_return", 0.0)) for _, o in usable)
    wins = sum(1 for _, o in usable if float(o.get("fwd_return", 0.0)) > 0)
    r = [o.get("realized_r") for _, o in usable if o.get("realized_r") is not None]
    positive_r = [x for x in r if x > 0]
    negative_r = [x for x in r if x <= 0]
    gross_profit = sum(positive_r)
    gross_loss = abs(sum(negative_r))
    return {
        "candidates": len(signals),
        "usable_60m": len(usable),
        "win_rate_pct": round(wins / len(usable) * 100, 2) if usable else 0.0,
        "sum_forward_return": round(pnl, 6),
        "avg_forward_return": round(pnl / len(usable), 6) if usable else 0.0,
        "sum_r": round(sum(r), 4),
        "avg_r": round(sum(r) / len(r), 4) if r else None,
        "profit_factor_r": round(gross_profit / gross_loss, 4) if gross_loss else None,
        "profitable_count": wins,
        "losing_count": len(usable) - wins,
    }


def _production_summary(result) -> dict:
    trades = result.trades
    return {
        "trades": len(trades),
        "net_pnl": round(sum(float(t.pnl) for t in trades), 2),
        "net_r": round(sum(float(t.r_multiple) for t in trades if t.r_multiple is not None), 4),
        "win_rate_pct": result.metrics.get("win_rate", 0.0),
        "expectancy": result.metrics.get("expectancy", 0.0),
        "profit_factor": result.metrics.get("profit_factor"),
        "max_drawdown_pct": result.metrics.get("max_drawdown_pct", 0.0),
        "avg_win": result.metrics.get("avg_win", 0.0),
        "avg_loss": result.metrics.get("avg_loss", 0.0),
    }


def _trend_aligned(signals: list[dict]) -> list[dict]:
    """Simple, non-ML trend rule using already-computed causal VWAP slope.

    LONG requires positive VWAP slope; SHORT requires negative VWAP slope.
    Missing/zero slope is rejected. This intentionally resembles a simple
    discretionary trend trader rather than another Alpha-Bot gate.
    """
    selected = []
    for s in signals:
        slope = s.get("vwap_slope")
        if slope is None:
            continue
        direction = str(s.get("direction", "")).lower()
        if direction == "long" and float(slope) > 0:
            selected.append(s)
        elif direction == "short" and float(slope) < 0:
            selected.append(s)
    return selected


def _random_matched(signals: list[dict], target_count: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    if target_count <= 0:
        return []
    if target_count >= len(signals):
        return list(signals)
    return rng.sample(signals, target_count)


def _gate_value(signals: list[dict]) -> dict:
    by_stage: dict[str, list[dict]] = defaultdict(list)
    for s in signals:
        stage = s.get("rejection_stage") or "accepted"
        by_stage[stage].append(s)
    result = {}
    for stage, items in sorted(by_stage.items()):
        result[stage] = _score(items)
    return result


def _missed_winners(signals: list[dict], top_n: int = 30) -> list[dict]:
    rows = []
    for s in signals:
        if s.get("final_decision") == "pipeline_approved":
            continue
        o = _outcome(s)
        if not o:
            continue
        rows.append({
            "timestamp": s.get("timestamp"),
            "symbol": s.get("symbol"),
            "direction": s.get("direction"),
            "strategy": s.get("strategy"),
            "rejection_stage": s.get("rejection_stage"),
            "rejection_reason": s.get("rejection_reason"),
            "opportunity_class": s.get("opportunity_class"),
            "regime": s.get("regime"),
            "ml_confidence": s.get("ml_confidence"),
            "fwd_return_60m": o.get("fwd_return"),
            "realized_r_60m": o.get("realized_r"),
            "mfe_r_60m": o.get("mfe_r"),
        })
    return sorted(rows, key=lambda x: float(x.get("fwd_return_60m") or 0), reverse=True)[:top_n]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--symbols", required=True, help="Comma-separated universe")
    parser.add_argument("--output", default="decision_quality_benchmark.json")
    parser.add_argument("--seed", type=int, default=20260813)
    args = parser.parse_args()

    start = _parse_date(args.start)
    end = _parse_date(args.end, end=True)
    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]

    settings = Settings()
    risk_cfg = settings.risk_config()
    cache_path = DATA_DIR / "bars_cache.db"
    dm = DataManager(BlockingProvider(), cache_path)
    dm.validate_cache_coverage = lambda *a, **k: None
    model = SignalModel(DATA_DIR / "models")

    from app.strategies.base import REGISTRY
    strategies = {name: {"enabled": True, "params": {}} for name in REGISTRY.keys()}
    params = BacktestParams(
        symbols=symbols,
        start=start,
        end=end,
        starting_cash=100_000.0,
        slippage_bps=2.0,
        use_ml_gate=True,
        use_dynamic_scanner=False,
        debug_mode=False,
        strategies=strategies,
        risk=risk_cfg,
        cache_policy=CachePolicy.IMMUTABLE_CACHE_ONLY,
        ignore_temporal_leakage=False,
        verify_dataset_immutability=True,
    )

    result = await run_backtest(dm, model, params, settings)
    signals = list(result.shadow_signals)
    production = _production_summary(result)

    approved = [s for s in signals if s.get("final_decision") == "pipeline_approved"]
    rejected = [s for s in signals if s.get("final_decision") != "pipeline_approved"]
    trend = _trend_aligned(signals)
    random_matched = _random_matched(signals, len(approved), args.seed)

    by_day = defaultdict(list)
    for s in signals:
        by_day[str(s.get("timestamp", ""))[:10]].append(s)

    report = {
        "manifest": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "symbols": symbols,
            "cache_policy": "IMMUTABLE_CACHE_ONLY",
            "ignore_temporal_leakage": False,
            "starting_cash": 100000.0,
            "slippage_bps": 2.0,
            "production_code_sha": result.code_state_sha256,
            "dataset_sha": result.dataset_sha256,
            "model_sha": result.model_sha256,
            "decision_artifacts_sha": result.decision_artifacts_sha256,
            "provenance": result.provenance,
        },
        "production": production,
        "opportunity_quality": {
            "all_strategy_signals": _score(signals),
            "production_approved_signals": _score(approved),
            "production_rejected_signals": _score(rejected),
            "simple_vwap_trend": _score(trend),
            "random_matched_to_production_count": _score(random_matched),
        },
        "gate_value": _gate_value(signals),
        "top_missed_winners": _missed_winners(signals),
        "daily_candidate_counts": {day: len(items) for day, items in sorted(by_day.items())},
        "limitations": [
            "Signal-only, trend, and random metrics are opportunity-quality benchmarks, not portfolio-equivalent P&L.",
            "They intentionally do not bypass causal data rules; all forward outcomes are precomputed by ShadowLedger from bars after the candidate timestamp.",
            "A production profitability verdict requires a portfolio-equivalent backtest for each policy, not just this post-hoc benchmark.",
        ],
    }

    output = Path(args.output)
    output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "production": production,
        "opportunity_quality": report["opportunity_quality"],
        "top_missed_winners": report["top_missed_winners"][:10],
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
