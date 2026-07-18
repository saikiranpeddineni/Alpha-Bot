from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "test_promotion.db"),
        DEFAULT_WATCHLIST="SPY,QQQ",
        DEMO_BAR_SECONDS=3600,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_get_challengers(client):
    r = client.get("/api/strategies/challengers")
    assert r.status_code == 200
    challengers = r.json()
    assert len(challengers) == 3
    policy_ids = [c["policy_id"] for c in challengers]
    assert "challenger_low_ml" in policy_ids
    assert "challenger_high_ml" in policy_ids
    assert "challenger_aggressive_score" in policy_ids


def test_get_shadow_performance(client):
    r = client.get("/api/strategies/shadow-performance")
    assert r.status_code == 200
    performance = r.json()
    assert isinstance(performance, list)


def test_promote_policy(client):
    # Promote a valid challenger
    r = client.post("/api/strategies/promote", json={"policy_id": "challenger_low_ml"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert body["config"]["ml_threshold"] == 0.48

    # Verify invalid policy raises 404
    r = client.post("/api/strategies/promote", json={"policy_id": "invalid_policy_name"})
    assert r.status_code == 404

    # Verify promoting "champion" raises 400
    r = client.post("/api/strategies/promote", json={"policy_id": "champion"})
    assert r.status_code == 400
