"""Exact production-path backtest via the ReplayRuntime.

Drives the authoritative TradingEngine._tick under deterministic clock/UUID
bindings against an immutable DB+model snapshot. This is THE backtester since
the legacy parallel orchestration loop was removed.

Usage (from repo root):
    .venv\\Scripts\\python backend\\scripts\\run_exact_backtest.py \
        --start 2026-08-10 --end 2026-08-21 \
        --symbols SPY,QQQ,AAPL,NVDA [--warmup 600]
"""
import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.db.database import Database
from app.replay.runtime import ReplayRuntime


def _fl(v):
    try:
        return float(v)
    except Exception:
        return 0.0


async def amain() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--warmup", type=int, default=600)
    ap.add_argument("--out", default="", help="optional path to write the full trades JSON")
    args = ap.parse_args()

    settings = get_settings()
    db = Database(settings.resolved_db_path)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or None

    rt = ReplayRuntime(settings, db)
    t0 = time.perf_counter()
    result = await rt.run_replay_fast_async(
        start_date=args.start,
        end_date=args.end,
        symbols=symbols,
        warmup_bars=args.warmup,
    )
    elapsed = time.perf_counter() - t0

    summary = result["session"]
    trades = result.get("trades", [])

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"trades": trades, "equity_curve": result.get("equity_curve", []),
             "session": summary}, default=str, indent=1))
        print(f"[out] wrote {len(trades)} trades -> {args.out}")

    wins = [t for t in trades if _fl(t.get("pnl")) > 0]
    losses = [t for t in trades if _fl(t.get("pnl")) <= 0]
    gp = sum(_fl(t.get("pnl")) for t in wins)
    gl = abs(sum(_fl(t.get("pnl")) for t in losses))

    print("\n=============== EXACT PROD-PATH REPLAY ===============")
    print(f"window      : {args.start} -> {args.end}")
    print(f"symbols     : {len(symbols or summary.get('symbols', []))} | ticks={summary.get('production_ticks')}")
    print(f"wall time   : {elapsed/60:.1f} min")
    print(f"trades      : {len(trades)}  WR={len(wins)/max(1,len(trades))*100:.1f}%  "
          f"PF={'inf' if gl == 0 else round(gp/gl, 3)}")
    print(f"NET         : {gp-gl:+,.2f}")

    by_strat = defaultdict(lambda: [0, 0.0])
    for t in trades:
        by_strat[str(t.get("strategy", "?"))][0] += 1
        by_strat[str(t.get("strategy", "?"))][1] += _fl(t.get("pnl"))
    print("\nper strategy:")
    for s, (n, net) in sorted(by_strat.items(), key=lambda kv: kv[1][1]):
        print(f"  {s:18s} n={n:3d} net={net:+10,.2f}")

    by_exit = defaultdict(lambda: [0, 0.0])
    for t in trades:
        k = str(t.get("exit_reason", "?"))[:46]
        by_exit[k][0] += 1
        by_exit[k][1] += _fl(t.get("pnl"))
    print("\nexit reasons:")
    for k, (n, net) in sorted(by_exit.items(), key=lambda kv: kv[1][1]):
        print(f"  {n:3d}x {net:+10,.2f}  {k}")

    daily = defaultdict(float)
    for t in trades:
        d = str(t.get("exit_at", ""))[:10]
        daily[d] += _fl(t.get("pnl"))
    print("\ndaily:")
    for d in sorted(daily):
        print(f"  {d} {daily[d]:+10,.2f}")


if __name__ == "__main__":
    asyncio.run(amain())
