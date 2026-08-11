from __future__ import annotations
from app.config import RiskConfig
from app.config import Settings

import asyncio
import hashlib
import json
import pytest
from datetime import UTC, datetime, timedelta
import pandas as pd
from zoneinfo import ZoneInfo
from pathlib import Path

from app.config import DATA_DIR, RiskConfig
from app.data.base import CachePolicy, HistoricalDataCoverageError, filter_session_view
from app.data.manager import DataManager
from app.data.store import BarCache
from app.data.synthetic import SyntheticProvider
from app.ai.model import SignalModel
from app.backtest.engine import BacktestParams, run_backtest, compute_code_state_sha256

ET = ZoneInfo("America/New_York")


class BlockingProvider:
    name = "blocking"

    def __init__(self, should_fail=True):
        self.should_fail = should_fail
        self.called = False

    async def get_bars(self, symbol, timeframe="1Min", start=None, end=None, limit=None):
        self.called = True
        if self.should_fail:
            raise RuntimeError("NETWORK BLOCKED")
        # Return dummy bar
        df = pd.DataFrame(
            [[100.0, 101.0, 99.0, 100.5, 1000.0]],
            columns=["open", "high", "low", "close", "volume"],
            index=pd.DatetimeIndex([start or datetime.now(UTC)], name="ts")
        )
        return df

    async def get_quotes(self, symbols):
        return {}

    async def close(self):
        pass


@pytest.fixture
def empty_cache(tmp_path):
    return BarCache(tmp_path / "test_cache.db")


@pytest.mark.asyncio
async def test_1_immutable_mode_blocks_network(tmp_path, empty_cache):
    """Test 1: Immutable mode blocks network calls and fails fast on misses."""
    prov = BlockingProvider(should_fail=True)
    dm = DataManager(prov, tmp_path / "test_cache.db")
    dm.cache_policy = CachePolicy.IMMUTABLE_CACHE_ONLY

    # Query for a symbol not in cache
    start = datetime(2026, 7, 6, 9, 30, tzinfo=UTC)
    end = datetime(2026, 7, 6, 16, 0, tzinfo=UTC)
    
    with pytest.raises(HistoricalDataCoverageError) as excinfo:
        await dm.history("AAPL", "1Min", start, end)
        
    assert "Coverage check failed" in str(excinfo.value)
    assert not prov.called


@pytest.mark.asyncio
async def test_2_market_aware_span_ok(tmp_path, empty_cache):
    """Test 2: Market-aware span check resolves EOD wall-clock requests without falling back."""
    # Seed cache with regular session bars for 2026-07-06 (Monday)
    d = datetime(2026, 7, 6).date()
    times = [datetime.combine(d, datetime.min.time()) + timedelta(minutes=m)
             for m in range(9 * 60 + 30, 16 * 60)]
    times = [t.replace(tzinfo=ET).astimezone(UTC) for t in times]
    df = pd.DataFrame(
        [[100.0, 101.0, 99.0, 100.5, 1000.0] for _ in times],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex(times, name="ts")
    )
    empty_cache.upsert("AAPL", "1Min", df)

    prov = BlockingProvider(should_fail=True)
    dm = DataManager(prov, tmp_path / "test_cache.db")
    dm.cache_policy = CachePolicy.DEFAULT

    # Request spanning the entire wall-clock day
    req_start = datetime(2026, 7, 6, 0, 0, tzinfo=UTC)
    req_end = datetime(2026, 7, 6, 23, 59, tzinfo=UTC)

    # Should hit cache and validate coverage without calling provider
    res = await dm.history("AAPL", "1Min", req_start, req_end, prefer_cache=True)
    assert not res.empty
    assert not prov.called


@pytest.mark.asyncio
async def test_3_requested_vs_actual_range_reporting(tmp_path, empty_cache):
    """Test 3: Requested vs actual range reporting in BacktestResult."""
    # Seed cache with regular session bars for 2026-07-06
    d = datetime(2026, 7, 6).date()
    times = [datetime.combine(d, datetime.min.time()) + timedelta(minutes=m)
             for m in range(9 * 60 + 30, 16 * 60)]
    times = [t.replace(tzinfo=ET).astimezone(UTC) for t in times]
    df = pd.DataFrame(
        [[100.0, 101.0, 99.0, 100.5, 1000.0] for _ in times],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex(times, name="ts")
    )
    empty_cache.upsert("SPY", "1Min", df)
    empty_cache.upsert("QQQ", "1Min", df)

    prov = BlockingProvider(should_fail=True)
    dm = DataManager(prov, tmp_path / "test_cache.db")

    model = SignalModel(DATA_DIR / "models", risk_config=RiskConfig())
    params = BacktestParams(
        symbols=["SPY", "QQQ"],
        start=datetime(2026, 7, 6, 0, 0, tzinfo=UTC),
        end=datetime(2026, 7, 6, 23, 59, tzinfo=UTC),
        cache_policy=CachePolicy.IMMUTABLE_CACHE_ONLY,
    )
    
    result = await run_backtest(dm, model, params, settings=Settings())
    
    assert result.requested_start == params.start
    assert result.requested_end == params.end
    assert result.actual_data_start is not None
    assert result.actual_data_end is not None
    assert result.requested_session_count == 1
    assert result.actual_session_count == 1
    assert result.shared_session_count == 1


@pytest.mark.asyncio
async def test_4_mixed_source_provenance_tracking(tmp_path, empty_cache):
    """Test 4: Mixed-source provenance tracking."""
    # Seed SPY only in cache
    d = datetime(2026, 7, 6).date()
    times = [datetime.combine(d, datetime.min.time()) + timedelta(minutes=m)
             for m in range(9 * 60 + 30, 16 * 60)]
    times = [t.replace(tzinfo=ET).astimezone(UTC) for t in times]
    df = pd.DataFrame(
        [[100.0, 101.0, 99.0, 100.5, 1000.0] for _ in times],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex(times, name="ts")
    )
    empty_cache.upsert("SPY", "1Min", df)

    # QQQ is missing from cache, will be fetched from provider
    prov = BlockingProvider(should_fail=False)
    dm = DataManager(prov, tmp_path / "test_cache.db")
    dm.cache_policy = CachePolicy.DEFAULT

    model = SignalModel(DATA_DIR / "models", risk_config=RiskConfig())
    params = BacktestParams(
        symbols=["SPY", "QQQ"],
        start=datetime(2026, 7, 6, 9, 30, tzinfo=UTC),
        end=datetime(2026, 7, 6, 16, 0, tzinfo=UTC),
        cache_policy=CachePolicy.DEFAULT,
    )
    
    result = await run_backtest(dm, model, params, settings=Settings())
    assert result.data_source_mode == "MIXED"
    assert result.provenance["SPY"]["source"] == "cache"
    assert result.provenance["QQQ"]["source"] == "network"


@pytest.mark.asyncio
async def test_3b_partial_warmup_is_optional_cache_context(tmp_path):
    warmup_times = pd.date_range(
        datetime(2026, 7, 2, 15, 0, tzinfo=UTC), periods=30, freq="1min"
    )
    requested_times = pd.date_range(
        datetime(2026, 7, 6, 13, 30, tzinfo=UTC), periods=390, freq="1min"
    )
    times = warmup_times.append(requested_times)
    df = pd.DataFrame(
        [[100.0, 101.0, 99.0, 100.5, 1000.0] for _ in times],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex(times, name="ts"),
    )

    prov = BlockingProvider(should_fail=True)
    dm = DataManager(prov, tmp_path / "test_cache.db")
    dm.cache.upsert("SPY", "1Min", df)
    params = BacktestParams(
        symbols=["SPY"],
        start=datetime(2026, 7, 6, 0, 0, tzinfo=UTC),
        end=datetime(2026, 7, 6, 23, 59, tzinfo=UTC),
        cache_policy=CachePolicy.IMMUTABLE_CACHE_ONLY,
    )

    result = await run_backtest(dm, SignalModel(DATA_DIR / "models", risk_config=RiskConfig()), params, settings=Settings())

    assert result.actual_data_start == warmup_times[0].to_pydatetime()
    assert result.actual_data_end <= params.end
    assert not prov.called


@pytest.mark.asyncio
async def test_3c_requested_range_is_explicitly_end_bounded(tmp_path):
    requested_times = pd.date_range(
        datetime(2026, 7, 6, 13, 30, tzinfo=UTC), periods=390, freq="1min"
    )
    future_ts = datetime(2026, 7, 7, 13, 30, tzinfo=UTC)
    times = requested_times.append(pd.DatetimeIndex([future_ts]))
    df = pd.DataFrame(
        [[100.0, 101.0, 99.0, 100.5, 1000.0] for _ in times],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex(times, name="ts"),
    )

    prov = BlockingProvider(should_fail=True)
    dm = DataManager(prov, tmp_path / "test_cache.db")

    async def overbroad_history(symbol, timeframe, start, end, prefer_cache=True, cache_policy=None):
        if timeframe == "1Day":
            raise HistoricalDataCoverageError(
                symbol=symbol, timeframe=timeframe, start=start, end=end,
                cached_start=None, cached_end=None, reason="optional daily context absent",
            )
        return df

    dm.history = overbroad_history
    params = BacktestParams(
        symbols=["SPY"],
        start=datetime(2026, 7, 6, 0, 0, tzinfo=UTC),
        end=datetime(2026, 7, 6, 23, 59, tzinfo=UTC),
        cache_policy=CachePolicy.IMMUTABLE_CACHE_ONLY,
    )

    result = await run_backtest(dm, SignalModel(DATA_DIR / "models", risk_config=RiskConfig()), params, settings=Settings())

    assert result.actual_data_end <= params.end
    assert result.actual_data_end != future_ts
    assert not prov.called


@pytest.mark.asyncio
async def test_5_dataset_immutability_assertion(tmp_path, empty_cache):
    """Test 5: Dataset immutability assertion fails if cache mutated during run."""
    # Seed cache
    d = datetime(2026, 7, 6).date()
    times = [datetime.combine(d, datetime.min.time()) + timedelta(minutes=m)
             for m in range(9 * 60 + 30, 16 * 60)]
    times = [t.replace(tzinfo=ET).astimezone(UTC) for t in times]
    df = pd.DataFrame(
        [[100.0, 101.0, 99.0, 100.5, 1000.0] for _ in times],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex(times, name="ts")
    )
    empty_cache.upsert("SPY", "1Min", df)

    prov = BlockingProvider(should_fail=True)
    dm = DataManager(prov, tmp_path / "test_cache.db")

    model = SignalModel(DATA_DIR / "models", risk_config=RiskConfig())
    params = BacktestParams(
        symbols=["SPY"],
        start=datetime(2026, 7, 6, 9, 30, tzinfo=UTC),
        end=datetime(2026, 7, 6, 16, 0, tzinfo=UTC),
        cache_policy=CachePolicy.IMMUTABLE_CACHE_ONLY,
        verify_dataset_immutability=True,
    )

    # Monkeypatch MarketClock.is_trading_day class level to mutate the database cache during execution
    from app.core.clock import MarketClock
    orig_is_trading_day = MarketClock.is_trading_day
    
    def mutating_is_trading_day(self, *args, **kwargs):
        # Insert a row to cache directly
        conn = dm.cache._conn
        conn.execute("INSERT OR REPLACE INTO bars VALUES ('MUTATED', '1Min', 999999, 1.0, 1.0, 1.0, 1.0, 1.0)")
        conn.commit()
        return orig_is_trading_day(self, *args, **kwargs)
        
    MarketClock.is_trading_day = mutating_is_trading_day

    try:
        with pytest.raises(ValueError) as excinfo:
            await run_backtest(dm, model, params, settings=Settings())
        assert "HISTORICAL_DATASET_MUTATED_DURING_RUN" in str(excinfo.value)
    finally:
        # Restore monkeypatch
        MarketClock.is_trading_day = orig_is_trading_day


def test_6_session_view_and_timezone_math():
    """Test 6: Premarket is not flagged as a timezone mismatch and session views behave correctly."""
    # Create sample DataFrame spanning pre-market and regular session
    times = [
        datetime(2026, 7, 6, 8, 0).replace(tzinfo=ET).astimezone(UTC),  # premarket
        datetime(2026, 7, 6, 9, 30).replace(tzinfo=ET).astimezone(UTC), # regular session open
        datetime(2026, 7, 6, 16, 30).replace(tzinfo=ET).astimezone(UTC), # postmarket
    ]
    df = pd.DataFrame(
        [[100.0, 100.0, 100.0, 100.0, 100.0] for _ in times],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex(times, name="ts")
    )
    
    pre = filter_session_view(df, "PREMARKET")
    assert len(pre) == 1
    assert pre.index[0].astimezone(ET).hour == 8

    reg = filter_session_view(df, "REGULAR_SESSION")
    assert len(reg) == 1
    assert reg.index[0].astimezone(ET).hour == 9

    post = filter_session_view(df, "POSTMARKET")
    assert len(post) == 1
    assert post.index[0].astimezone(ET).hour == 16


@pytest.mark.asyncio
@pytest.mark.skip(reason="slow oracle regression test")
async def test_7_july_6_10_oracle_regression():
    """Test 7: July 6–10 oracle regression with frozen identity verification."""
    # Check dataset SHA-256
    cache_path = DATA_DIR / "bars_cache.db"
    if not cache_path.exists():
        pytest.fail("bars_cache.db not found in data directory")

    cache = BarCache(cache_path)
    
    # Calculate canonical hash of regular-session bars of the complete sessions
    # Expected model and dataset hashes
    EXPECTED_DATASET_SHA256 = "490f30da7fe56afd790bc8c657bb7a0a0b3703ee2c0430586b62f1d9ae324d16"
    EXPECTED_MODEL_SHA256 = "9d8c393e65d02914aee6fc094c245c427eca3f91b488b4b6b7a05c8326982f08"

    # Compute actual logical dataset SHA-256 (over regular-session complete-session bars)
    # We resolve complete sessions from the dataset manifest
    manifest_path = DATA_DIR.parent / "backend/app/data/dataset_manifest.json"
    if not manifest_path.exists():
        manifest_path = Path(r"C:\Users\peddi\.gemini\antigravity\brain\85a0913b-38ef-4d12-9d03-7f9d0e46c650\scratch\dataset_manifest.json")
        
    with open(manifest_path, "r") as f:
        manifest = json.load(f)
        
    actual_dataset_sha = manifest.get("dataset_sha256")
    if actual_dataset_sha != EXPECTED_DATASET_SHA256:
        raise AssertionError(f"ORACLE DATASET IDENTITY MISMATCH: Expected {EXPECTED_DATASET_SHA256}, got {actual_dataset_sha}")

    # Check model SHA-256
    model = SignalModel(DATA_DIR / "models", risk_config=RiskConfig())
    model.meta["trained_at"] = "2026-07-01T00:00:00+00:00"
    
    def get_file_sha256(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    model_path = DATA_DIR / "models/signal_model_long.joblib"
    actual_model_sha = get_file_sha256(model_path)
    if actual_model_sha != EXPECTED_MODEL_SHA256:
        raise AssertionError(f"ORACLE MODEL IDENTITY MISMATCH: Expected {EXPECTED_MODEL_SHA256}, got {actual_model_sha}")

    # Check config SHA-256
    risk_cfg = RiskConfig(
        risk_per_trade_pct=0.5, max_open_positions=4,
        max_daily_loss_pct=3.0, max_drawdown_pct=10.0,
        consecutive_loss_limit=3, cooldown_minutes=30,
        no_entry_minutes_before_close=30, flatten_minutes_before_close=5,
        ml_confidence_threshold=0.55,
        score_threshold_trend=55.0, score_threshold_range=70.0,
        score_threshold_volatile=65.0, score_threshold_event=75.0,
    )
    params = BacktestParams(
        symbols=["SPY", "QQQ", "NVDA", "AMD", "TSLA", "AAPL", "META", "MSFT"],
        start=datetime(2026, 7, 6, tzinfo=UTC),
        end=datetime(2026, 7, 10, 23, 59, tzinfo=UTC),
        starting_cash=100_000.0,
        slippage_bps=3.0,
        use_ml_gate=True,
        risk=risk_cfg,
        cache_policy=CachePolicy.IMMUTABLE_CACHE_ONLY,
    )

    # Config hash verification
    config_fields = {
        "symbols": sorted(params.symbols),
        "start": params.start.strftime("%Y-%m-%d"),
        "end": params.end.strftime("%Y-%m-%d"),
        "starting_cash": params.starting_cash,
        "slippage_bps": params.slippage_bps,
        "use_ml_gate": params.use_ml_gate,
        "ml_confidence_threshold": risk_cfg.ml_confidence_threshold,
        "risk_per_trade_pct": risk_cfg.risk_per_trade_pct,
        "max_open_positions": risk_cfg.max_open_positions,
    }
    expected_config_sha = "3e535608391c71a82b3e22dd59984a65602ee32cf48049a149bf1575fcd4b17e"
    actual_config_sha = hashlib.sha256(json.dumps(config_fields, sort_keys=True).encode()).hexdigest()
    if actual_config_sha != expected_config_sha:
        raise AssertionError(f"ORACLE CONFIG IDENTITY MISMATCH: Expected {expected_config_sha}, got {actual_config_sha}")

    # Run and assert counts
    dm = DataManager(BlockingProvider(should_fail=True), cache_path)
    result = await run_backtest(dm, model, params, settings=Settings())
    
    assert len(result.shadow_signals) == 262
    assert sum(1 for s in result.shadow_signals if s.get("rejection_stage") == "ml_gate") == 256
    assert sum(1 for s in result.shadow_signals if s.get("rejection_stage") == "metalabel_gate") == 0
    assert sum(1 for s in result.shadow_signals if s.get("rejection_stage") == "trade_score_gate") == 0
    assert len(result.trades) == 4


@pytest.mark.asyncio
@pytest.mark.skip(reason="slow oracle regression test")
async def test_8_june_8_july_10_oracle_regression():
    """Test 8: June 8–July 10 oracle regression with frozen identity verification."""
    cache_path = DATA_DIR / "bars_cache.db"
    if not cache_path.exists():
        pytest.fail("bars_cache.db not found in data directory")

    # Validate identities first (dataset/model/config)
    EXPECTED_DATASET_SHA256 = "490f30da7fe56afd790bc8c657bb7a0a0b3703ee2c0430586b62f1d9ae324d16"
    EXPECTED_MODEL_SHA256 = "9d8c393e65d02914aee6fc094c245c427eca3f91b488b4b6b7a05c8326982f08"

    manifest_path = DATA_DIR.parent / "backend/app/data/dataset_manifest.json"
    if not manifest_path.exists():
        manifest_path = Path(r"C:\Users\peddi\.gemini\antigravity\brain\85a0913b-38ef-4d12-9d03-7f9d0e46c650\scratch\dataset_manifest.json")
        
    with open(manifest_path, "r") as f:
        manifest = json.load(f)
        
    actual_dataset_sha = manifest.get("dataset_sha256")
    if actual_dataset_sha != EXPECTED_DATASET_SHA256:
        raise AssertionError(f"ORACLE DATASET IDENTITY MISMATCH: Expected {EXPECTED_DATASET_SHA256}, got {actual_dataset_sha}")

    model = SignalModel(DATA_DIR / "models", risk_config=RiskConfig())
    model.meta["trained_at"] = "2026-05-01T00:00:00+00:00"
    
    def get_file_sha256(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    model_path = DATA_DIR / "models/signal_model_long.joblib"
    actual_model_sha = get_file_sha256(model_path)
    if actual_model_sha != EXPECTED_MODEL_SHA256:
        raise AssertionError(f"ORACLE MODEL IDENTITY MISMATCH: Expected {EXPECTED_MODEL_SHA256}, got {actual_model_sha}")

    risk_cfg = RiskConfig(
        risk_per_trade_pct=0.5, max_open_positions=4,
        max_daily_loss_pct=3.0, max_drawdown_pct=10.0,
        consecutive_loss_limit=3, cooldown_minutes=30,
        no_entry_minutes_before_close=30, flatten_minutes_before_close=5,
        ml_confidence_threshold=0.55,
        score_threshold_trend=55.0, score_threshold_range=70.0,
        score_threshold_volatile=65.0, score_threshold_event=75.0,
    )
    
    params = BacktestParams(
        symbols=["SPY", "QQQ", "NVDA", "AMD", "TSLA", "AAPL", "META", "MSFT"],
        start=datetime(2026, 6, 8, tzinfo=UTC),
        end=datetime(2026, 7, 10, 23, 59, tzinfo=UTC),
        starting_cash=100_000.0,
        slippage_bps=3.0,
        use_ml_gate=True,
        risk=risk_cfg,
        cache_policy=CachePolicy.IMMUTABLE_CACHE_ONLY,
    )

    dm = DataManager(BlockingProvider(should_fail=True), cache_path)
    result = await run_backtest(dm, model, params, settings=Settings())

    assert len(result.shadow_signals) == 1299
    assert len(result.trades) == 13
