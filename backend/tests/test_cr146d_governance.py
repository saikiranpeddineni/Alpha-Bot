import pytest
from datetime import datetime, UTC
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from app.config import Settings
from app.main import create_app
from app.db.database import PolicyPromotionRow, PolicyEvidenceRow
from app.strategies.challenger import get_active_challengers


def nominate_policy_for_test(db, settings, policy_id: str):
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
    db.save_spawned_strategy(policy_id, 1000.0)


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "test_governance.db"),
        DEFAULT_WATCHLIST="SPY,QQQ",
        DEMO_BAR_SECONDS=3600,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_frozen_evidence_endpoint(client):
    db = client.app.state.db
    settings = client.app.state.settings
    policy_id = "challenger_low_ml"
    
    nominate_policy_for_test(db, settings, policy_id)

    # Insert a replayed shadow signal so we have data for metrics
    mock_sig = {
        "candidate_id": "cand_1",
        "policy_id": policy_id,
        "timestamp": "2026-01-06T10:00:00",
        "symbol": "AAPL",
        "strategy": "momentum",
        "direction": "long",
        "signal_price": 100.0,
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
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
    }
    db.save_shadow_signal(
        candidate_id=mock_sig["candidate_id"],
        ts=datetime.fromisoformat(mock_sig["timestamp"]),
        symbol=mock_sig["symbol"],
        strategy=mock_sig["strategy"],
        direction=mock_sig["direction"],
        mode="demo",
        payload=mock_sig
    )

    # Request the evidence
    r = client.get(f"/api/strategies/nominees/{policy_id}/evidence")
    assert r.status_code == 200
    evidence = r.json()

    # Validate structure
    assert "evidence_package_id" in evidence
    assert evidence["policy_id"] == policy_id
    assert "policy_snapshot_diff" in evidence
    assert "performance" in evidence
    assert "confidence" in evidence
    assert "robustness" in evidence
    assert "portfolio_simulation" in evidence

    # Verify config diff exists
    assert "ml_threshold" in evidence["policy_snapshot_diff"]
    
    # Verify bootstrap CI is calculated
    assert "bootstrap_95_ci" in evidence["confidence"]
    assert "mean" in evidence["confidence"]["bootstrap_95_ci"]

    # Verify slippage sensitivity sweep
    slippages = [item["slippage_bps"] for item in evidence["robustness"]["slippage_sensitivity"]]
    assert 0 in slippages
    assert 3 in slippages
    assert 5 in slippages
    assert 10 in slippages

    # Verify EOD simulator metrics are embedded
    assert evidence["portfolio_simulation"]["total_trades"] == 1

    # Verify that the package is FROZEN (idempotency check)
    original_id = evidence["evidence_package_id"]
    original_ts = evidence["timestamp"]

    # Insert another trade afterwards
    mock_sig_2 = {**mock_sig, "candidate_id": "cand_2", "timestamp": "2026-01-06T11:00:00"}
    db.save_shadow_signal(
        candidate_id=mock_sig_2["candidate_id"],
        ts=datetime.fromisoformat(mock_sig_2["timestamp"]),
        symbol=mock_sig_2["symbol"],
        strategy=mock_sig_2["strategy"],
        direction=mock_sig_2["direction"],
        mode="demo",
        payload=mock_sig_2
    )

    r2 = client.get(f"/api/strategies/nominees/{policy_id}/evidence")
    assert r2.status_code == 200
    evidence2 = r2.json()

    # The evidence MUST be returned from cache, so ID and timestamps are identical
    assert evidence2["evidence_package_id"] == original_id
    assert evidence2["timestamp"] == original_ts


def test_promotion_governance_controls(client):
    db = client.app.state.db
    settings = client.app.state.settings
    policy_id = "challenger_low_ml"
    
    nominate_policy_for_test(db, settings, policy_id)

    # 1. Fetch frozen evidence first to get a valid evidence_package_id
    mock_sig = {
        "candidate_id": "cand_1",
        "policy_id": policy_id,
        "timestamp": "2026-01-06T10:00:00",
        "symbol": "AAPL",
        "strategy": "momentum",
        "direction": "long",
        "signal_price": 100.0,
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
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
    }
    db.save_shadow_signal(
        candidate_id=mock_sig["candidate_id"],
        ts=datetime.fromisoformat(mock_sig["timestamp"]),
        symbol=mock_sig["symbol"],
        strategy=mock_sig["strategy"],
        direction=mock_sig["direction"],
        mode="demo",
        payload=mock_sig
    )
    
    evidence_res = client.get(f"/api/strategies/nominees/{policy_id}/evidence")
    evidence = evidence_res.json()
    evidence_package_id = evidence["evidence_package_id"]

    # 2. Verify promotion fails without promoter identity
    r = client.post("/api/strategies/promote", json={
        "policy_id": policy_id,
        "promoter_id": "",
        "reason": "testing governance",
        "evidence_package_id": evidence_package_id
    })
    assert r.status_code == 400

    # 3. Verify promotion fails with invalid evidence package ID
    r = client.post("/api/strategies/promote", json={
        "policy_id": policy_id,
        "promoter_id": "admin_user",
        "reason": "testing governance",
        "evidence_package_id": "invalid_package_id"
    })
    assert r.status_code == 400

    # 4. Successful Promotion with all fields
    r = client.post("/api/strategies/promote", json={
        "policy_id": policy_id,
        "promoter_id": "admin_user",
        "reason": "approved outperformance",
        "evidence_package_id": evidence_package_id
    })
    assert r.status_code == 200
    
    # 5. Verify log is saved in append-only promotions DB table
    promotions = db.get_policy_promotions()
    assert len(promotions) == 1
    log = promotions[0]
    assert log["policy_id"] == policy_id
    assert log["promoter_id"] == "admin_user"
    assert log["reason"] == "approved outperformance"
    assert log["evidence_package_id"] == evidence_package_id
    assert log["evidence_package"]["evidence_package_id"] == evidence_package_id


def test_promotion_authorization_enforcement(tmp_path):
    # Set up settings with admin token configured
    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "test_auth.db"),
        DEFAULT_WATCHLIST="SPY,QQQ",
        DEMO_BAR_SECONDS=3600,
        ALPHADESK_ADMIN_TOKEN="supersecret"
    )
    app = create_app(settings)
    
    with TestClient(app) as client:
        db = client.app.state.db
        policy_id = "challenger_low_ml"
        nominate_policy_for_test(db, settings, policy_id)
        
        # 1. Promote without token -> 401
        r = client.post("/api/strategies/promote", json={
            "policy_id": policy_id,
            "promoter_id": "admin_user",
            "reason": "testing auth",
            "evidence_package_id": ""
        })
        assert r.status_code == 401
        
        # 2. Promote with invalid token -> 403
        r = client.post("/api/strategies/promote", json={
            "policy_id": policy_id,
            "promoter_id": "admin_user",
            "reason": "testing auth",
            "evidence_package_id": ""
        }, headers={"Authorization": "Bearer wrongtoken"})
        assert r.status_code == 403
        
        # 3. Promote with valid token -> 200
        r = client.post("/api/strategies/promote", json={
            "policy_id": policy_id,
            "promoter_id": "admin_user",
            "reason": "testing auth",
            "evidence_package_id": ""
        }, headers={"Authorization": "Bearer supersecret"})
        assert r.status_code == 200


def test_promotion_atomic_rollback_on_failure(client):
    db = client.app.state.db
    engine = client.app.state.engine
    settings = client.app.state.settings
    policy_id = "challenger_low_ml"
    
    nominate_policy_for_test(db, settings, policy_id)
    
    # Store initial Champion values
    initial_snapshot = db.get_setting("active_policy_snapshot")
    initial_ml_thresh = db.get_setting("ml_threshold")
    
    # Inject database failure during promotion save
    db.save_policy_promotion = MagicMock(side_effect=Exception("Disk full / DB connection lost"))
    
    # Attempt promotion -> should return 500 and trigger rollback
    r = client.post("/api/strategies/promote", json={
        "policy_id": policy_id,
        "promoter_id": "admin_user",
        "reason": "approved outperformance",
        "evidence_package_id": ""
    })
    assert r.status_code == 500
    assert "Failed to promote policy" in r.json()["detail"]
    
    # Verify DB states were rolled back to original Champion values
    assert db.get_setting("active_policy_snapshot") == initial_snapshot
    assert db.get_setting("ml_threshold") == initial_ml_thresh
    
    # Verify engine runtime config was also rolled back
    if getattr(engine, "pipeline", None) is not None:
        assert engine.pipeline.ml_threshold == (initial_snapshot["ml_threshold"] if initial_snapshot else settings.ml_confidence_threshold)


def test_immutable_history_no_update_delete():
    # Assert that no update/delete methods exist for PolicyPromotionRow or PolicyEvidenceRow on the Database class
    from app.db.database import Database
    methods = dir(Database)
    
    # Check that update/delete keywords don't match any promotion/evidence methods
    governance_methods = [m for m in methods if "promotion" in m or "evidence" in m]
    
    for m in governance_methods:
        assert "delete" not in m.lower()
        assert "update" not in m.lower()
        assert "remove" not in m.lower()
