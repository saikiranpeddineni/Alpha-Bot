import pytest
import asyncio
import json
from datetime import datetime, UTC, timedelta
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, AsyncMock

from app.config import Settings
from app.main import create_app
from app.db.database import PolicyNomineeRow, PolicyRejectionRow, PolicyResurrectionRow, ShadowLedgerRow
from app.core.engine import TradingEngine
from app.strategies.reflection import generate_eod_reflection_report


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "test_loop_safety.db"),
        DEFAULT_WATCHLIST="SPY,QQQ",
        DEMO_BAR_SECONDS=3600,
    )
    
    original_run_loop = TradingEngine._run_loop
    TradingEngine._run_loop = AsyncMock()
    
    app = create_app(settings)
    
    with TestClient(app) as c:
        c.post("/api/engine/start", json={"mode": "demo", "confirm": "start"})
        yield c
        
    TradingEngine._run_loop = original_run_loop


def test_reject_nominee_validation_and_persistence(client):
    db = client.app.state.db
    
    # 1. Try to reject non-existent policy nominee -> 404
    r = client.post("/api/strategies/nominees/unknown_policy/reject", json={"operator_id": "op_123", "reason": "No good"})
    assert r.status_code == 404
    assert "No pending nomination" in r.json()["detail"]
    
    # 2. Insert a pending nominee
    db.save_policy_nominee({
        "policy_id": "strategy_a",
        "nominated_at": datetime.now(UTC).isoformat(),
        "approved_count": 20,
        "expectancy": 0.5,
        "edge_over_champion": 0.2,
        "champion_expectancy": 0.3,
        "status": "pending",
        "cohort_fingerprint": "cohort_xyz",
        "config_fingerprint": "config_xyz",
        "champion_config_fingerprint": "config_champ",
    })
    
    # 3. Reject with empty reason -> 400
    r = client.post("/api/strategies/nominees/strategy_a/reject", json={"operator_id": "op_123", "reason": "  "})
    assert r.status_code == 400
    assert "reason/comment is required" in r.json()["detail"]
    
    # 4. Reject successfully -> 200
    r = client.post("/api/strategies/nominees/strategy_a/reject", json={"operator_id": "op_123", "reason": "High variance"})
    assert r.status_code == 200
    assert r.json()["status"] == "success"
    
    # 5. Check database states
    with db._session() as s:
        nominee = s.query(PolicyNomineeRow).filter(PolicyNomineeRow.policy_id == "strategy_a").first()
        assert nominee.status == "rejected"
        
        rejections = s.query(PolicyRejectionRow).filter(PolicyRejectionRow.policy_id == "strategy_a").all()
        assert len(rejections) == 1
        assert rejections[0].rejected_by == "op_123"
        assert rejections[0].reason == "High variance"
        assert rejections[0].config_fingerprint == "config_xyz"


def test_reflection_nominations_cooling_off(client):
    db = client.app.state.db
    engine = client.app.state.engine
    
    # Setup candidate map / EOD reflection inputs by inserting shadow ledger rows
    now = datetime.now(UTC)
    with db._session() as s:
        for i in range(20):
            cand_id = f"sig_{i}"
            payload_chall = {
                "candidate_id": cand_id,
                "policy_id": "strategy_b",
                "strategy": "momentum",
                "timestamp": "2026-01-01T10:00:00",
                "symbol": "SPY",
                "price": 100.0,
                "outcomes": {
                    "close": {
                        "realized_r": 0.6,
                        "entry_price": 100.0,
                        "exit_price": 106.0,
                        "exit_at": "2026-01-01T11:00:00"
                    }
                }
            }
            s.add(ShadowLedgerRow(
                id=f"{cand_id}_strategy_b",
                ts=datetime.now(UTC),
                symbol="SPY",
                strategy="momentum",
                direction="long",
                mode="demo",
                payload=json.dumps(payload_chall),
                final_decision="pipeline_approved",
                policy_id="strategy_b",
                simulated_pnl=6.0
            ))
            
            payload_champ = {
                "candidate_id": cand_id,
                "policy_id": "champion",
                "strategy": "momentum",
                "timestamp": "2026-01-01T10:00:00",
                "symbol": "SPY",
                "price": 100.0,
                "outcomes": {
                    "close": {
                        "realized_r": 0.2,
                        "entry_price": 100.0,
                        "exit_price": 102.0,
                        "exit_at": "2026-01-01T11:00:00"
                    }
                }
            }
            s.add(ShadowLedgerRow(
                id=f"{cand_id}_champion",
                ts=datetime.now(UTC),
                symbol="SPY",
                strategy="momentum",
                direction="long",
                mode="demo",
                payload=json.dumps(payload_champ),
                final_decision="pipeline_approved",
                policy_id="champion",
                simulated_pnl=2.0
            ))
        s.commit()
        
    # Mock challenger config
    class MockChallenger:
        policy_id = "strategy_b"
        def to_snapshot(self):
            return {"ml_threshold": 0.6}
            
    from app.strategies import challenger
    original_get_active = challenger.get_active_challengers
    challenger.get_active_challengers = MagicMock(return_value=[MockChallenger()])
    
    try:
        # 1. Run reflection -> Nominee is created!
        report = generate_eod_reflection_report(db, Settings())
        print("REPORT TEXT:\n", report)
        pending = db.get_pending_nominee_for_policy("strategy_b")
        assert pending is not None
        assert pending["status"] == "pending"
        
        # 2. Reject the nominee -> status is rejected
        r = client.post("/api/strategies/nominees/strategy_b/reject", json={"operator_id": "op_123", "reason": "No edge"})
        assert r.status_code == 200
        
        # 3. Run reflection again -> nominee must NOT be regenerated because of 7-day cooling-off!
        report2 = generate_eod_reflection_report(db, Settings())
        pending2 = db.get_pending_nominee_for_policy("strategy_b")
        assert pending2 is None
        
        # 4. Bypass cooling-off: manually set rejected_at to 10 days ago
        with db._session() as s:
            rej = s.query(PolicyRejectionRow).filter(PolicyRejectionRow.policy_id == "strategy_b").first()
            rej.rejected_at = datetime.utcnow() - timedelta(days=10)
            s.commit()
            
        # 5. Run reflection a third time -> Nominee should be resurrected/re-nominated!
        report3 = generate_eod_reflection_report(db, Settings())
        pending3 = db.get_pending_nominee_for_policy("strategy_b")
        assert pending3 is not None
        assert pending3["status"] == "pending"
        
    finally:
        challenger.get_active_challengers = original_get_active


def test_resurrection_cooling_off_and_limits(client):
    db = client.app.state.db
    
    # Create and reject nominee
    db.save_policy_nominee({
        "policy_id": "strategy_c",
        "nominated_at": datetime.now(UTC).isoformat(),
        "approved_count": 25,
        "expectancy": 0.7,
        "edge_over_champion": 0.35,
        "champion_expectancy": 0.35,
        "status": "pending",
        "cohort_fingerprint": "cohort_abc",
        "config_fingerprint": "config_abc",
        "champion_config_fingerprint": "config_champ",
    })
    
    r = client.post("/api/strategies/nominees/strategy_c/reject", json={"operator_id": "op_123", "reason": "Volatile market"})
    assert r.status_code == 200
    
    # 1. Try to resurrect non-rejected policy -> 404
    r = client.post("/api/strategies/nominees/non_existent/resurrect", json={"operator_id": "op_123", "reason": "Manual force", "override_approved": True})
    assert r.status_code == 404
    
    # 2. Try to resurrect rejected policy in cooling-off WITHOUT override -> 400
    r = client.post("/api/strategies/nominees/strategy_c/resurrect", json={"operator_id": "op_123", "reason": "Force back", "override_approved": False})
    print("RESURRECT IN COOLING OFF:", r.json())
    assert r.status_code == 400
    assert "cooling-off period" in r.json()["detail"]
    
    # 3. Try to resurrect in cooling-off WITH override but empty reason -> 400
    r = client.post("/api/strategies/nominees/strategy_c/resurrect", json={"operator_id": "op_123", "reason": "  ", "override_approved": True})
    assert r.status_code == 400
    assert "override reason/comments are required" in r.json()["detail"]
    
    # 4. Resurrect successfully in cooling-off WITH override and reason -> 200
    r = client.post("/api/strategies/nominees/strategy_c/resurrect", json={"operator_id": "op_123", "reason": "Market stabilized", "override_approved": True})
    assert r.status_code == 200
    assert r.json()["status"] == "success"
    assert r.json()["resurrection_count"] == 1
    
    # Verify database state reset to pending
    pending = db.get_pending_nominee_for_policy("strategy_c")
    assert pending is not None
    assert pending["status"] == "pending"
    
    # 5. Reject it a 2nd time
    client.post("/api/strategies/nominees/strategy_c/reject", json={"operator_id": "op_123", "reason": "Second failure"})
    
    # 6. Resurrect 2nd time (cooling-off bypass using old timestamp)
    with db._session() as s:
        rejs = s.query(PolicyRejectionRow).filter(PolicyRejectionRow.policy_id == "strategy_c").all()
        for rej in rejs:
            rej.rejected_at = datetime.utcnow() - timedelta(days=10)
        s.commit()
        
    r = client.post("/api/strategies/nominees/strategy_c/resurrect", json={"operator_id": "op_123", "reason": "Stabilized again"})
    print("RESURRECT 2ND TIME:", r.json())
    assert r.status_code == 200
    
    # 7. Reject it a 3rd time
    client.post("/api/strategies/nominees/strategy_c/reject", json={"operator_id": "op_123", "reason": "Third failure"})
    
    # 8. Resurrect 3rd time (cooling-off bypass using old timestamp)
    with db._session() as s:
        rejs = s.query(PolicyRejectionRow).filter(PolicyRejectionRow.policy_id == "strategy_c").all()
        for rej in rejs:
            rej.rejected_at = datetime.utcnow() - timedelta(days=10)
        s.commit()
        
    r = client.post("/api/strategies/nominees/strategy_c/resurrect", json={"operator_id": "op_123", "reason": "Stabilized again"})
    assert r.status_code == 200
    
    # 9. Reject it a 4th time
    client.post("/api/strategies/nominees/strategy_c/reject", json={"operator_id": "op_123", "reason": "Fourth failure"})
    
    # 10. Try to resurrect 4th time -> should fail (exceeded limit of 3)
    with db._session() as s:
        rejs = s.query(PolicyRejectionRow).filter(PolicyRejectionRow.policy_id == "strategy_c").all()
        for rej in rejs:
            rej.rejected_at = datetime.utcnow() - timedelta(days=10)
        s.commit()
        
    r = client.post("/api/strategies/nominees/strategy_c/resurrect", json={"operator_id": "op_123", "reason": "Stabilized again"})
    assert r.status_code == 400
    assert "Max resurrection limit of 3 reached" in r.json()["detail"]
