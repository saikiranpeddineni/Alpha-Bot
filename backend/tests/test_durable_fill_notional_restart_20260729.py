"""Regression contract for exact incremental fill pricing across restart.

Materialize with:
    python tools/apply_durable_fill_notional_hardening_20260729.py
"""
from pathlib import Path


def test_fill_progress_persists_notional_with_quantity():
    src = (Path(__file__).parents[1] / "app/db/database.py").read_text(encoding="utf-8")
    assert "cumulative_notional: Mapped[float]" in src
    assert "def processed_fill_notional" in src
    commit = src[src.index("    def commit_fill_processing"):src.index("    def bootstrap_broker_checkpoint")]
    assert "cumulative_notional=cumulative_notional" in commit


def test_tracker_commits_notional_atomically_with_fill_checkpoint():
    src = (Path(__file__).parents[1] / "app/portfolio/tracker.py").read_text(encoding="utf-8")
    body = src[src.index("    def on_fill"):src.index("    def _apply_fill")]
    assert "previous_notional = db.processed_fill_notional" in body
    assert "cumulative_notional = previous_notional + float(effective.qty) * float(effective.price)" in body
    assert "cumulative_notional=cumulative_notional" in body


def test_alpaca_restart_prefers_durable_notional_for_next_delta_price():
    src = (Path(__file__).parents[1] / "app/brokers/alpaca.py").read_text(encoding="utf-8")
    poll = src[src.index("    async def poll_fills"):src.index("    def ack_fill_processed")]
    assert "durable_notional = db.processed_fill_notional" in poll
    assert "prev_notional = durable_notional if durable_notional > 0 else processed * cum_avg_price" in poll
    assert "delta_price = round((cum_notional - prev_notional) / delta, 4)" in poll


def test_fill_progress_schema_migration_is_backward_compatible():
    src = (Path(__file__).parents[1] / "app/db/database.py").read_text(encoding="utf-8")
    assert 'PRAGMA table_info(broker_fill_progress)' in src
    assert 'ALTER TABLE broker_fill_progress ADD COLUMN cumulative_notional FLOAT DEFAULT 0.0' in src
