from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from app.config import RiskConfig
from app.core.domain import EngineState, Quote
from app.core.engine import TradingEngine
from app.core.gap_scanner import PreMarketGapScanner
from app.core.scanner import MarketScanner, ScannerOutcome
from app.core.scanner_selection import select_sector_diverse_momentum
from app.core.top_movers_scanner import TopMoversResult, TopMoversScanner
from app.core.universe_discovery import DiscoverableAsset, DiscoveryCoordinator
from app.data.alpaca_data import AlpacaDataProvider
from app.db.database import Database
from app.replay.clock import ReplayClock
from app.replay.ledger import ReplayLedger


def _engine(tmp_path: Path, risk_config: RiskConfig | None = None) -> TradingEngine:
    config = risk_config or RiskConfig()
    settings = MagicMock()
    settings.mode = "demo"
    settings.watchlist_symbols.return_value = ["SPY"]
    settings.risk_config.return_value = config
    engine = TradingEngine(settings, Database(tmp_path / "scanner.db"))
    engine.state = EngineState.RUNNING
    engine.risk_config = config
    engine.data = MagicMock()
    engine.data.provider = SimpleNamespace(name="mock", supports_asset_discovery=False)
    engine.data.warmup = AsyncMock()
    engine.broker = MagicMock()
    engine.broker.positions = AsyncMock(return_value=[])
    engine.broker.open_orders = AsyncMock(return_value=[])
    engine.publish = MagicMock()
    engine.status = MagicMock(return_value={})
    return engine


def _scanner_mock(outcome: ScannerOutcome, watchlist: list[str]) -> MagicMock:
    scanner = MagicMock()
    scanner.run = AsyncMock(return_value=(outcome, watchlist))
    scanner.get_last_scans = MagicMock(return_value=[])
    return scanner


@pytest.mark.asyncio
async def test_live_engine_scan_keeps_live_discovery_semantics(tmp_path: Path):
    engine = _engine(tmp_path)
    scanner = _scanner_mock(ScannerOutcome.SUCCESS_EMPTY, [])

    with patch("app.core.scanner.MarketScanner", return_value=scanner):
        await engine.run_market_scanner(is_intraday=True)

    scanner.run.assert_awaited_once_with(as_of=None)


@pytest.mark.asyncio
async def test_replay_engine_scan_is_explicitly_causal(tmp_path: Path):
    scan_time = datetime(2026, 7, 14, 14, 0, tzinfo=UTC)
    engine = _engine(tmp_path)
    engine.clock = ReplayClock(scan_time, always_open=True)
    engine.replay_fast_path = True
    scanner = _scanner_mock(ScannerOutcome.SUCCESS_EMPTY, [])

    with patch("app.core.scanner.MarketScanner", return_value=scanner):
        await engine.run_market_scanner(is_intraday=True)

    scanner.run.assert_awaited_once_with(as_of=scan_time)


@pytest.mark.asyncio
async def test_engine_reuses_scanner_instance_for_mover_cache(tmp_path: Path):
    engine = _engine(tmp_path)
    scanner = _scanner_mock(ScannerOutcome.SUCCESS_EMPTY, [])
    factory = MagicMock(return_value=scanner)

    with patch("app.core.scanner.MarketScanner", factory):
        await engine.run_market_scanner(is_intraday=True)
        await engine.run_market_scanner(is_intraday=True)

    factory.assert_called_once()


def test_production_scanner_selection_uses_symbol_scoped_unknown_buckets():
    candidates = [
        {"symbol": f"DYN{i}", "sector": "unknown", "auc": 0.61}
        for i in range(1, 9)
    ]

    selected = MarketScanner._select_final(candidates)

    assert selected == [f"DYN{i}" for i in range(1, 9)]
    assert select_sector_diverse_momentum(candidates, max_symbols=8, max_per_sector=3) == selected


@pytest.mark.asyncio
async def test_scanner_ttl_uses_the_configured_risk_field(tmp_path: Path):
    scan_time = datetime(2026, 7, 14, 14, 0, tzinfo=UTC)
    engine = _engine(tmp_path, RiskConfig(scanner_watchlist_ttl=7))
    scanner = _scanner_mock(ScannerOutcome.SUCCESS_NONEMPTY, ["AAPL"])

    with patch("app.core.scanner.MarketScanner", return_value=scanner):
        await engine.run_market_scanner(is_intraday=True, as_of=scan_time)

    assert engine.scanned_watchlist_expires_at["AAPL"] == scan_time + timedelta(minutes=7)
    assert engine.scanned_watchlist_ttl["AAPL"] == 7


@pytest.mark.asyncio
async def test_event_monitor_additions_receive_bounded_scanner_ttl(tmp_path: Path, monkeypatch):
    now = datetime(2026, 7, 14, 14, 0, tzinfo=UTC)
    engine = _engine(tmp_path, RiskConfig(scanner_watchlist_ttl=7))
    frame = pd.DataFrame(
        {
            "open": [100.0] * 20,
            "high": [101.0] * 20,
            "low": [99.0] * 20,
            "close": [100.0] * 19 + [102.0],
            "volume": [100.0] * 20,
        },
        index=pd.date_range("2026-07-14 13:40:00", periods=20, freq="min", tz="UTC"),
    )
    engine.data.provider.get_multi_bars = AsyncMock(return_value={"EVENT1": frame})
    engine.data.warmup = AsyncMock()
    engine.calendar.check = AsyncMock(return_value=(False, ""))
    engine.model.score_action = MagicMock(return_value=0.9)
    monkeypatch.setattr("app.core.scanner.CANDIDATE_POOL", ["EVENT1"])
    monkeypatch.setattr("app.indicators.ta.enrich", lambda df: df)

    await engine.run_event_monitor(now)

    assert "EVENT1" in engine.scanned_watchlist
    assert engine.scanned_watchlist_expires_at["EVENT1"] == now + timedelta(minutes=7)
    assert engine.scanned_watchlist_ttl["EVENT1"] == 7


@pytest.mark.asyncio
async def test_failed_scan_is_visible_and_does_not_mark_the_day_complete(tmp_path: Path):
    engine = _engine(tmp_path)
    scanner = _scanner_mock(ScannerOutcome.FAILED, [])

    with patch("app.core.scanner.MarketScanner", return_value=scanner):
        result = await engine.run_market_scanner(is_intraday=True)

    assert result == []
    assert engine._last_scan_date is None
    assert engine.scanner_status["outcome"] == ScannerOutcome.FAILED.value
    assert engine.scanner_status["status"] == "failed"


@pytest.mark.asyncio
async def test_replay_scanner_candidates_are_recorded_for_forensics(tmp_path: Path):
    scan_time = datetime(2026, 7, 14, 14, 0, tzinfo=UTC)
    engine = _engine(tmp_path)
    engine.runtime_ledger = ReplayLedger("replay", "2026-07-14", "2026-07-14")
    scanner = _scanner_mock(ScannerOutcome.SUCCESS_NONEMPTY, ["AAPL"])
    scanner.get_last_scans.return_value = [
        {"symbol": "AAPL", "score": 0.82, "auc": 0.61, "sector": "mega_cap"},
    ]

    with patch("app.core.scanner.MarketScanner", return_value=scanner):
        await engine.run_market_scanner(is_intraday=True, as_of=scan_time)

    assert len(engine.runtime_ledger.candidates) == 1
    details = engine.runtime_ledger.candidates[0]["details"]
    assert details["selected"] is True
    assert details["outcome"] == ScannerOutcome.SUCCESS_NONEMPTY.value


@pytest.mark.asyncio
async def test_negative_gap_preserves_loser_direction():
    provider = SimpleNamespace(
        get_snapshots=AsyncMock(
            return_value={
                "DOWN": SimpleNamespace(
                    prev_daily_bar=SimpleNamespace(c=100.0),
                    daily_bar=SimpleNamespace(o=95.0, v=1_000_000.0),
                    minute_bar=SimpleNamespace(c=95.0),
                )
            }
        )
    )
    scanner = PreMarketGapScanner(SimpleNamespace(provider=provider))

    gaps = await scanner._fetch_via_snapshots(provider)
    assert gaps["DOWN"] == pytest.approx(-5.0)

    scanner._fetch_gap_movers = AsyncMock(return_value=gaps)
    scanner._fetch_top_movers = AsyncMock(
        return_value=SimpleNamespace(gainers=[], losers=[], entries={})
    )
    await scanner.get_gap_movers()
    assert scanner.mover_context["DOWN"]["direction"] == "loser"


@pytest.mark.asyncio
async def test_empty_gap_result_is_cached_instead_of_refetched():
    scanner = PreMarketGapScanner(SimpleNamespace(provider=SimpleNamespace()))
    scanner._fetch_gap_movers = AsyncMock(return_value={})
    scanner._fetch_top_movers = AsyncMock(
        return_value=TopMoversResult(source="empty")
    )

    assert await scanner.get_gap_movers() == []
    assert await scanner.get_gap_movers() == []
    assert scanner._fetch_gap_movers.await_count == 1
    assert scanner._fetch_top_movers.await_count == 1


@pytest.mark.asyncio
async def test_alpaca_snapshot_contract_reaches_gap_scanner():
    captured = {}

    class Client:
        def get_stock_snapshot(self, request):
            captured["symbols"] = request.symbol_or_symbols
            return {"AAPL": SimpleNamespace()}

    provider = AlpacaDataProvider.__new__(AlpacaDataProvider)
    provider._client = Client()

    result = await provider.get_snapshots(["aapl"])

    assert set(result) == {"AAPL"}
    assert captured["symbols"] == ["AAPL"]


@pytest.mark.asyncio
async def test_gap_scanner_accepts_alpaca_snapshot_field_names():
    provider = SimpleNamespace(
        get_snapshots=AsyncMock(
            return_value={
                "DOWN": SimpleNamespace(
                    previous_daily_bar=SimpleNamespace(close=100.0),
                    daily_bar=SimpleNamespace(open=95.0, volume=1_000_000.0),
                    latest_trade=SimpleNamespace(price=95.0),
                )
            }
        )
    )
    scanner = PreMarketGapScanner(SimpleNamespace(provider=provider))

    gaps = await scanner._fetch_via_snapshots(provider)

    assert gaps["DOWN"] == pytest.approx(-5.0)


@pytest.mark.asyncio
async def test_top_movers_uses_alpaca_screener_client():
    screener = SimpleNamespace(
        get_market_movers=MagicMock(
            return_value=SimpleNamespace(
                gainers=[SimpleNamespace(symbol="UP", price=5.0, percent_change=8.0)],
                losers=[SimpleNamespace(symbol="DOWN", price=5.0, percent_change=-8.0)],
            )
        )
    )
    provider = SimpleNamespace(_screener_client=screener)
    scanner = TopMoversScanner(SimpleNamespace(provider=provider))

    result = await scanner._fetch_via_screener()

    assert result.gainers == ["UP"]
    assert result.losers == ["DOWN"]
    assert result.source == "screener"


@pytest.mark.asyncio
async def test_top_movers_falls_back_when_screener_client_raises():
    screener = SimpleNamespace(
        get_market_movers=MagicMock(side_effect=RuntimeError("screener unavailable"))
    )
    provider = SimpleNamespace(_screener_client=screener)
    scanner = TopMoversScanner(SimpleNamespace(provider=provider))
    fallback = TopMoversResult(gainers=["UP"], source="bars")
    scanner._fetch_via_bars = AsyncMock(return_value=fallback)

    result = await scanner._fetch_movers()

    assert result is fallback
    scanner._fetch_via_bars.assert_awaited_once()


@pytest.mark.asyncio
async def test_alpaca_discovery_includes_losers_from_market_movers():
    class Screener:
        def get_most_actives(self, request):
            raise RuntimeError("not configured")

        def get_market_movers(self, request):
            return SimpleNamespace(
                gainers=[SimpleNamespace(symbol="UP")],
                losers=[SimpleNamespace(symbol="DOWN")],
            )

    provider = AlpacaDataProvider.__new__(AlpacaDataProvider)
    provider._screener_client = Screener()

    assets = await provider.discover_assets()

    assert {asset.symbol for asset in assets} == {"UP", "DOWN"}


def test_scanner_endpoint_does_not_report_failed_scan_as_success(tmp_path: Path):
    from fastapi.testclient import TestClient

    from app.main import app

    engine = _engine(tmp_path)
    engine.run_market_scanner = AsyncMock(return_value=[])
    engine.scanner_status = {
        "status": "failed",
        "outcome": ScannerOutcome.FAILED.value,
        "last_error": "scanner pipeline failed",
    }

    with patch("app.api.routes._engine", return_value=engine):
        response = TestClient(app).post("/api/scanner/run")

    assert response.status_code == 503
    assert response.json()["detail"]["scanner"]["outcome"] == ScannerOutcome.FAILED.value


@pytest.mark.asyncio
async def test_discovery_refreshes_quotes_after_shortlist_ttl():
    now = datetime(2026, 7, 14, 14, 0, tzinfo=UTC)
    current = [now]
    provider = SimpleNamespace(
        name="mock",
        supports_asset_discovery=True,
        discover_assets=AsyncMock(
            return_value=(DiscoverableAsset("AAPL", True, True),)
        ),
        get_quotes=AsyncMock(
            side_effect=lambda symbols: {
                "AAPL": Quote(
                    symbol="AAPL",
                    price=100.0,
                    change_pct=1.0,
                    volume=10_000.0,
                    ts=current[0],
                )
            }
        ),
    )
    coordinator = DiscoveryCoordinator(
        provider,
        feed="iex",
        asset_cache_ttl=timedelta(hours=6),
        result_cache_ttl=timedelta(minutes=15),
        batch_size=200,
        request_budget=10,
        clock=lambda: current[0],
    )

    await coordinator.acquire()
    current[0] = now + timedelta(minutes=16)
    await coordinator.acquire()

    assert provider.discover_assets.await_count == 1
    assert provider.get_quotes.await_count == 2


def test_fast_replay_persists_scanner_events():
    from app.api import replay as replay_api

    result = {
        "manifest": {},
        "scanner": {
            "timeline": [{"event_type": "SCANNER_CANDIDATE", "symbol": "AAPL"}],
            "candidates": [{"event_type": "SCANNER_CANDIDATE", "symbol": "AAPL"}],
        },
        "trades": [],
        "equity_curve": [],
    }

    with patch.object(replay_api.session_mgr, "save_artifact") as save_artifact:
        replay_api._persist_fast_result("rpl_test", result)

    ledger_call = next(
        call for call in save_artifact.call_args_list if call.args[1] == "ledger"
    )
    assert ledger_call.args[2]["candidates"][0]["symbol"] == "AAPL"


def test_replay_ledger_exposes_nonempty_scanner_hash():
    ledger = ReplayLedger("rpl_hash", "2026-07-14", "2026-07-14")
    ledger.record_event(
        "SCANNER_CANDIDATE",
        datetime(2026, 7, 14, 14, 0, tzinfo=UTC),
        "AAPL",
        {"selected": True, "auc": 0.61},
    )

    payload = ledger.to_payload()

    assert payload["stage_hashes"]["scanner_hash"]
