import pytest
import asyncio
from datetime import datetime, UTC
import pandas as pd
from typing import Any
from app.core.engine import TradingEngine, EngineState
from app.core.domain import Signal, SignalAction, OrderStatus, OrderSide, OrderType, OrderRole
from app.strategies.momentum import MomentumCross
from app.strategies.pipeline import SignalPipeline

@pytest.mark.asyncio
async def test_signal_acceptance_callback():
    class MockBroker:
        name = "mock"
        def __init__(self):
            self.orders = []
            self.is_paper = True
            self.diagnostics = {"connected": True}

        def drain_fills(self):
            return []

        async def open_orders(self):
            return []

        async def account(self):
            class MockAccount:
                cash = 100000
                equity = 100000
                day_trades = 0
                buying_power = 100000
            return MockAccount()

        async def positions(self):
            return []

        async def get_diagnostics(self):
            return self.diagnostics

        async def submit(self, req: Any):
            from app.core.domain import Order
            import uuid
            o = Order(
                id=str(uuid.uuid4()),
                client_order_id=req.client_order_id,
                symbol=req.symbol,
                side=req.side,
                qty=req.qty,
                order_type=req.order_type,
                limit_price=req.limit_price,
                status=OrderStatus.SUBMITTED,
                reason="mock accepted",
                role=req.role
            )
            self.orders.append(o)
            return o

        def status(self):
            return {}

    class MockRisk:
        class MockConfig:
            def model_dump(self):
                return {"max_risk": 100}
        def __init__(self):
            self.config = self.MockConfig()
            self.daily_halted = False
            self.hard_halted = False

        def is_entry_permitted(self, sig, now=None):
            class MockPreCheck:
                approved = True
                reason = ""
            return MockPreCheck()

        def evaluate_entry(self, sig: Signal, ctx: Any, df: Any, now: Any = None, kelly_override: Any = None):
            class MockDecision:
                approved = True
                reason = ""
                class MockOrder:
                    client_order_id = "test"
                    symbol = sig.symbol
                    side = OrderSide.BUY if sig.action == SignalAction.ENTER_LONG else OrderSide.SELL
                    qty = 1.0
                    order_type = OrderType.MARKET
                    role = OrderRole.ENTRY
                    limit_price = 100.0
                order = MockOrder()
            return MockDecision()

        def check_halts(self, ctx, now):
            return None
            
        def should_flatten(self, now):
            return False

        def roll_session(self, session):
            pass

        def get_risk_state(self):
            return {}

        def status(self):
            return {}

    class MockSettings:
        mode = "paper"
        broker = "mock"
        shadow_broker = None
        redis_url = "redis://localhost:6379"
        api_key = "test"
        api_secret = "test"
        account_id = "test"
        gemini_api_key = "test"
        anthropic_api_key = "test"
        enable_live_trading = False
        has_alpaca_keys = True
        
        def resolve_data_provider(self):
            return "mock"
        
        def watchlist_symbols(self):
            return ["AAPL"]
            
        def risk_config(self):
            class M:
                def model_dump(self): return {}
            return M()
            
        def trade_score_weights(self):
            return {"entry_quality": 1.0}
            
    class MockDB:
        def save_order(self, *args, **kwargs): pass
        def save_equity(self, *args, **kwargs): pass
        def set_setting(self, *args, **kwargs): pass
        def append_insight(self, *args, **kwargs): pass
        def get_setting(self, *args, **kwargs): return None
        def get_all_positions(self, *args, **kwargs): return []
        def get_latest_position_for_symbol(self, *args, **kwargs): return None
        def add_trade(self, *args, **kwargs): pass
        def get_replay_queue_telemetry(self, *args, **kwargs): return {}
        
    engine = TradingEngine(
        settings=MockSettings(),
        db=MockDB()
    )
    engine.broker = MockBroker()
    engine.risk = MockRisk()
    
    class MockInsights:
        def on_signal(self, sig, gated):
            class M:
                def model_dump(self, mode): return {}
            return M()
        def on_performance(self, stats, day_pnl):
            pass
    engine.insights = MockInsights()
    engine.pending_reconciliation_orders = {}
    engine._shadow_decisions = {}
    class MockModel:
        def info(self): return {}
        def __init__(self):
            self._meta_label_engine = None
    
    class MockTrainer:
        status = {}
    engine.trainer = MockTrainer()
    engine.model = MockModel()
    
    pipeline = SignalPipeline(
        model=MockModel(),
        settings=MockSettings(),
        risk_config=engine.risk.config
    )
    momentum = MomentumCross()
    engine.pipeline = pipeline
    engine.strategies = [momentum]
    
    class MockCtx:
        debug_mode = False
        
    momentum._set_event(MockCtx(), "AAPL", "long", datetime.now(UTC))
    assert momentum._sym_events("AAPL")["long"]["emitted"] is False
    
    sig = Signal(
        symbol="AAPL",
        action=SignalAction.ENTER_LONG,
        price=150.0,
        strategy="momentum",
        candidate_id="cand_1",
        thesis_fingerprint="finger_1",
        confidence=0.8,
        reason="test"
    )
    df = pd.DataFrame(
        {"open": [150.0], "high": [150.0], "low": [150.0], "close": [150.0], "volume": [1000], "vwap": [150.0], "atr14": [1.0]},
        index=pd.DatetimeIndex([datetime.now(UTC)])
    )
    
    async def mock_evaluate_symbol(*args, **kwargs):
        return [(sig, df)]
    engine._evaluate_symbol = mock_evaluate_symbol

    async def mock_check_behavioral_safety(*args, **kwargs):
        return None
    engine._check_behavioral_safety = mock_check_behavioral_safety

    async def mock_check_system_heartbeat(*args, **kwargs):
        class MockSafetyDecision:
            allow_new_entries = True
            market_state = "NORMAL"
            risk_multiplier = 1.0
        engine.last_safety_decision = MockSafetyDecision()
        engine.allow_new_entries = True
        engine.system_health = "NORMAL"
    engine._check_system_heartbeat = mock_check_system_heartbeat

    def mock_run_heartbeat_tasks(*args, **kwargs):
        pass
    engine._run_heartbeat_tasks = mock_run_heartbeat_tasks
    
    class MockAccount:
        cash = 100000
        equity = 100000
        day_trades = 0
        buying_power = 100000
    async def mock_get_account():
        return MockAccount()
    engine.broker.get_account = mock_get_account
    
    class MockClock:
        def is_open(self, now): return True
        def next_close(self, now): return datetime.now(UTC)
        def session_date(self, now): return now.date()
        def status(self): return {}
    engine.clock = MockClock()
    
    engine.user_watchlist = ["AAPL"]
    engine.watchlist = ["AAPL"]
    
    class MockData:
        class MockProvider:
            name = "mock"
        provider = MockProvider()
        def frame(self, symbol, n=None, **kwargs): return df
        async def poll(self, symbols): return {"AAPL": df}
    class MockPortfolio:
        _lots = {}
        positions = []
        equity = 100000
        session_start_equity = 100000
        peak_equity = 100000
        day_trades_today = 0
        def roll_session(self, session, equity): return False
        def day_pnl(self, equity): return 0.0
        def day_trades_in_window(self, session): return 0
        def get_all_positions(self): return []
        def mark_equity(self, equity, now): pass
        def drawdown_pct(self, equity): return 0.0
        def stats(self): return {}
        def checkpoint(self, **kwargs): return {}
        is_tradable = lambda self, s: True
    engine.data = MockData()
    engine.portfolio = MockPortfolio()
    engine.broker.is_paper = True
    engine.state = EngineState.RUNNING

    class MockSafetyDecision:
        allow_new_entries = True
        market_state = "NORMAL"
        risk_multiplier = 1.0
    engine.last_safety_decision = MockSafetyDecision()
    engine.allow_new_entries = True
    
    await engine._tick()

    print(f"DEBUG eval_results: getattr engine '_last_evaluated_symbol_count': {engine._last_evaluated_symbol_count}")
    print(f"DEBUG engine.broker.orders: {engine.broker.orders}")
    print(f"DEBUG engine.pending_reconciliation_orders: {engine.pending_reconciliation_orders}")
    assert len(engine.broker.orders) == 1
    assert momentum._sym_events("AAPL")["long"]["emitted"] is True
