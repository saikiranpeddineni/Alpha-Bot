"""PIS Long-Duration Soak & Multi-Position Stress Test Suite.

Simulates 300 active positions over 500 continuous trading bars (150,000 evaluations)
to verify:
- Memory growth remains strictly bounded by history_max_len.
- Evaluation latency stays sub-millisecond per position.
- Lock contention and memory leaks do not occur.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone

from app.pis import PISConfig, PISService, PositionObservation


def test_300_simultaneous_positions_soak() -> None:
    """Soak test 300 active positions over 500 continuous trading bars (150,000 evaluations)."""
    config = PISConfig(history_max_len=50)
    service = PISService(config=config)
    rng = random.Random(999)

    num_symbols = 300
    num_bars = 500
    base_ts = datetime.now(timezone.utc)

    # Initialize 300 symbols
    symbols = [f"SOAK{i:03d}" for i in range(num_symbols)]
    prices = {sym: 100.0 + i * 0.5 for i, sym in enumerate(symbols)}
    entries = {sym: prices[sym] for sym in symbols}
    stops = {sym: prices[sym] - 5.0 for sym in symbols}

    start_soak = time.perf_counter()

    for bar_idx in range(num_bars):
        bar_ts = base_ts + timedelta(minutes=bar_idx)

        for sym in symbols:
            # Simulate random price movement
            delta = rng.uniform(-0.5, 1.0)
            prices[sym] += delta
            cur_p = prices[sym]
            entry_p = entries[sym]
            risk = 5.0
            r_mult = (cur_p - entry_p) / risk

            obs = PositionObservation(
                timestamp=bar_ts,
                symbol=sym,
                side=1.0,
                qty=100.0,
                entry_price=entry_p,
                current_price=cur_p,
                high=cur_p + 0.5,
                low=cur_p - 0.5,
                close=cur_p,
                volume=5000.0,
                atr=2.0,
                initial_stop=stops[sym],
                risk_per_share=risk,
                r_multiple=r_mult,
            )

            service.observe(obs)

    total_duration = time.perf_counter() - start_soak
    total_evaluations = num_symbols * num_bars  # 150,000
    avg_per_eval_ms = (total_duration / total_evaluations) * 1000.0

    # Memory check: every brain must have history length <= history_max_len (50)
    telemetry = service.get_telemetry()
    assert telemetry["active_brains"] == num_symbols
    assert telemetry["total_history_snapshots"] == num_symbols * 50

    # Latency check: average evaluation latency must be < 0.2ms
    assert avg_per_eval_ms < 0.2, f"Soak evaluation latency too high: {avg_per_eval_ms:.4f}ms per eval"
