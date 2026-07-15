from types import SimpleNamespace

import pytest

from app.data.alpaca_data import AlpacaDataProvider
from app.data.base import DataProvider


class _NoDiscoveryProvider(DataProvider):
    async def get_bars(self, symbol, timeframe="1Min", start=None, end=None, limit=None):
        raise AssertionError("not used")

    async def get_quotes(self, symbols):
        raise AssertionError("not used")


@pytest.mark.asyncio
async def test_provider_discovery_capability_is_explicit_and_fail_closed():
    provider = _NoDiscoveryProvider()

    assert provider.supports_asset_discovery is False
    with pytest.raises(NotImplementedError, match="does not support asset discovery"):
        await provider.discover_assets()


@pytest.mark.asyncio
async def test_alpaca_discovery_requests_active_us_equities_and_maps_metadata(monkeypatch):
    class AssetStatus:
        ACTIVE = "active"

    class AssetClass:
        US_EQUITY = "us_equity"

    class GetAssetsRequest:
        def __init__(self, *, status, asset_class):
            self.status = status
            self.asset_class = asset_class

    monkeypatch.setattr("alpaca.trading.enums.AssetStatus", AssetStatus)
    monkeypatch.setattr("alpaca.trading.enums.AssetClass", AssetClass)
    monkeypatch.setattr("alpaca.trading.requests.GetAssetsRequest", GetAssetsRequest)

    captured = {}

    class TradingClient:
        def get_all_assets(self, request):
            captured["request"] = request
            return [
                SimpleNamespace(
                    symbol="AAPL",
                    status=AssetStatus.ACTIVE,
                    tradable=True,
                    asset_class=AssetClass.US_EQUITY,
                    exchange="NASDAQ",
                ),
                SimpleNamespace(
                    symbol="LOCKED",
                    status=AssetStatus.ACTIVE,
                    tradable=False,
                    asset_class=AssetClass.US_EQUITY,
                    exchange="NYSE",
                ),
            ]

    provider = AlpacaDataProvider.__new__(AlpacaDataProvider)
    provider._trading_client = TradingClient()

    assets = await provider.discover_assets()

    assert provider.supports_asset_discovery is True
    assert captured["request"].status == AssetStatus.ACTIVE
    assert captured["request"].asset_class == AssetClass.US_EQUITY
    assert [(asset.symbol, asset.active, asset.tradable, asset.asset_class, asset.exchange) for asset in assets] == [
        ("AAPL", True, True, "us_equity", "NASDAQ"),
        ("LOCKED", True, False, "us_equity", "NYSE"),
    ]


@pytest.mark.asyncio
async def test_alpaca_discovery_failure_returns_empty_universe():
    class TradingClient:
        def get_all_assets(self, request):
            raise RuntimeError("provider unavailable")

    provider = AlpacaDataProvider.__new__(AlpacaDataProvider)
    provider._trading_client = TradingClient()

    assert await provider.discover_assets() == ()
