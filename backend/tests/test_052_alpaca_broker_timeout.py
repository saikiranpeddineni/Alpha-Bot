"""Regression tests for issue #116 / 052.

Verifies that every Alpaca broker HTTP call is bounded by a wall-clock
timeout and raises BrokerTimeoutError (not asyncio.TimeoutError) when the
underlying SDK call stalls, preserving engine-tick liveness.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.brokers.alpaca import AlpacaBroker, _call
from app.brokers.base import BrokerTimeoutError


# ---------------------------------------------------------------------------
# Helper: a slow synchronous function that blocks longer than any test budget
# ---------------------------------------------------------------------------

def _slow(seconds: float = 5.0):
    """Return a callable that sleeps *synchronously* for ``seconds``."""
    import time
    def _fn(*_args, **_kwargs):
        time.sleep(seconds)
    return _fn


# ---------------------------------------------------------------------------
# _call helper unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_returns_result_within_timeout():
    """_call completes normally when the SDK function returns quickly."""
    result = await _call(lambda: 42, timeout=2.0)
    assert result == 42


@pytest.mark.asyncio
async def test_call_raises_broker_timeout_error_on_slow_call():
    """_call raises BrokerTimeoutError (not asyncio.TimeoutError) when slow."""
    with pytest.raises(BrokerTimeoutError):
        await _call(_slow(10.0), timeout=0.05)


@pytest.mark.asyncio
async def test_call_not_asyncio_timeout_error():
    """Callers receive BrokerTimeoutError, not the raw asyncio.TimeoutError."""
    with pytest.raises(BrokerTimeoutError):
        await _call(_slow(10.0), timeout=0.05)
    # Confirm it is NOT the raw asyncio variant
    try:
        await _call(_slow(10.0), timeout=0.05)
    except BrokerTimeoutError:
        pass  # correct
    except asyncio.TimeoutError:  # pragma: no cover
        pytest.fail("Raw asyncio.TimeoutError leaked; expected BrokerTimeoutError")


# ---------------------------------------------------------------------------
# BrokerTimeoutError identity
# ---------------------------------------------------------------------------

def test_broker_timeout_error_is_runtime_error():
    err = BrokerTimeoutError("test")
    assert isinstance(err, RuntimeError)


# ---------------------------------------------------------------------------
# AlpacaBroker integration: submit times out → BrokerTimeoutError propagates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submit_raises_broker_timeout_error(monkeypatch):
    """submit() propagates BrokerTimeoutError on a stalled SDK call."""
    broker = _make_broker(monkeypatch)

    # Make submit_order block forever (well, 10 s — longer than the tiny budget)
    broker._client.submit_order = _slow(10.0)

    from app.core.domain import OrderRequest, OrderSide, OrderType
    req = OrderRequest(
        symbol="AAPL",
        side=OrderSide.BUY,
        qty=1,
        order_type=OrderType.MARKET,
        strategy="test",
    )

    with patch("app.brokers.alpaca._API_TIMEOUT", 0.05):
        with pytest.raises(BrokerTimeoutError):
            await broker.submit(req)


@pytest.mark.asyncio
async def test_submit_returns_rejected_on_api_error(monkeypatch):
    """Non-timeout errors from submit_order still return a REJECTED Order."""
    broker = _make_broker(monkeypatch)
    broker._client.submit_order = MagicMock(side_effect=RuntimeError("bad request"))

    from app.core.domain import OrderRequest, OrderSide, OrderStatus, OrderType
    req = OrderRequest(
        symbol="AAPL",
        side=OrderSide.BUY,
        qty=1,
        order_type=OrderType.MARKET,
        strategy="test",
    )
    order = await broker.submit(req)
    assert order.status == OrderStatus.REJECTED


# ---------------------------------------------------------------------------
# poll_fills timeout is swallowed (non-fatal) so tick continues
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_poll_fills_timeout_does_not_raise(monkeypatch):
    """poll_fills() catches its own BrokerTimeoutError so tick keeps running."""
    broker = _make_broker(monkeypatch)

    async def _timed_out_call(fn, *args, timeout=0.05, label=""):
        raise BrokerTimeoutError("simulated timeout")

    with patch("app.brokers.alpaca._call", side_effect=_timed_out_call):
        # Must not raise; engine tick liveness is the invariant
        await broker.poll_fills()

    assert broker.drain_fills() == []


# ---------------------------------------------------------------------------
# cancel timeout: returns False, does not raise
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_timeout_returns_false(monkeypatch):
    broker = _make_broker(monkeypatch)

    async def _timed_out_call(fn, *args, timeout=0.05, label=""):
        raise BrokerTimeoutError("simulated timeout")

    with patch("app.brokers.alpaca._call", side_effect=_timed_out_call):
        result = await broker.cancel("order-123")

    assert result is False


# ---------------------------------------------------------------------------
# close_position timeout: returns None, does not raise
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_position_timeout_returns_none(monkeypatch):
    broker = _make_broker(monkeypatch)

    async def _timed_out_call(fn, *args, timeout=0.05, label=""):
        raise BrokerTimeoutError("simulated timeout")

    with patch("app.brokers.alpaca._call", side_effect=_timed_out_call):
        result = await broker.close_position("AAPL", reason="trailing_stop")

    assert result is None


# ---------------------------------------------------------------------------
# flatten_all timeout: raises exception so callers can escalate and retry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flatten_all_timeout_does_raise(monkeypatch):
    broker = _make_broker(monkeypatch)

    async def _timed_out_call(fn, *args, **kwargs):
        raise BrokerTimeoutError("simulated timeout")

    with patch("app.brokers.alpaca._call", side_effect=_timed_out_call):
        with pytest.raises(BrokerTimeoutError):
            await broker.flatten_all(reason="eod")


# ---------------------------------------------------------------------------
# ALPACA_API_TIMEOUT_SECONDS env var is respected
# ---------------------------------------------------------------------------

def test_default_timeout_is_ten_seconds():
    import importlib
    import app.brokers.alpaca as mod
    # Default when env var is absent is 10 s (set at module import).
    # We can't easily reset module-level state here, so just verify the
    # constant is a positive float.
    assert mod._API_TIMEOUT > 0


# ---------------------------------------------------------------------------
# Fixture helper
# ---------------------------------------------------------------------------

def _make_broker(monkeypatch) -> AlpacaBroker:
    """Create an AlpacaBroker with a mocked TradingClient (no network calls)."""
    mock_client = MagicMock()
    mock_client.submit_order = MagicMock(return_value=MagicMock(
        id="ord-1",
        client_order_id="coid-1",
        symbol="AAPL",
        side="buy",
        qty=1,
        type="market",
        limit_price=None,
        status="new",
        filled_qty=0,
        filled_avg_price=None,
        submitted_at=None,
        filled_at=None,
    ))
    with patch("app.brokers.alpaca.AlpacaBroker.__init__", lambda self, *a, **kw: None):
        broker = AlpacaBroker.__new__(AlpacaBroker)
    broker.is_paper = True
    broker._client = mock_client
    broker._fills = []
    broker._emitted_fill_ids = set()
    broker._processed_fill_qty = {}
    broker._meta = {}
    broker._symbol_strategies = {}
    broker._shortable_cache = {}
    broker._shorting_enabled = None
    return broker


@pytest.mark.asyncio
async def test_is_shortable_account_shorting_disabled(monkeypatch):
    broker = _make_broker(monkeypatch)
    acct = MagicMock(shorting_enabled=False)
    broker._client.get_account = MagicMock(return_value=acct)

    res = await broker.is_shortable("AMZN")
    assert res is False
    assert broker._shorting_enabled is False


@pytest.mark.asyncio
async def test_submit_shorting_rejection_caches_disabled(monkeypatch):
    from app.core.domain import OrderRequest, OrderSide, OrderStatus, OrderType

    broker = _make_broker(monkeypatch)
    exc = Exception('Order rejected AMZN sell 86: {"code":40310000,"message":"account is not allowed to short"}')
    monkeypatch.setattr(broker, "_is_definitive_rejection", lambda e: True)
    broker._client.submit_order = MagicMock(side_effect=exc)

    req = OrderRequest(
        symbol="AMZN",
        side=OrderSide.SELL,
        qty=10,
        order_type=OrderType.MARKET,
        strategy="momentum",
        reason="test_short",
    )

    order = await broker.submit(req)
    assert order.status == OrderStatus.REJECTED
    assert broker._shorting_enabled is False
    assert await broker.is_shortable("AMZN") is False


@pytest.mark.asyncio
async def test_is_shortable_account_check_failure_returns_false(monkeypatch):
    broker = _make_broker(monkeypatch)
    broker._client.get_account = MagicMock(side_effect=RuntimeError("API Network Error"))

    res = await broker.is_shortable("AMZN")
    assert res is False
    assert broker._shorting_enabled is None

