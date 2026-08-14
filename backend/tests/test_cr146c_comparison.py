import pytest
from datetime import datetime, UTC
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.strategies.reflection import generate_eod_reflection_report


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "test_comparison.db"),
        DEFAULT_WATCHLIST="SPY,QQQ",
        DEMO_BAR_SECONDS=3600,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_portfolio_comparison_and_reflection(client):
    db = client.app.state.db
    
    # 1. Insert mock shadow signals for Champion and Challenger
    mock_signals = [
        # Champion approved signal (AAPL)
        {
            "candidate_id": "cand_1",
            "policy_id": "champion",
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "AAPL",
            "strategy": "momentum",
            "direction": "long",
            "signal_price": 100.0,
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 104.0,
            "trade_score": 85.0,
            "final_decision": "pipeline_approved",
            "replayed": 1,
            "simulated_pnl": 2.0,
            "outcomes": {
                "close": {
                    "pnl_pct": 2.0,
                    "bars_held": 30,
                    "exit_reason": "tp",
                    "exit_timestamp": "2026-01-06T10:30:00"
                }
            }
        },
        # Challenger approved signal (MSFT)
        {
            "candidate_id": "cand_2",
            "policy_id": "challenger_low_ml",
            "timestamp": "2026-01-06T10:00:00",
            "symbol": "MSFT",
            "strategy": "momentum",
            "direction": "long",
            "signal_price": 200.0,
            "entry_price": 200.0,
            "stop_loss": 196.0,
            "take_profit": 208.0,
            "trade_score": 90.0,
            "final_decision": "pipeline_approved",
            "replayed": 1,
            "simulated_pnl": -1.0,
            "outcomes": {
                "close": {
                    "pnl_pct": -1.0,
                    "bars_held": 45,
                    "exit_reason": "sl",
                    "exit_timestamp": "2026-01-06T10:45:00"
                }
            }
        }
    ]

    for sig in mock_signals:
        db.save_shadow_signal(
            candidate_id=sig["candidate_id"],
            ts=datetime.fromisoformat(sig["timestamp"]),
            symbol=sig["symbol"],
            strategy=sig["strategy"],
            direction=sig["direction"],
            mode="demo",
            payload=sig
        )

    # 2. Run EOD reflection report generation
    now = datetime(2026, 1, 6, 17, 0, 0, tzinfo=UTC)
    from app.config import Settings
    settings = Settings()
    report_text = generate_eod_reflection_report(db, now=now, settings=settings)

    # Verify that the report contains the portfolio simulation header and comparison table
    assert "## Portfolio-Level Virtual Simulation Comparison" in report_text
    assert "| Policy ID | Return | Max Drawdown | Win Rate | Profit Factor |" in report_text
    assert "champion" in report_text
    assert "challenger_low_ml" in report_text

    # 3. Call the API route `/api/strategies/portfolio-comparison`
    r = client.get("/api/strategies/portfolio-comparison")
    assert r.status_code == 200
    comparison_data = r.json()

    # Ensure list is returned with both policies
    assert isinstance(comparison_data, list)
    assert len(comparison_data) >= 2

    # Check structure of returned objects
    policy_ids = [item["policy_id"] for item in comparison_data]
    assert "champion" in policy_ids
    assert "challenger_low_ml" in policy_ids

    champ_item = next(item for item in comparison_data if item["policy_id"] == "champion")
    assert "metrics" in champ_item
    assert "equity_curve" in champ_item
    assert champ_item["metrics"]["total_trades"] == 1
    assert len(champ_item["equity_curve"]) > 0
