"""Replay a chronological JSONL observation stream through the shadow spawn army.

Input rows are either:
  {"kind":"bar","ts":"...+00:00","symbol":"AAPL","open":...,"high":...,"low":...,"close":...}
  {"kind":"candidate", ...same candidate observation schema used by SpawnShadowCoordinator...}

This script is analysis-only: no broker, production engine, config, or model writes.
"""
from __future__ import annotations
import argparse
import json
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.research.spawn.spawn_evolution import EvolutionSettings, SpawnEvolutionEngine
from app.research.spawn.spawn_executor import ChildPolicyExecutor


def parse_ts(value):
    ts = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(ts, datetime) or ts.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware: {value!r}")
    return ts


def load_rows(path: Path):
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            row["ts"] = parse_ts(row.get("ts"))
            row["_line"] = line_number
            rows.append(row)
    rows.sort(key=lambda row: (row["ts"], 0 if row.get("kind") == "bar" else 1, row["_line"]))
    return rows


def retire_executor(executor: ChildPolicyExecutor, ts: datetime) -> int:
    """Match live coordinator retirement: flatten at last causally known marks."""
    exits = 0
    for symbol in list(executor.portfolio.positions):
        position = executor.portfolio.positions[symbol]
        price = float(executor.portfolio.last_prices.get(symbol, position.entry_price))
        executor.portfolio.close_virtual_position(symbol, price, "RETIRED_FLATTEN", ts)
        exits += 1
    executor.status = "RETIRED"
    return exits


def flatten_active_at_end(executors: dict[str, ChildPolicyExecutor], ts: datetime) -> int:
    exits = 0
    for executor in executors.values():
        if executor.status != "ACTIVE":
            continue
        for symbol in list(executor.portfolio.positions):
            position = executor.portfolio.positions[symbol]
            price = float(executor.portfolio.last_prices.get(symbol, position.entry_price))
            executor.portfolio.close_virtual_position(symbol, price, "REPLAY_END_FLATTEN", ts)
            exits += 1
    return exits


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--max-population", type=int, default=24)
    parser.add_argument("--evolve-every", type=int, default=250)
    parser.add_argument("--min-trades", type=int, default=12)
    parser.add_argument(
        "--flatten-at-end",
        action="store_true",
        help="Realize all still-open virtual positions at their last causally observed marks.",
    )
    parser.add_argument("--output", type=Path, default=Path("spawn_army_replay_report.json"))
    args = parser.parse_args()

    rows = load_rows(args.input)
    if not rows:
        raise SystemExit("No replay rows")

    settings = EvolutionSettings(
        max_population=args.max_population,
        elite_count=min(6, args.max_population - 1),
        min_trades_for_selection=args.min_trades,
    )
    evolution = SpawnEvolutionEngine(settings)
    executors = {
        config.policy_id: ChildPolicyExecutor(config)
        for config in evolution.bootstrap_configs(rows[0]["ts"])
    }
    candidates = 0
    bars = 0
    exits = 0
    retirement_exits = 0

    for row in rows:
        kind = row.get("kind")
        if kind == "bar":
            bars += 1
            bar_observation = {
                "observation_type": "MARKET_BAR",
                "symbol": str(row["symbol"]).upper(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "ts": row["ts"],
            }
            for executor in list(executors.values()):
                if executor.status != "ACTIVE":
                    continue
                record = executor.process_market_bar(bar_observation)
                if record:
                    exits += 1
        elif kind == "candidate":
            candidates += 1
            for executor in list(executors.values()):
                executor.evaluate_observation(row)
            if candidates % max(1, args.evolve_every) == 0:
                for policy_id in evolution.retirement_candidates(executors):
                    retirement_exits += retire_executor(executors[policy_id], row["ts"])
                for config in evolution.next_generation(executors, row["ts"]):
                    executors[config.policy_id] = ChildPolicyExecutor(config)
        else:
            raise ValueError(f"Unknown kind on line {row['_line']}: {kind!r}")

    replay_end_exits = 0
    if args.flatten_at_end:
        replay_end_exits = flatten_active_at_end(executors, rows[-1]["ts"])

    ranking = evolution.rank(executors.values())
    report = {
        "input": str(args.input),
        "rows": len(rows),
        "bars": bars,
        "candidates": candidates,
        "virtual_exits": exits + retirement_exits + replay_end_exits,
        "natural_bar_exits": exits,
        "retirement_exits": retirement_exits,
        "replay_end_exits": replay_end_exits,
        "flatten_at_end": args.flatten_at_end,
        "generation": evolution.generation,
        "population": len(executors),
        "active": sum(executor.status == "ACTIVE" for executor in executors.values()),
        "ranking": [item.__dict__ for item in ranking],
    }
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
