"""AlphaDesk application entrypoint.

Run from the repo root:
    uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000

The FastAPI app serves the REST API, the /ws event stream, and (when built)
the production frontend from frontend/dist.
"""
from __future__ import annotations

import asyncio
import math
import sys
import time
from contextlib import asynccontextmanager

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport
        orig_call = _ProactorBasePipeTransport._call_connection_lost
        def patched_call(self, exc):
            try:
                orig_call(self, exc)
            except ConnectionResetError:
                pass
            except OSError as e:
                if getattr(e, "winerror", None) == 10054:
                    pass
                else:
                    raise
        _ProactorBasePipeTransport._call_connection_lost = patched_call
    except ImportError:
        pass

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.routes import router
from app.api.replay import router as replay_router
from app.api.ws import EventHub
from app.config import FRONTEND_DIST, Settings, get_settings
from app.core.engine import TradingEngine
from app.core.logging import applog, configure_std_logging
from app.db.database import Database


def _json_safe(value):
    """Normalize non-finite floats at the HTTP JSON boundary."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


_json_response_render = JSONResponse.render

def _safe_json_response_render(self, content) -> bytes:
    return _json_response_render(self, _json_safe(content))

JSONResponse.render = _safe_json_response_render


class SafeJSONResponse(JSONResponse):
    """Default response class; JSONResponse itself is normalized above too."""


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_std_logging()
    settings = settings or get_settings()
    db = Database(settings.resolved_db_path)
    engine = TradingEngine(settings, db)
    hub = EventHub()
    engine.publish = hub.publish

    _log_seq = 0
    def _safe_log_publish(entry) -> None:
        nonlocal _log_seq
        if entry.level.upper() == "DEBUG":
            return
        _log_seq += 1
        hub.publish("log", entry.model_dump(mode="json"))

    applog.subscribe(_safe_log_publish)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.set_loop(asyncio.get_running_loop())
        await engine.ensure_data()
        applog.info("app", f"AlphaDesk v{__version__} ready -- mode={settings.mode}")
        yield
        await engine.shutdown()

    app = FastAPI(
        title="AlphaDesk",
        version=__version__,
        lifespan=lifespan,
        default_response_class=SafeJSONResponse,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.engine = engine
    app.state.hub = hub
    app.state.backtest_lock = asyncio.Lock()

    import socket
    local_ips = ["localhost", "127.0.0.1"]
    try:
        hostname = socket.gethostname()
        local_ips.append(hostname)
        for ip in socket.gethostbyname_ex(hostname)[2]:
            local_ips.append(ip)
    except Exception:
        pass

    origins = []
    for ip in local_ips:
        origins.append(f"http://{ip}:5173")
        origins.append(f"https://{ip}:5173")
        origins.append(f"http://{ip}:{settings.port}")
        origins.append(f"https://{ip}:{settings.port}")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)
    app.include_router(replay_router)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        t0 = time.perf_counter()
        t_broker_ms = 0.0
        bootstrap = {
            "status": engine.status(),
            "quotes": engine.quotes_payload(),
            "orders": engine.orders_payload(),
            "insights": [i.model_dump(mode="json") for i in engine.insights.recent(30)],
            "logs": [e.model_dump(mode="json") for e in applog.recent(100)],
            "metrics": engine.metrics_payload(),
        }
        if engine.broker is not None:
            tb0 = time.perf_counter()
            try:
                bootstrap["positions"] = [p.to_payload() for p in await engine.broker.positions()]
                account = await engine.broker.account()
                bootstrap["account"] = {
                    "equity": account.equity,
                    "cash": account.cash,
                    "buying_power": account.buying_power,
                    "day_pnl": round(engine.portfolio.day_pnl(account.equity), 2) if engine.portfolio else 0.0,
                    "day_pnl_pct": 0.0,
                    "drawdown_pct": round(engine.portfolio.drawdown_pct(account.equity), 2) if engine.portfolio else 0.0,
                }
            except Exception as exc:
                applog.warning("ws", f"Broker calls during WebSocket bootstrap failed: {exc}")
            t_broker_ms = round((time.perf_counter() - tb0) * 1000, 2)

        t_total_ms = round((time.perf_counter() - t0) * 1000, 2)
        applog.debug("ws", f"WebSocket bootstrap built in {t_total_ms}ms (broker: {t_broker_ms}ms)")
        await hub.handle(ws, bootstrap)

    if FRONTEND_DIST.exists():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
    else:
        @app.get("/")
        async def index_placeholder() -> dict:
            return {
                "app": "AlphaDesk API",
                "version": __version__,
                "note": "Frontend not built. Run the install script, or use `npm run dev` in frontend/ for development.",
                "docs": "/docs",
            }

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    s = get_settings()
    uvicorn.run(app, host=s.host, port=s.port)
