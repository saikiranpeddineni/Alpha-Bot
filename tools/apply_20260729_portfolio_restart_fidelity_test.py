"""Add regression coverage for durable entry-risk metadata across restart."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = ROOT / "backend/tests/test_portfolio_restart_risk_fidelity.py"

TEST.write_text(r'''from datetime import UTC, datetime

from app.portfolio.tracker import PortfolioTracker, _OpenLot


def test_checkpoint_roundtrip_preserves_entry_bound_risk_metadata():
    tracker = PortfolioTracker()
    tracker._lots["SPY"] = _OpenLot(
        qty=10.0,
        avg_price=100.0,
        entry_ts=datetime(2026, 7, 28, 14, 30, tzinfo=UTC),
        strategy="momentum",
        stop_loss=99.25,
        initial_stop_loss=98.50,
        initial_risk_per_share=1.50,
        initial_risk_dollars=15.0,
    )

    payload = tracker.checkpoint(account_id="acct")
    lot_payload = payload["lots"]["SPY"]
    assert lot_payload["initial_stop_loss"] == 98.50
    assert lot_payload["initial_risk_dollars"] == 15.0

    restored = PortfolioTracker()
    restored.restore_checkpoint(payload)
    lot = restored._lots["SPY"]
    assert lot.stop_loss == 99.25
    assert lot.initial_stop_loss == 98.50
    assert lot.initial_risk_per_share == 1.50
    assert lot.initial_risk_dollars == 15.0


def test_legacy_checkpoint_uses_stop_as_initial_stop_without_inventing_risk_dollars():
    payload = {
        "lots": {
            "QQQ": {
                "qty": 5.0,
                "avg_price": 200.0,
                "entry_ts": "2026-07-28T14:30:00+00:00",
                "strategy": "orb",
                "stop_loss": 197.0,
                "initial_risk_per_share": 3.0,
            }
        }
    }
    restored = PortfolioTracker()
    restored.restore_checkpoint(payload)
    lot = restored._lots["QQQ"]
    assert lot.initial_stop_loss == 197.0
    assert lot.initial_risk_per_share == 3.0
    assert lot.initial_risk_dollars == 0.0
''', encoding="utf-8")
compile(TEST.read_text(encoding="utf-8"), str(TEST), "exec")
print("Portfolio restart-fidelity regression tests added; syntax compile passed.")
