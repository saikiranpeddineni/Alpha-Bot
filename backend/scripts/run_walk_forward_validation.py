"""Run strict walk-forward, OOS, and block-bootstrap uncertainty validation.

This is a validation-only command. It reads the local SQLite bar cache in
read-only mode, trains only on bars through ``--train-end``, evaluates on a
later untouched date range, and writes a JSON evidence artifact. It does not
promote models, alter trading settings, or submit orders.

Example (from the repository root)::

    PYTHONPATH=backend ALPHADESK_TRAIN_WORKERS=2 \
      python backend/scripts/run_walk_forward_validation.py \
      --train-end 2026-08-31 --oos-start 2026-09-01 --oos-end 2026-10-14 \
      --symbols SPY,QQQ,AAPL,NVDA \
      --out data/reports/walk_forward_oos_uncertainty.json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.features import build_training_set
from app.ai.prediction import WalkForwardResult, WalkForwardValidator, _trading_metrics

ET = ZoneInfo("America/New_York")


class ValidationBlocked(RuntimeError):
    """Raised when evidence is not strong enough for a strict validation run."""


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO date: {value!r}") from exc


def _local_day_start(value: date) -> pd.Timestamp:
    return pd.Timestamp(value.isoformat(), tz=ET).tz_convert("UTC")


def _local_day_end(value: date) -> pd.Timestamp:
    return pd.Timestamp(f"{value.isoformat()} 23:59:59", tz=ET).tz_convert("UTC")


def _expected_sessions(start: date, end: date) -> set[date]:
    try:
        import exchange_calendars as xcals

        sessions = xcals.get_calendar("XNYS").sessions_in_range(
            pd.Timestamp(start.isoformat()), pd.Timestamp(end.isoformat())
        )
        return {ts.date() for ts in sessions}
    except Exception as exc:  # pragma: no cover - dependency failure is operational
        raise ValidationBlocked(f"XNYS session calendar unavailable: {exc}") from exc


def _load_frames(
    db_path: Path,
    symbols: list[str],
    start: date,
    end: date,
) -> dict[str, pd.DataFrame]:
    if not db_path.exists():
        raise ValidationBlocked(f"bar cache does not exist: {db_path}")
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise ValidationBlocked(f"could not open bar cache read-only: {exc}") from exc

    start_ts = int(_local_day_start(start).timestamp())
    end_ts = int(_local_day_end(end).timestamp())
    frames: dict[str, pd.DataFrame] = {}
    try:
        for symbol in symbols:
            rows = pd.read_sql_query(
                """
                SELECT ts, open, high, low, close, volume
                FROM bars
                WHERE symbol = ? AND tf = '1Min' AND ts BETWEEN ? AND ?
                ORDER BY ts
                """,
                con,
                params=(symbol, start_ts, end_ts),
            )
            if rows.empty:
                raise ValidationBlocked(f"no 1Min bars for {symbol} in requested range")
            rows["ts"] = pd.to_datetime(rows["ts"], unit="s", utc=True)
            frames[symbol] = rows.set_index("ts")[["open", "high", "low", "close", "volume"]]
    finally:
        con.close()
    return frames


def _coverage_report(
    frames: dict[str, pd.DataFrame],
    start: date,
    end: date,
    min_bars_per_session: int,
) -> dict[str, dict]:
    expected = _expected_sessions(start, end)
    report: dict[str, dict] = {}
    for symbol, frame in frames.items():
        et_dates = frame.index.tz_convert(ET).date
        counts = pd.Series(1, index=et_dates).groupby(level=0).sum()
        observed = set(counts.index)
        missing = sorted(expected - observed)
        thin = sorted(str(day) for day, count in counts.items() if int(count) < min_bars_per_session)
        report[symbol] = {
            "rows": int(len(frame)),
            "sessions_expected": int(len(expected)),
            "sessions_observed": int(len(observed & expected)),
            "missing_sessions": [str(day) for day in missing],
            "thin_sessions": thin,
            "min_bars_per_observed_session": int(counts.min()) if len(counts) else 0,
            "max_bars_per_observed_session": int(counts.max()) if len(counts) else 0,
            "first_bar": str(frame.index.min()),
            "last_bar": str(frame.index.max()),
        }
        if missing or thin:
            raise ValidationBlocked(
                f"coverage failed for {symbol}: missing={len(missing)} thin={len(thin)} "
                f"(minimum bars/session={min_bars_per_session})"
            )
    return report


def _calibration_ece(y: np.ndarray, probabilities: np.ndarray) -> float:
    edges = np.linspace(0.0, 1.0, 11)
    ece = 0.0
    for index in range(10):
        mask = (probabilities >= edges[index]) & (
            (probabilities < edges[index + 1]) if index < 9 else (probabilities <= edges[index + 1])
        )
        if mask.any():
            ece += float(mask.mean()) * abs(
                float(probabilities[mask].mean()) - float(y[mask].mean())
            )
    return ece


def _oos_metrics(y: np.ndarray, probabilities: np.ndarray) -> dict:
    if len(y) == 0 or np.unique(y).size < 2:
        raise ValidationBlocked("OOS labels are empty or contain only one class")
    result = dict(_trading_metrics(y, probabilities, threshold=0.5))
    result.update(
        {
            "n_samples": int(len(y)),
            "positive_rate": float(y.mean()),
            "auc": float(roc_auc_score(y, probabilities)),
            "brier": float(brier_score_loss(y, probabilities)),
            "log_loss": float(log_loss(y, probabilities, labels=[0, 1])),
            "ece_10bin": _calibration_ece(y, probabilities),
        }
    )
    return result


def _block_bootstrap(
    y: np.ndarray,
    probabilities: np.ndarray,
    block_labels: np.ndarray,
    samples: int,
    seed: int,
) -> dict:
    """Estimate uncertainty by resampling complete ET trading sessions.

    Resampling sessions rather than individual one-minute rows preserves much
    of the serial and cross-symbol dependence that row-wise bootstrap would
    incorrectly destroy. This is a block-bootstrap Monte Carlo diagnostic,
    not a claim about future strategy returns or execution slippage.
    """
    rng = np.random.default_rng(seed)
    blocks = np.array(sorted(set(block_labels)))
    # The execution-oriented metrics below are additive by session. Computing
    # them from per-session sufficient statistics keeps a 2,000-draw run
    # bounded; repeatedly concatenating 40k+ rows and recalculating AUC would
    # make uncertainty analysis needlessly quadratic. AUC/Brier/ECE remain
    # reported as strict-OOS point estimates above.
    trade_mask = probabilities >= 0.5
    traded_labels = y[trade_mask]
    traded_blocks = block_labels[trade_mask]
    if not len(traded_labels):
        raise ValidationBlocked("OOS decision threshold produced no trades for block bootstrap")
    wins_by_block = np.asarray(
        [float(np.sum(traded_labels[traded_blocks == block])) for block in blocks],
        dtype=float,
    )
    trades_by_block = np.asarray(
        [float(np.sum(traded_blocks == block)) for block in blocks],
        dtype=float,
    )
    draws = {name: [] for name in ("profit_factor", "expectancy_r", "win_rate", "sharpe", "n_trades")}
    infinite_profit_factor_draws = 0

    for _ in range(samples):
        chosen = rng.integers(0, len(blocks), size=len(blocks))
        wins = float(wins_by_block[chosen].sum())
        n_trades = float(trades_by_block[chosen].sum())
        losses = n_trades - wins
        if losses > 0:
            draws["profit_factor"].append(wins / losses)
        else:
            infinite_profit_factor_draws += 1
        if n_trades > 0:
            mean_return = (wins - losses) / n_trades
            variance = max(0.0, 1.0 - mean_return * mean_return)
            draws["expectancy_r"].append(mean_return)
            draws["win_rate"].append(wins / n_trades)
            draws["sharpe"].append(mean_return / np.sqrt(variance + 1e-8) * np.sqrt(252.0))
            draws["n_trades"].append(n_trades)

    def interval(values: list[float]) -> list[float] | None:
        if not values:
            return None
        return [
            float(np.percentile(values, 2.5)),
            float(np.percentile(values, 50.0)),
            float(np.percentile(values, 97.5)),
        ]

    pf = np.asarray(draws["profit_factor"], dtype=float)
    expectancy = np.asarray(draws["expectancy_r"], dtype=float)
    return {
        "method": "session_block_bootstrap_monte_carlo",
        "block_unit": "America/New_York trading session",
        "n_blocks": int(len(blocks)),
        "n_samples": int(samples),
        "seed": int(seed),
        "infinite_profit_factor_draws": int(infinite_profit_factor_draws),
        "probability": {
            "profit_factor_gt_1": float(np.mean(pf > 1.0)) if len(pf) else None,
            "expectancy_gt_0": float(np.mean(expectancy > 0.0)) if len(expectancy) else None,
        },
        "intervals_95": {name: interval(values) for name, values in draws.items()},
    }


def _fold_bootstrap(result: WalkForwardResult, samples: int, seed: int) -> dict:
    """Bootstrap the selected model's temporal validation-fold metrics."""
    folds = result.window_metrics.get(result.best_name, [])
    if not folds:
        return {
            "supported": False,
            "reason": "validator did not expose any non-degenerate fold metrics",
        }
    rng = np.random.default_rng(seed)
    keys = ("profit_factor", "expectancy_r", "win_rate", "sharpe")
    values = {key: np.asarray([float(item[key]) for item in folds], dtype=float) for key in keys}
    draws = {key: [] for key in keys}
    for _ in range(samples):
        indices = rng.integers(0, len(folds), size=len(folds))
        for key in keys:
            draws[key].append(float(values[key][indices].mean()))

    def interval(items: list[float]) -> list[float]:
        return [
            float(np.percentile(items, 2.5)),
            float(np.percentile(items, 50.0)),
            float(np.percentile(items, 97.5)),
        ]

    return {
        "supported": True,
        "n_folds": int(len(folds)),
        "n_samples": int(samples),
        "seed": int(seed),
        "fold_ranges": {
            key: [float(values[key].min()), float(np.median(values[key])), float(values[key].max())]
            for key in keys
        },
        "mean_intervals_95": {key: interval(items) for key, items in draws.items()},
        "probability": {
            "mean_profit_factor_gt_1": float(np.mean(np.asarray(draws["profit_factor"]) > 1.0)),
            "mean_expectancy_gt_0": float(np.mean(np.asarray(draws["expectancy_r"]) > 0.0)),
        },
    }


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None


def _git_dirty() -> bool | None:
    try:
        return bool(
            subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
    except Exception:
        return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-db", type=Path, default=Path("data/bars_cache.db"))
    parser.add_argument("--symbols", default="SPY,QQQ,AAPL,NVDA")
    parser.add_argument("--train-start", type=_date, default=date(2025, 8, 5))
    parser.add_argument("--train-end", type=_date, default=date(2026, 8, 31))
    parser.add_argument("--oos-start", type=_date, default=date(2026, 9, 1))
    parser.add_argument("--oos-end", type=_date, default=date(2026, 10, 14))
    parser.add_argument("--train-days", type=int, default=90)
    parser.add_argument("--val-days", type=int, default=10)
    parser.add_argument("--min-windows", type=int, default=3)
    parser.add_argument("--horizon-bars", type=int, default=15)
    parser.add_argument("--threshold-pct", type=float, default=0.10)
    parser.add_argument("--min-bars-per-session", type=int, default=180)
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20261017)
    parser.add_argument("--train-workers", type=int, default=2)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/reports/walk_forward_oos_uncertainty.json"),
    )
    return parser


def run(args: argparse.Namespace) -> dict:
    if not (args.train_start <= args.train_end < args.oos_start <= args.oos_end):
        raise ValidationBlocked("dates must satisfy train-start <= train-end < oos-start <= oos-end")
    if args.train_days < 1 or args.val_days < 1 or args.min_windows < 1:
        raise ValidationBlocked("train-days, val-days, and min-windows must be positive")
    if args.bootstrap_samples < 100:
        raise ValidationBlocked("bootstrap-samples must be at least 100 for a useful interval")
    if args.train_workers < 1:
        raise ValidationBlocked("train-workers must be positive")

    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    if not symbols:
        raise ValidationBlocked("at least one symbol is required")

    os.environ["ALPHADESK_TRAIN_WORKERS"] = str(args.train_workers)
    np.random.seed(args.seed)
    frames = _load_frames(args.data_db, symbols, args.train_start, args.oos_end)
    coverage = _coverage_report(
        frames,
        args.train_start,
        args.oos_end,
        args.min_bars_per_session,
    )

    train_end_utc = _local_day_end(args.train_end)
    oos_start_utc = _local_day_start(args.oos_start)
    oos_end_utc = _local_day_end(args.oos_end)
    train_frames = {symbol: frame.loc[frame.index <= train_end_utc].copy() for symbol, frame in frames.items()}
    wfv = WalkForwardValidator(
        train_days=args.train_days,
        val_days=args.val_days,
        min_windows=args.min_windows,
        horizon_bars=args.horizon_bars,
        threshold_pct=args.threshold_pct,
    )

    report = {
        "schema": "alphadesk.validation.v1",
        "git_commit": _git_commit(),
        "git_dirty": _git_dirty(),
        "parameters": {
            "data_db": str(args.data_db),
            "symbols": symbols,
            "train_start": str(args.train_start),
            "train_end": str(args.train_end),
            "oos_start": str(args.oos_start),
            "oos_end": str(args.oos_end),
            "train_days": args.train_days,
            "val_days": args.val_days,
            "min_windows": args.min_windows,
            "horizon_bars": args.horizon_bars,
            "threshold_pct": args.threshold_pct,
            "min_bars_per_session": args.min_bars_per_session,
            "bootstrap_samples": args.bootstrap_samples,
            "seed": args.seed,
            "train_workers": args.train_workers,
        },
        "coverage": coverage,
        "leakage_checks": {
            "training_frame_cutoff": str(train_end_utc),
            "oos_frame_start": str(oos_start_utc),
            "oos_frame_cutoff": str(oos_end_utc),
            "training_and_oos_date_ranges_disjoint": True,
            "labels_built_from_frames_truncated_at_oos_end": True,
        },
        "sides": {},
    }

    for side in ("long", "short"):
        print(f"[validation] walk-forward side={side}", flush=True)
        result = wfv.run(train_frames, side=side)
        selected_fold_metrics = result.window_metrics.get(result.best_name, [])
        if (
            result.fallback_used
            or result.windows_evaluated < args.min_windows
            or len(selected_fold_metrics) < args.min_windows
        ):
            raise ValidationBlocked(
                f"strict {side} validation blocked: windows={result.windows_evaluated}, "
                f"non_degenerate_selected={len(selected_fold_metrics)}, "
                f"required={args.min_windows}, fallback_used={result.fallback_used}"
            )

        X_parts, y_parts = [], []
        for frame in frames.values():
            # Truncating before label creation prevents a forward label from
            # reaching beyond the declared OOS end date.
            bounded = frame.loc[frame.index <= oos_end_utc]
            X, y, _weights = build_training_set(
                bounded,
                horizon_bars=args.horizon_bars,
                threshold_pct=args.threshold_pct,
                side=side,
            )
            mask = (X.index >= oos_start_utc) & (X.index <= oos_end_utc)
            if mask.any():
                X_parts.append(X.loc[mask])
                y_parts.append(y.loc[mask])
        if not X_parts:
            raise ValidationBlocked(f"no strict OOS samples for {side}")
        X_oos = pd.concat(X_parts).sort_index()
        y_oos = pd.concat(y_parts).sort_index()
        probabilities = result.best_classifier.predict_proba(X_oos)[:, 1]
        labels = y_oos.to_numpy()
        metrics = _oos_metrics(labels, probabilities)
        block_labels = np.asarray(X_oos.index.tz_convert(ET).date.astype(str))
        oos_days = sorted(set(block_labels))

        report["sides"][side] = {
            "best_classifier": result.best_name,
            "windows_evaluated": result.windows_evaluated,
            "non_degenerate_selected_windows": len(selected_fold_metrics),
            "validator_summary": result.summary,
            "fold_metrics": result.window_metrics.get(result.best_name, []),
            "fold_uncertainty": _fold_bootstrap(
                result,
                samples=args.bootstrap_samples,
                seed=args.seed + (101 if side == "long" else 102),
            ),
            "strict_oos": {
                **metrics,
                "oos_start": str(X_oos.index.min()),
                "oos_end": str(X_oos.index.max()),
                "oos_sessions": int(len(oos_days)),
                "decision_threshold": 0.5,
                "block_bootstrap_monte_carlo": _block_bootstrap(
                    labels,
                    probabilities,
                    block_labels,
                    samples=args.bootstrap_samples,
                    seed=args.seed + (1 if side == "long" else 2),
                ),
            },
        }
        print(
            f"[validation] {side}: best={result.best_name} windows={result.windows_evaluated} "
            f"OOS n={metrics['n_samples']} AUC={metrics['auc']:.4f} "
            f"PF={metrics['profit_factor']:.4f} E={metrics['expectancy_r']:.4f}",
            flush=True,
        )
    return report


def main() -> int:
    args = _parser().parse_args()
    try:
        report = run(args)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(f"[validation] wrote {args.out}")
        return 0
    except ValidationBlocked as exc:
        print(f"[validation] BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
