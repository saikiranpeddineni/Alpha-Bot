"""API smoke tests over the full app (demo mode, temp storage)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        ALPHADESK_MODE="demo",
        DATA_PROVIDER="synthetic",
        ALPHADESK_DB_PATH=str(tmp_path / "test.db"),
        DEFAULT_WATCHLIST="SPY,QQQ",
        DEMO_BAR_SECONDS=3600,  # engine loop effectively idle during tests
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_status(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "stopped"
    assert body["mode"] == "demo"
    assert body["provider"] == "synthetic"
    assert "SPY" in body["watchlist"]


def test_watchlist_roundtrip(client):
    r = client.put("/api/watchlist", json={"symbols": ["nvda", "SPY", "spy"]})
    assert r.status_code == 200
    assert r.json()["symbols"] == ["NVDA", "SPY"]
    assert client.get("/api/watchlist").json()["symbols"] == ["NVDA", "SPY"]
    assert client.put("/api/watchlist", json={"symbols": []}).status_code == 400


def test_bars_endpoint(client):
    r = client.get("/api/bars", params={"symbol": "SPY", "limit": 50})
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "SPY"
    assert len(body["bars"]) > 0
    bar = body["bars"][-1]
    for key in ("t", "o", "h", "l", "c", "v"):
        assert key in bar


def test_engine_start_stop_demo(client):
    r = client.post("/api/engine/start", json={"mode": "demo"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "running"
    # Double start is a clean 400
    assert client.post("/api/engine/start", json={"mode": "demo"}).status_code == 400

    positions = client.get("/api/positions")
    assert positions.status_code == 200

    order = client.post("/api/orders", json={"symbol": "SPY", "side": "buy", "qty": 5})
    assert order.status_code == 200, order.text
    assert order.json()["status"] == "filled"
    positions = client.get("/api/positions").json()
    assert any(p["symbol"] == "SPY" for p in positions)

    closed = client.post("/api/positions/SPY/close")
    assert closed.status_code == 200

    r = client.post("/api/engine/stop", json={})
    assert r.status_code == 200
    assert r.json()["state"] == "stopped"


def test_live_mode_is_gated(client):
    r = client.post("/api/engine/start", json={"mode": "live", "confirm": "LIVE"})
    assert r.status_code == 400
    assert "disabled" in r.json()["detail"].lower()


def test_risk_settings_validation(client):
    r = client.put("/api/settings/risk", json={"risk_per_trade_pct": 0.75})
    assert r.status_code == 200
    assert r.json()["risk_per_trade_pct"] == 0.75
    assert client.get("/api/settings").json()["risk"]["risk_per_trade_pct"] == 0.75
    # Out-of-bounds rejected by validation
    assert client.put("/api/settings/risk", json={"risk_per_trade_pct": 50}).status_code == 400


def test_strategies_endpoints(client):
    r = client.get("/api/strategies")
    assert r.status_code == 200
    names = {s["name"] for s in r.json()}
    assert {"orb", "momentum", "vwap_reversion", "rsi2_reversion"} <= names
    r = client.put("/api/strategies/orb", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert client.put("/api/strategies/nope", json={"enabled": True}).status_code == 404


def test_backtest_endpoint(client):
    r = client.post("/api/backtest", json={"symbols": ["SPY"], "days": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "metrics" in body and "equity_curve" in body
    runs = client.get("/api/backtests").json()
    assert len(runs) == 1
    full = client.get(f"/api/backtests/{body['id']}")
    assert full.status_code == 200
    assert full.json()["id"] == body["id"]


def test_logs_insights_metrics(client):
    assert client.get("/api/logs").status_code == 200
    assert client.get("/api/insights").status_code == 200
    assert client.get("/api/metrics").status_code == 200
