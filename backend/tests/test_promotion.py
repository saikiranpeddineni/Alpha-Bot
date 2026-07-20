from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.strategies.challenger import ChallengerConfig


def nominate_policy_for_test(db, settings, policy_id: str):
    from app.strategies.challenger import get_active_challengers
    import hashlib
    import json
    
    challengers = get_active_challengers()
    challenger = next((c for c in challengers if c.policy_id == policy_id), None)
    if not challenger:
        return
        
    snapshot = challenger.to_snapshot()
    cleaned = {k: v for k, v in snapshot.items() if k not in ("snapshot_at", "snapshot_version")}
    config_fp = hashlib.sha256(json.dumps(cleaned, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    
    champ_snapshot = db.get_setting("active_policy_snapshot")
    if not champ_snapshot:
        champ_snapshot = {
            "policy_id": "champion",
            "ml_threshold": settings.ml_confidence_threshold,
            "score_threshold_offset": 0.0,
            "risk_config": settings.risk_config().model_dump()
        }
    cleaned_champ = {k: v for k, v in champ_snapshot.items() if k not in ("snapshot_at", "snapshot_version")}
    champ_fp = hashlib.sha256(json.dumps(cleaned_champ, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    
    db.save_policy_nominee({
        "policy_id": policy_id,
        "nominated_at": "2026-01-06T12:00:00",
        "approved_count": 15,
        "expectancy": 0.8,
        "edge_over_champion": 0.2,
        "champion_expectancy": 0.6,
        "status": "pending",
        "cohort_fingerprint": "cohort_123",
        "config_fingerprint": config_fp,
        "champion_config_fingerprint": champ_fp,
    })

    with db._session() as s:
        from app.db.database import SpawnedStrategyRow
        from datetime import UTC, datetime
        s.add(SpawnedStrategyRow(policy_id=policy_id, status="ACTIVE", seed_capital=10000.0, spawned_at=datetime.now(UTC)))
        s.commit()


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
    nominate_policy_for_test(client.app.state.db, client.app.state.settings, "challenger_low_ml")
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
    nominate_policy_for_test(app1.state.db, app1.state.settings, "challenger_low_ml")
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


def test_promote_aggressive_score_adjusts_thresholds(tmp_path):
    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "test_promote_aggressive.db"),
        DEFAULT_WATCHLIST="SPY,QQQ",
        DEMO_BAR_SECONDS=3600,
    )
    app = create_app(settings)
    nominate_policy_for_test(app.state.db, app.state.settings, "challenger_aggressive_score")
    with TestClient(app) as client:
        # Start engine
        r_start = client.post("/api/engine/start", json={"mode": "demo"})
        assert r_start.status_code == 200
        
        # Save baseline thresholds
        engine = app.state.engine
        baseline_event = engine.risk_config.score_threshold_event
        
        # Promote aggressive score policy (should have offset of -10.0)
        r = client.post("/api/strategies/promote", json={"policy_id": "challenger_aggressive_score"})
        assert r.status_code == 200
        body = r.json()
        assert body["config"]["score_threshold_offset"] == -10.0
        
        # Verify in-memory risk config thresholds are lowered by 10 points
        assert engine.risk_config.score_threshold_event == baseline_event - 10.0
        assert engine.pipeline.trade_score_engine.risk_config.score_threshold_event == baseline_event - 10.0
        
        # Verify DB stored risk config is NOT polluted with the offset (base config remains clean)
        stored_risk = app.state.db.get_setting("risk_config") or {}
        # If it was saved, it should be the clean base settings.
        # But wait, we did not write base_risk to the DB during promotion, we kept stored_risk as is!
        # So it should not have offsetted values:
        if "score_threshold_event" in stored_risk:
            assert stored_risk["score_threshold_event"] == baseline_event

        # Simulate update_risk_config (should preserve base configuration correctly in DB)
        engine.update_risk_config({"risk_per_trade_pct": 1.0})
        # Running risk config should still have the offset
        assert engine.risk_config.score_threshold_event == baseline_event - 10.0
        # DB base configuration should NOT have the offset
        db_stored = app.state.db.get_setting("risk_config") or {}
        assert db_stored.get("risk_per_trade_pct") == 1.0
        if "score_threshold_event" in db_stored:
            assert db_stored["score_threshold_event"] == baseline_event

    # Simulate restart
    app2 = create_app(settings)
    with TestClient(app2) as client2:
        r_start2 = client2.post("/api/engine/start", json={"mode": "demo"})
        assert r_start2.status_code == 200
        
        engine2 = app2.state.engine
        # Verify restarted engine correctly restores both ml_threshold (0.50) and score offset (-10)
        assert engine2.pipeline.ml_threshold == 0.50
        assert engine2.risk_config.score_threshold_event == baseline_event - 10.0


