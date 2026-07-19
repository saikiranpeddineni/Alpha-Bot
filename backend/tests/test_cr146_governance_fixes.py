import pytest
import asyncio
from datetime import datetime, UTC, timedelta
from unittest.mock import MagicMock, AsyncMock, patch
import pandas as pd
from fastapi import HTTPException
from fastapi.testclient import TestClient

from pathlib import Path
from app.db.database import Database, PolicyNomineeRow
from app.core.domain import Signal, SignalAction, Order, OrderSide, OrderStatus, OrderType, utcnow
from app.core.engine import TradingEngine, EngineState
from app.core.shadow_ledger import ShadowLedger
from app.api.routes import router, PromoteRequest
from app.strategies.reflection import generate_eod_reflection_report
from app.strategies.challenger import ChallengerConfig


@pytest.fixture
def temp_db():
    db = Database(path=Path(utcnow().strftime("test_db_%Y%m%d_%H%M%S_%f.db")))
    # Run self-healing columns migration manually in case schema is loaded differently in test memory
    with db.engine.connect() as conn:
        for col in ("cohort_fingerprint", "config_fingerprint", "champion_config_fingerprint"):
            try:
                conn.execute(f"ALTER TABLE policy_nominees ADD COLUMN {col} VARCHAR(64)")
            except Exception:
                pass
    yield db
    db.close()
    import os
    try:
        os.remove(db.engine.url.database)
    except Exception:
        pass


def _make_bars(n=10):
    dates = [datetime(2026, 1, 6, 9, 30) + timedelta(minutes=i) for i in range(n)]
    return pd.DataFrame({
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [100.0] * n,
        "volume": [1000] * n,
        "symbol": ["AAPL"] * n,
    }, index=pd.to_datetime(dates, utc=True))


# ==============================================================================
# Fix #156: Nominations Deduplication & Lifecycle
# ==============================================================================
def test_nomination_deduplication_and_lifecycle(temp_db):
    # Save nomination first time
    temp_db.save_policy_nominee({
        "policy_id": "challenger_low_ml",
        "nominated_at": "2026-01-06T12:00:00",
        "approved_count": 15,
        "expectancy": 0.8,
        "edge_over_champion": 0.2,
        "champion_expectancy": 0.6,
        "status": "pending",
        "cohort_fingerprint": "cohort_123",
        "config_fingerprint": "config_abc",
        "champion_config_fingerprint": "champ_xyz",
    })

    nominees = temp_db.get_pending_nominees()
    assert len(nominees) == 1
    assert nominees[0]["policy_id"] == "challenger_low_ml"
    assert nominees[0]["config_fingerprint"] == "config_abc"
    assert nominees[0]["status"] == "pending"

    # Save nomination again with IDENTICAL config_fingerprint -> should update in-place
    temp_db.save_policy_nominee({
        "policy_id": "challenger_low_ml",
        "nominated_at": "2026-01-06T13:00:00",
        "approved_count": 16,
        "expectancy": 0.85,
        "edge_over_champion": 0.25,
        "champion_expectancy": 0.6,
        "status": "pending",
        "cohort_fingerprint": "cohort_123",
        "config_fingerprint": "config_abc",
        "champion_config_fingerprint": "champ_xyz",
    })

    nominees = temp_db.get_pending_nominees()
    assert len(nominees) == 1  # Still exactly one pending nomination
    assert nominees[0]["approved_count"] == 16
    assert nominees[0]["nominated_at"] == "2026-01-06T13:00:00"

    # Save nomination with DIFFERENT config_fingerprint -> should dismiss old one and insert new
    temp_db.save_policy_nominee({
        "policy_id": "challenger_low_ml",
        "nominated_at": "2026-01-06T14:00:00",
        "approved_count": 20,
        "expectancy": 0.9,
        "edge_over_champion": 0.3,
        "champion_expectancy": 0.6,
        "status": "pending",
        "cohort_fingerprint": "cohort_123",
        "config_fingerprint": "config_diff",
        "champion_config_fingerprint": "champ_xyz",
    })

    # Get pending nominees
    nominees = temp_db.get_pending_nominees()
    assert len(nominees) == 1
    assert nominees[0]["config_fingerprint"] == "config_diff"

    # Verify the old one was marked 'dismissed' in the DB directly
    from sqlalchemy import select
    with temp_db._session() as s:
        rows = s.scalars(select(PolicyNomineeRow).where(PolicyNomineeRow.policy_id == "challenger_low_ml")).all()
        assert len(rows) == 2
        dismissed_count = sum(1 for r in rows if r.status == "dismissed")
        pending_count = sum(1 for r in rows if r.status == "pending")
        assert dismissed_count == 1
        assert pending_count == 1


# ==============================================================================
# Fix #160: Matched Cohort Expectancy Comparison
# ==============================================================================
def test_matched_cohort_expectancy_comparison(temp_db):
    df = _make_bars()
    # Record shadow signals for champion
    ShadowLedger.record_candidate(
        db_engine=temp_db, symbol="AAPL", ts=datetime(2026, 1, 6, 10, 0, tzinfo=UTC),
        strategy="orb", direction="long", signal_price=100.0, stop_loss=99.0, take_profit=103.0,
        ml_confidence=0.6, final_decision="pipeline_approved", rejection_stage="none", rejection_reason="",
        df=df, spy_df=df, slot_rvol_val=1.0, vwap_telemetry={}, policy_id="champion", candidate_id="C1",
    )
    # Set outcome for C1 (+1.0R P&L)
    r1 = temp_db.get_shadow_signal("C1", "champion")
    r1["outcomes"] = {"60m": {"realized_r": 1.0}}
    temp_db.save_shadow_signal("C1", datetime(2026, 1, 6, 10, 0, tzinfo=UTC), "AAPL", "orb", "long", "backtest", r1)

    # Record shadow signals for challenger_low_ml on C1 (+1.5R) and C2 (+2.0R, but unmatched)
    ShadowLedger.record_candidate(
        db_engine=temp_db, symbol="AAPL", ts=datetime(2026, 1, 6, 10, 0, tzinfo=UTC),
        strategy="orb", direction="long", signal_price=100.0, stop_loss=99.0, take_profit=103.0,
        ml_confidence=0.6, final_decision="pipeline_approved", rejection_stage="none", rejection_reason="",
        df=df, spy_df=df, slot_rvol_val=1.0, vwap_telemetry={}, policy_id="challenger_low_ml", candidate_id="C1",
    )
    r_c1 = temp_db.get_shadow_signal("C1", "challenger_low_ml")
    r_c1["outcomes"] = {"60m": {"realized_r": 1.5}}
    temp_db.save_shadow_signal("C1", datetime(2026, 1, 6, 10, 0, tzinfo=UTC), "AAPL", "orb", "long", "backtest", r_c1)

    ShadowLedger.record_candidate(
        db_engine=temp_db, symbol="AAPL", ts=datetime(2026, 1, 6, 10, 1, tzinfo=UTC),
        strategy="orb", direction="long", signal_price=100.0, stop_loss=99.0, take_profit=103.0,
        ml_confidence=0.6, final_decision="pipeline_approved", rejection_stage="none", rejection_reason="",
        df=df, spy_df=df, slot_rvol_val=1.0, vwap_telemetry={}, policy_id="challenger_low_ml", candidate_id="C2",
    )
    r_c2 = temp_db.get_shadow_signal("C2", "challenger_low_ml")
    r_c2["outcomes"] = {"60m": {"realized_r": 2.0}}
    temp_db.save_shadow_signal("C2", datetime(2026, 1, 6, 10, 1, tzinfo=UTC), "AAPL", "orb", "long", "backtest", r_c2)

    # Generate reflection report
    report = generate_eod_reflection_report(temp_db, now=datetime(2026, 1, 6, 16, 0, tzinfo=UTC))
    
    # Since only C1 is matched between Challenger and Champion:
    # Challenger expectancy should be compared ONLY on C1 (expectancy = 1.5R) vs Champion on C1 (expectancy = 1.0R).
    # The edge should be exactly +0.5000R, and matched champ expectancy should be +1.0000R.
    assert "1.5000R" in report
    assert "+1.0000R" in report
    assert "+0.5000R" in report


# ==============================================================================
# Fix #152 & #158: Authentication & Input Schema Validation
# ==============================================================================
@pytest.mark.asyncio
async def test_api_authentication_and_input_validation(temp_db):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    
    # Mock settings with an admin token
    settings_mock = MagicMock()
    settings_mock.admin_token = "secure_auth_token"
    
    with patch("app.config.get_settings", return_value=settings_mock), \
         patch("app.api.routes._db", return_value=temp_db):
        
        client = TestClient(app)

        # 1. No Authorization Header -> 401
        res = client.post("/api/strategies/promote", json={"policy_id": "challenger_low_ml"})
        assert res.status_code == 401
        assert "token required" in res.json()["detail"].lower()

        # 2. Invalid Token -> 403
        res = client.post(
            "/api/strategies/promote", 
            json={"policy_id": "challenger_low_ml"},
            headers={"Authorization": "Bearer bad_token"}
        )
        assert res.status_code == 403
        assert "invalid authentication token" in res.json()["detail"].lower()

        # 3. Invalid Schema (empty policy_id) -> 400
        res = client.post(
            "/api/strategies/promote", 
            json={"policy_id": "  "},
            headers={"Authorization": "Bearer secure_auth_token"}
        )
        assert res.status_code == 400
        assert "invalid policy_id" in res.json()["detail"].lower()


# ==============================================================================
# Fix #158 & #159: Idempotency & CAS Concurrency Veto
# ==============================================================================
@pytest.mark.asyncio
async def test_promotion_idempotency_and_concurrency_cas(temp_db):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)

    settings_mock = MagicMock()
    settings_mock.admin_token = ""  # no auth needed
    settings_mock.ml_confidence_threshold = 0.55
    settings_mock.risk_config().model_dump.return_value = {}

    # Setup database with current active snapshot
    active_snapshot = {
        "policy_id": "challenger_low_ml",
        "ml_threshold": 0.48,
        "score_threshold_offset": 0.0,
    }
    temp_db.set_setting("active_policy_snapshot", active_snapshot)

    # 1. Idempotency Check
    # Promoting 'challenger_low_ml' again should return success directly without checking nominations or lock content
    with patch("app.config.get_settings", return_value=settings_mock), \
         patch("app.api.routes._db", return_value=temp_db), \
         patch("app.api.routes._engine", return_value=MagicMock()):
        client = TestClient(app)
        res = client.post("/api/strategies/promote", json={"policy_id": "challenger_low_ml"})
        assert res.status_code == 200
        assert "successfully" in res.json()["message"].lower()

    # 2. CAS Conflict Check
    c_config = ChallengerConfig(policy_id="challenger_high_ml", ml_threshold=0.56)
    chall_snapshot = c_config.to_snapshot()
    import hashlib
    import json
    cleaned_chall = {k: v for k, v in chall_snapshot.items() if k not in ("snapshot_at", "snapshot_version")}
    config_fp = hashlib.sha256(json.dumps(cleaned_chall, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]

    temp_db.save_policy_nominee({
        "policy_id": "challenger_high_ml",
        "nominated_at": "2026-01-06T12:00:00",
        "approved_count": 15,
        "expectancy": 0.8,
        "edge_over_champion": 0.2,
        "champion_expectancy": 0.6,
        "status": "pending",
        "cohort_fingerprint": "cohort_123",
        "config_fingerprint": config_fp,
        "champion_config_fingerprint": "stale_fingerprint", # does not match current champion
    })

    with patch("app.config.get_settings", return_value=settings_mock), \
         patch("app.api.routes._db", return_value=temp_db), \
         patch("app.api.routes._engine", return_value=MagicMock()):
        client = TestClient(app)
        res = client.post("/api/strategies/promote", json={"policy_id": "challenger_high_ml"})
        assert res.status_code == 409
        assert "conflict: champion configuration has changed" in res.json()["detail"].lower()


# ==============================================================================
# Fix #151: Pending Nomination Verification
# ==============================================================================
@pytest.mark.asyncio
async def test_pending_nomination_verification(temp_db):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)

    settings_mock = MagicMock()
    settings_mock.admin_token = ""

    # Try promoting challenger_high_ml with NO pending nomination -> 400
    with patch("app.config.get_settings", return_value=settings_mock), \
         patch("app.api.routes._db", return_value=temp_db), \
         patch("app.api.routes._engine", return_value=MagicMock()):
        client = TestClient(app)
        res = client.post("/api/strategies/promote", json={"policy_id": "challenger_high_ml"})
        assert res.status_code == 400
        assert "no pending nomination found" in res.json()["detail"].lower()


# ==============================================================================
# Fix #150: Atomic Transaction & Rollback
# ==============================================================================
@pytest.mark.asyncio
async def test_atomic_promotion_rollback_on_failure(temp_db):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)

    settings_mock = MagicMock()
    settings_mock.admin_token = ""
    settings_mock.ml_confidence_threshold = 0.55
    settings_mock.risk_config().model_dump.return_value = {}

    # Setup pending nomination
    # Calculate config fingerprint for challenger_high_ml
    c_config = ChallengerConfig(policy_id="challenger_high_ml", ml_threshold=0.56)
    import hashlib
    import json
    chall_snapshot = c_config.to_snapshot()
    cleaned_chall = {k: v for k, v in chall_snapshot.items() if k not in ("snapshot_at", "snapshot_version")}
    config_fp = hashlib.sha256(json.dumps(cleaned_chall, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]

    # Current Champion Fingerprint
    champ_snapshot = {
        "policy_id": "champion",
        "ml_threshold": 0.55,
        "score_threshold_offset": 0.0,
        "risk_config": {}
    }
    cleaned_champ = {k: v for k, v in champ_snapshot.items() if k not in ("snapshot_at", "snapshot_version")}
    champ_fp = hashlib.sha256(json.dumps(cleaned_champ, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]

    temp_db.save_policy_nominee({
        "policy_id": "challenger_high_ml",
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

    # Mock engine
    engine_mock = MagicMock()
    engine_mock.settings = settings_mock
    engine_mock.risk_config = MagicMock()
    engine_mock.pipeline.ml_threshold = 0.55
    engine_mock.evidence_evaluator.ml_threshold = 0.55

    # Mock db.set_setting to fail when writing 'ml_threshold' (to inject a mutation failure)
    original_set_setting = temp_db.set_setting
    def mock_set_setting(key, value):
        if key == "ml_threshold":
            raise RuntimeError("Database write failure!")
        original_set_setting(key, value)
    
    temp_db.set_setting = mock_set_setting

    with patch("app.config.get_settings", return_value=settings_mock), \
         patch("app.api.routes._db", return_value=temp_db), \
         patch("app.api.routes._engine", return_value=engine_mock):
        client = TestClient(app, raise_server_exceptions=False)
        res = client.post("/api/strategies/promote", json={"policy_id": "challenger_high_ml"})
        if res.status_code != 500:
            print("FAILED ROLLBACK RESPONSE:", res.status_code, res.json())
        assert res.status_code == 500
        assert "internal server error" in res.text.lower()

        # Assert DB settings rolled back (ml_threshold should still be None or pre-promotion state)
        temp_db.set_setting = original_set_setting  # restore original set_setting helper
        assert temp_db.get_setting("active_policy_snapshot") is None
        assert temp_db.get_setting("ml_threshold") is None

        # Assert engine in-memory properties rolled back
        assert engine_mock.pipeline.ml_threshold == 0.55
        assert engine_mock.evidence_evaluator.ml_threshold == 0.55


# ==============================================================================
# Fix #161: Same-bar TP+SL Ambiguity
# ==============================================================================
def test_same_bar_tp_sl_ambiguity():
    # Setup data where bar hits both stop loss and take profit
    df = pd.DataFrame({
        "open": [100.0],
        "high": [105.0],  # hits take profit (103.0)
        "low": [95.0],   # hits stop loss (99.0)
        "close": [100.0],
        "volume": [1000],
        "symbol": ["AAPL"],
    }, index=pd.to_datetime([datetime(2026, 1, 6, 10, 0)], utc=True))

    res = ShadowLedger.replay_signal_60m(
        df=df,
        entry_idx=0,
        direction="long",
        stop_loss=99.0,
        take_profit=103.0,
        entry_price=100.0,
    )

    # Output should conservatively assume stop-loss exit, but exit_reason must be marked 'ambiguous'
    assert res["exit_reason"] == "ambiguous"
    assert res["sl_hit"] is True
    assert res["exit_price"] == 99.0  # Conservative stop loss execution
    assert res["pnl_pct"] == -1.0     # P&L calculation reflect stop loss


# ==============================================================================
# Fix #164: Broker rejections persisted correctly in Shadow Ledger
# ==============================================================================
@pytest.mark.asyncio
async def test_broker_rejection_updates_shadow_decision(temp_db):
    settings = MagicMock()
    settings.mode = "demo"
    settings.watchlist_symbols.return_value = ["AAPL"]
    settings.risk_config().model_dump.return_value = {}

    engine = TradingEngine(settings, temp_db)
    engine.state = EngineState.RUNNING

    # Mock broker calls
    engine.broker = AsyncMock()
    engine.broker.is_paper = True
    
    rejected_order = Order(
        id="ORD123",
        client_order_id="CLIENT123",
        symbol="AAPL",
        side=OrderSide.BUY,
        qty=100,
        order_type=OrderType.LIMIT,
        limit_price=100.0,
        status=OrderStatus.REJECTED,
        reason="Buying power exceeded"
    )
    engine.broker.submit.return_value = rejected_order
    engine.broker.account.return_value = MagicMock(cash=10000.0, equity=10000.0)
    engine.broker.positions.return_value = []
    engine.broker.open_orders.return_value = []
    engine.broker.drain_fills = MagicMock(return_value=[])

    # Setup pipeline and risk manager
    engine.pipeline = MagicMock()
    engine.risk = MagicMock()
    engine.risk.should_flatten.return_value = False
    engine.risk.daily_halted = False
    engine.risk.hard_halted = False
    engine.risk.check_halts.return_value = None

    engine.portfolio = MagicMock()
    engine.portfolio.day_pnl.return_value = 0.0
    engine.portfolio.day_trades_in_window.return_value = 0
    
    # Mock data poll
    df = _make_bars(n=100)
    engine.data = MagicMock()
    engine.data.poll = AsyncMock()
    engine.data.poll.return_value = {"AAPL": df}
    engine.data.frame.return_value = df

    sig = Signal(
        symbol="AAPL", action=SignalAction.ENTER_LONG, strategy="orb", price=100.0, confidence=0.6, reason=""
    )
    sig.candidate_id = "CAND_REJECT_SL"
    
    # Mock strategies loop
    strat_mock = MagicMock()
    strat_mock.name = "orb"
    strat_mock.enabled = True
    strat_mock.params = {}
    strat_mock.on_bar.return_value = sig
    engine.strategies = [strat_mock]

    # Evaluate symbol -> approved by risk, goes to submission
    engine.risk.evaluate_entry.return_value = MagicMock(approved=True, order=rejected_order)
    engine.pipeline.process.return_value = ([sig], [])

    # Evaluate using the full tick cycle
    await engine._tick()

    # Retrieve shadow signal from DB -> should be marked final_decision="rejected" with stage="broker_rejected"
    shadow = temp_db.get_shadow_signal("CAND_REJECT_SL", "champion")
    assert shadow is not None
    assert shadow["final_decision"] == "rejected"
    assert shadow["rejection_stage"] == "broker_rejected"
    assert shadow["rejection_reason"] == "Buying power exceeded"
