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


def test_engine_restart_restores_snapshot(tmp_path):
    # Setup settings with a shared database path
    db_path = tmp_path / "test_restart_promotion.db"
    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(db_path),
        DEFAULT_WATCHLIST="SPY,QQQ",
        DEMO_BAR_SECONDS=3600,
    )
    
    # 1. Start initial app, promote "challenger_low_ml"
    app1 = create_app(settings)
    with TestClient(app1) as c1:
        # Start engine to initialize
        r_start = c1.post("/api/engine/start", json={"mode": "demo"})
        assert r_start.status_code == 200

        r = c1.post("/api/strategies/promote", json={"policy_id": "challenger_low_ml"})
        assert r.status_code == 200
        assert r.json()["config"]["ml_threshold"] == 0.48

    # 2. Simulate restart: create a new app instance pointing to the same DB
    app2 = create_app(settings)
    
    # Verify new app's engine has restored the promoted snapshot state after lifespan start and engine start
    with TestClient(app2) as c2:
        r_start2 = c2.post("/api/engine/start", json={"mode": "demo"})
        assert r_start2.status_code == 200
        
        engine = app2.state.engine
        assert engine.pipeline is not None
        assert engine.pipeline.ml_threshold == 0.48
        assert engine.evidence_evaluator.ml_threshold == 0.48

