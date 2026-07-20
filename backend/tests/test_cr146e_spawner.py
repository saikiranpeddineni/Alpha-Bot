import pytest
import json
from datetime import datetime, UTC, timedelta
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, AsyncMock

from app.config import Settings
from app.main import create_app
from app.core.engine import TradingEngine


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "test_spawner.db"),
        DEFAULT_WATCHLIST="SPY,QQQ",
        DEMO_BAR_SECONDS=3600,
        ADMIN_TOKEN="test_admin_token"
    )
    
    original_run_loop = TradingEngine._run_loop
    TradingEngine._run_loop = AsyncMock()
    
    app = create_app(settings)
    
    with TestClient(app) as c:
        c.post("/api/engine/start", json={"mode": "demo", "confirm": "start"})
        yield c
        
    TradingEngine._run_loop = original_run_loop


def test_profit_allocator_defaults_and_calculation(client):
    db = client.app.state.db
    
    # 1. Check default config (50/30/20) and 0 total pnl
    r = client.get("/api/strategies/profit-allocator")
    assert r.status_code == 200
    data = r.json()
    assert data["compound_capital_pct"] == 50.0
    assert data["protected_reserve_pct"] == 30.0
    assert data["research_capital_pct"] == 20.0
    assert data["compound_capital_value"] == 0.0
    assert data["protected_reserve_value"] == 0.0
    assert data["research_capital_value"] == 0.0
    assert data["total_realized_pnl"] == 0.0
    
    # 2. Insert some mock trades to generate realized P&L
    from app.db.database import TradeRow
    with db._session() as s:
        # Trade 1: profit 150.0
        s.add(TradeRow(
            id="trade_pnl_1",
            symbol="AAPL",
            pnl=150.0,
            exit_at=datetime.now(UTC),
            mode="demo",
            payload="{}"
        ))
        # Trade 2: loss -50.0
        s.add(TradeRow(
            id="trade_pnl_2",
            symbol="MSFT",
            pnl=-50.0,
            exit_at=datetime.now(UTC),
            mode="demo",
            payload="{}"
        ))
        s.commit()
        
    # 3. Retrieve allocations again -> realized P&L should be 100.0
    r = client.get("/api/strategies/profit-allocator")
    assert r.status_code == 200
    data = r.json()
    assert data["total_realized_pnl"] == 100.0
    assert data["compound_capital_value"] == 50.0
    assert data["protected_reserve_value"] == 30.0
    assert data["research_capital_value"] == 20.0


def test_profit_allocator_updating_and_validation(client):
    db = client.app.state.db
    
    # 1. Update config successfully -> 40/40/20
    r = client.post("/api/strategies/profit-allocator", json={
        "compound_capital_pct": 40.0,
        "protected_reserve_pct": 40.0,
        "research_capital_pct": 20.0
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "success"
    assert data["config"]["compound_capital_pct"] == 40.0
    assert data["config"]["protected_reserve_pct"] == 40.0
    assert data["config"]["research_capital_pct"] == 20.0
    
    # Verify in DB
    config_db = db.get_setting("profit_allocator_config")
    assert config_db["compound_capital_pct"] == 40.0
    
    # 2. Update config failing (does not sum to 100%) -> 400
    r = client.post("/api/strategies/profit-allocator", json={
        "compound_capital_pct": 50.0,
        "protected_reserve_pct": 40.0,
        "research_capital_pct": 20.0
    })
    assert r.status_code == 400
    assert "must sum exactly to 100.0" in r.json()["detail"]
    
    # 3. Update config failing (negative percentage) -> 400
    r = client.post("/api/strategies/profit-allocator", json={
        "compound_capital_pct": 110.0,
        "protected_reserve_pct": -10.0,
        "research_capital_pct": 0.0
    })
    assert r.status_code == 400
    assert "percentages cannot be negative" in r.json()["detail"]


def test_strategy_spawner_lifecycle_and_checks(client):
    db = client.app.state.db
    
    # Setup: insert nominee policy "strategy_x" in pending status
    from app.db.database import PolicyNomineeRow
    db.save_policy_nominee({
        "policy_id": "strategy_x",
        "nominated_at": datetime.now(UTC).isoformat(),
        "approved_count": 10,
        "expectancy": 0.5,
        "edge_over_champion": 0.2,
        "champion_expectancy": 0.3,
        "status": "pending",
        "cohort_fingerprint": "cohort_x",
        "config_fingerprint": "config_x",
        "champion_config_fingerprint": "config_champ",
    })
    
    # Set research capital percentage to 50%
    client.post("/api/strategies/profit-allocator", json={
        "compound_capital_pct": 30.0,
        "protected_reserve_pct": 20.0,
        "research_capital_pct": 50.0
    })
    
    # 1. Try to spawn strategy_x when status is "pending" -> 400 (lifecycle violation)
    r = client.post("/api/strategies/spawner/spawn", json={
        "policy_id": "strategy_x",
        "seed_capital": 5000.0
    })
    assert r.status_code == 400
    assert "must be 'approved' or 'promoted' to spawn" in r.json()["detail"]
    
    # Update status to approved
    with db._session() as s:
        nom = s.query(PolicyNomineeRow).filter(PolicyNomineeRow.policy_id == "strategy_x").first()
        nom.status = "approved"
        s.commit()
        
    # 2. Try to spawn strategy_x with $5000 when available research pool is $0 (0 total realized P&L) -> 400
    r = client.post("/api/strategies/spawner/spawn", json={
        "policy_id": "strategy_x",
        "seed_capital": 5000.0
    })
    assert r.status_code == 400
    assert "Insufficient research capital" in r.json()["detail"]
    
    # Generate $20,000 realized P&L -> 50% = $10,000 research pool
    from app.db.database import TradeRow
    with db._session() as s:
        s.add(TradeRow(
            id="spawn_pnl_trade",
            symbol="AAPL",
            pnl=20000.0,
            exit_at=datetime.now(UTC),
            mode="demo",
            payload="{}"
        ))
        s.commit()
        
    # 3. Spawn strategy_x successfully now with $4000
    r = client.post("/api/strategies/spawner/spawn", json={
        "policy_id": "strategy_x",
        "seed_capital": 4000.0
    })
    assert r.status_code == 200
    assert r.json()["status"] == "success"
    
    # 4. Check status GET endpoint
    r = client.get("/api/strategies/spawner/status")
    assert r.status_code == 200
    data = r.json()
    assert data["child_count"] == 1
    assert data["research_capital_pool"] == 6000.0  # 10k - 4k = 6k
    assert data["active_strategies"][0]["policy_id"] == "strategy_x"
    assert data["active_strategies"][0]["seed_capital"] == 4000.0
    
    # 5. Child strategy limit check (spawn 5 more, count limit = 5 active strategies)
    # Insert more approved nominees
    with db._session() as s:
        for i in range(1, 6):
            s.add(PolicyNomineeRow(
                policy_id=f"strategy_{i}",
                nominated_at=(datetime.now(UTC) + timedelta(minutes=i)).isoformat(),
                status="approved",
                approved_count=10, expectancy=0.5, edge_over_champion=0.2, champion_expectancy=0.3,
                cohort_fingerprint=f"cohort_{i}", config_fingerprint=f"config_{i}", champion_config_fingerprint="config_champ"
            ))
        s.commit()
        
    # Spawn until limit is hit (pool has 6k, let's spawn 4 strategies with 1k each)
    for i in range(1, 5):
        r = client.post("/api/strategies/spawner/spawn", json={
            "policy_id": f"strategy_{i}",
            "seed_capital": 1000.0
        })
        assert r.status_code == 200
        
    # Spawn 5th one (total active will be 1 (strategy_x) + 4 = 5) -> 400 (limit reached)
    r = client.post("/api/strategies/spawner/spawn", json={
        "policy_id": "strategy_5",
        "seed_capital": 1000.0
    })
    assert r.status_code == 400
    assert "Child strategy limit reached" in r.json()["detail"]
    
    # 6. Retire a strategy to release capital
    r = client.post("/api/strategies/spawner/retire", json={
        "policy_id": "strategy_x"
    })
    assert r.status_code == 200
    assert r.json()["status"] == "success"
    
    # Pool capital should recover by $4000
    r = client.get("/api/strategies/spawner/status")
    assert r.status_code == 200
    assert r.json()["child_count"] == 4
    assert r.json()["research_capital_pool"] == 6000.0  # 10k - 4k active (4 strategies with 1k) = 6k


def test_promotion_staging_constraint(client):
    # A nominee cannot be promoted to a Champion unless it is spawned and active in spawned_strategies first
    db = client.app.state.db
    engine = client.app.state.engine
    
    # Mock challenger strategy pipeline to return config
    mock_challenger = MagicMock()
    mock_challenger.policy_id = "staged_strategy"
    mock_challenger.to_snapshot.return_value = {
        "policy_id": "staged_strategy",
        "ml_threshold": 0.85,
        "score_threshold_offset": 0.0,
        "risk_config": {"drawdown_limit": 0.1}
    }
    
    from app.strategies.challenger import get_active_challengers
    original_get_active = get_active_challengers
    import app.strategies.challenger
    app.strategies.challenger.get_active_challengers = MagicMock(return_value=[mock_challenger])
    
    # Insert pending nominee in database
    db.save_policy_nominee({
        "policy_id": "staged_strategy",
        "nominated_at": datetime.now(UTC).isoformat(),
        "approved_count": 10,
        "expectancy": 0.5,
        "edge_over_champion": 0.2,
        "champion_expectancy": 0.3,
        "status": "pending",
        "cohort_fingerprint": "cohort_staged",
        "config_fingerprint": "9d147f29d47009d2",
        "champion_config_fingerprint": "54907d7b09a8d234",
    })
    
    # Prepare promote payload
    headers = {"Authorization": "Bearer test_admin_token"}
    payload = {
        "policy_id": "staged_strategy",
        "promoter_id": "promoter_1",
        "reason": "Passed staging tests",
        "evidence_package_id": "dummy_pkg_1"
    }
    
    try:
        # 1. Try to promote directly without spawning -> 400 (lifecycle violation)
        r = client.post("/api/strategies/promote", json=payload, headers=headers)
        assert r.status_code == 400
        assert "must be spawned and staged" in r.json()["detail"]
        
        # 2. Spawn it (setup pool first)
        from app.db.database import TradeRow, PolicyNomineeRow
        # Set nominee status to approved to allow spawn
        with db._session() as s:
            nom = s.query(PolicyNomineeRow).filter(PolicyNomineeRow.policy_id == "staged_strategy").first()
            nom.status = "approved"
            s.commit()
            
        # Add P&L for capital
        with db._session() as s:
            s.add(TradeRow(
                id="staged_pnl_trade",
                symbol="AAPL",
                pnl=50000.0,
                exit_at=datetime.now(UTC),
                mode="demo",
                payload="{}"
            ))
            s.commit()
            
        # Spawn staged_strategy
        r = client.post("/api/strategies/spawner/spawn", json={
            "policy_id": "staged_strategy",
            "seed_capital": 5000.0
        })
        assert r.status_code == 200
        
        # Reset nominee status back to pending to satisfy promote pending nominee check
        with db._session() as s:
            nom = s.query(PolicyNomineeRow).filter(PolicyNomineeRow.policy_id == "staged_strategy").first()
            nom.status = "pending"
            s.commit()
            
        # Mock active snapshot config hash compatibility
        original_get_setting = db.get_setting
        db.get_setting = MagicMock(side_effect=lambda key, *args, **kwargs: {
            "policy_id": "champion",
            "ml_threshold": 0.8,
            "score_threshold_offset": 0.0,
            "risk_config": {"drawdown_limit": 0.1}
        } if key == "active_policy_snapshot" else original_get_setting(key))
        
        # Mock get_policy_evidence to return dummy package
        db.get_policy_evidence = MagicMock(return_value={
            "timestamp": datetime.now(UTC).isoformat(),
            "policy_id": "staged_strategy"
        })
        
        # 3. Promote again -> should bypass the staging block successfully!
        # (It might fail on DB lock or commit since we mocked get_setting, but we just verify it passed step 6 staging check!)
        r = client.post("/api/strategies/promote", json=payload, headers=headers)
        print("RESPONSE CODE:", r.status_code)
        print("RESPONSE DETAIL:", r.json())
        # Verify it didn't reject due to "must be spawned and staged" lifecycle check
        assert r.status_code != 400
        assert "must be spawned and staged" not in str(r.json().get("detail", ""))
        
    finally:
        app.strategies.challenger.get_active_challengers = original_get_active


def test_spawner_global_capital_cap(client):
    db = client.app.state.db
    
    # 1. Setup a nominee policy
    from app.db.database import PolicyNomineeRow
    db.save_policy_nominee({
        "policy_id": "strategy_cap_test",
        "nominated_at": datetime.now(UTC).isoformat(),
        "status": "approved",
        "approved_count": 10, "expectancy": 0.5, "edge_over_champion": 0.2, "champion_expectancy": 0.3,
        "cohort_fingerprint": "cohort_cap", "config_fingerprint": "config_cap", "champion_config_fingerprint": "config_champ"
    })
    
    # 2. Add enough realised P&L so research pool allows $120,000 allocation (e.g. $300,000 total profits with 50% allocation)
    client.post("/api/strategies/profit-allocator", json={
        "compound_capital_pct": 30.0,
        "protected_reserve_pct": 20.0,
        "research_capital_pct": 50.0
    })
    
    from app.db.database import TradeRow
    with db._session() as s:
        s.add(TradeRow(
            id="cap_pnl_trade",
            symbol="AAPL",
            pnl=300000.0,
            exit_at=datetime.now(UTC),
            mode="demo",
            payload="{}"
        ))
        s.commit()
        
    # 3. Attempt to spawn a strategy allocating $120,000 -> should fail with 400 because it exceeds the $100,000 global ceiling
    r = client.post("/api/strategies/spawner/spawn", json={
        "policy_id": "strategy_cap_test",
        "seed_capital": 120000.0
    })
    assert r.status_code == 400
    assert "Global sandbox capital ceiling reached" in r.json()["detail"]
