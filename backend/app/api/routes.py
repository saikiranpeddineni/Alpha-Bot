"""REST API.

Everything the dashboard does goes through these routes; the WebSocket at
/ws streams live updates. All state lives in app.state (engine, db, hub).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.applog import applog
from app.core.domain import EngineState, OrderRequest, OrderSide, OrderType, utcnow

router = APIRouter(prefix="/api")


def _engine(request: Request):  # noqa: ANN202
    return getattr(request.app.state, "engine", None)


def _db(request: Request):  # noqa: ANN202
    return request.app.state.db


# ---------------------------------------------------------------- status --

@router.get("/status")
async def get_status(request: Request) -> dict:
    return _engine(request).status()


@router.get("/system/heartbeat")
async def get_system_heartbeat(request: Request) -> dict:
    engine = _engine(request)
    return {
        "system_health": getattr(engine, "system_health", "NORMAL"),
        "allow_new_entries": getattr(engine, "allow_new_entries", True),
        "heartbeat_diagnostics": getattr(engine, "heartbeat_diagnostics", {})
    }


class AllowNewEntriesBody(BaseModel):
    allow: bool


@router.post("/system/allow-new-entries")
async def post_allow_new_entries(body: AllowNewEntriesBody, request: Request) -> dict:
    engine = _engine(request)
    engine._force_gated = not body.allow
    if not body.allow:
        engine.allow_new_entries = False
        if engine.system_health not in ("SAFE_MODE", "HALTED"):
            engine.system_health = "DEGRADED"
    else:
        # Trigger immediate heartbeat run to recheck if it can recover
        await engine._check_system_heartbeat(utcnow())

    return {
        "status": "success",
        "allow_new_entries": engine.allow_new_entries,
        "system_health": engine.system_health
    }


@router.post("/system/reset-health")
async def post_reset_health(request: Request) -> dict:
    engine = _engine(request)

    # Re-reconcile positions to match the broker before heartbeat check
    if engine.broker:
        try:
            positions = await engine.broker.positions()
            engine._reconcile_positions(positions)
            applog.info("routes", f"Successfully re-reconciled {len(positions)} positions from broker on manual health reset")
        except Exception as exc:
            applog.error("routes", f"Failed to reconcile positions from broker during reset: {exc}")

    # Reset reconciliation failure states
    engine.reconciliation_failed = False
    engine.reconciliation_mismatch = False
    engine.system_health = "NORMAL"
    engine.allow_new_entries = True

    # Trigger immediate heartbeat run to recheck health status
    await engine._check_system_heartbeat(utcnow())

    return {
        "status": "success",
        "system_health": engine.system_health,
        "allow_new_entries": engine.allow_new_entries
    }


class StartBody(BaseModel):
    mode: str = "demo"
    confirm: str = ""


@router.post("/engine/start")
async def engine_start(body: StartBody, request: Request) -> dict:
    engine = _engine(request)
    try:
        await engine.start(body.mode, confirm=body.confirm)
    except (PermissionError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return engine.status()


class StopBody(BaseModel):
    flatten: bool | None = None


@router.post("/engine/stop")
async def engine_stop(body: StopBody, request: Request) -> dict:
    engine = _engine(request)
    await engine.stop(flatten=body.flatten)
    return engine.status()


@router.post("/engine/reset-halt")
async def engine_reset_halt(request: Request) -> dict:
    engine = _engine(request)
    engine.reset_halt()
    return engine.status()


# ------------------------------------------------------------- watchlist --

class WatchlistBody(BaseModel):
    symbols: list[str]


@router.get("/watchlist")
async def get_watchlist(request: Request) -> dict:
    return {"symbols": _engine(request).watchlist}


@router.put("/watchlist")
async def put_watchlist(body: WatchlistBody, request: Request) -> dict:
    try:
        symbols = await _engine(request).set_watchlist(body.symbols)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"symbols": symbols}


# ------------------------------------------------------------------ data --

@router.get("/bars")
async def get_bars(request: Request, symbol: str, timeframe: str = "1Min", limit: int = 500) -> dict:
    engine = _engine(request)
    symbol = symbol.upper()
    limit = max(10, min(limit, 2000))
    data = await engine.ensure_data()
    df = data.frame(symbol, timeframe, limit=limit)
    if df.empty:
        try:
            df = await data.provider.get_bars(symbol, timeframe, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"data fetch failed: {exc}") from exc
    bars = [
        {
            "t": int(ts.timestamp()),
            "o": round(float(r["open"]), 4),
            "h": round(float(r["high"]), 4),
            "l": round(float(r["low"]), 4),
            "c": round(float(r["close"]), 4),
            "v": float(r["volume"]),
        }
        for ts, r in df.iterrows()
    ]
    return {"symbol": symbol, "timeframe": timeframe, "bars": bars}


@router.get("/quotes")
async def get_quotes(request: Request) -> list[dict]:
    return _engine(request).quotes_payload()


# --------------------------------------------------------------- trading --

@router.get("/positions")
async def get_positions(request: Request) -> list[dict]:
    engine = _engine(request)
    if engine.broker is None:
        return []
    try:
        return [p.to_payload() for p in await engine.broker.positions()]
    except Exception as exc:
        applog.warning("api", f"Failed to fetch broker positions: {exc}")
        return []



@router.post("/positions/{symbol}/close")
async def close_position(symbol: str, request: Request) -> dict:
    engine = _engine(request)
    try:
        order = await engine.close_symbol(symbol)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if order is None:
        raise HTTPException(status_code=404, detail=f"No open position in {symbol.upper()}")
    return order.model_dump(mode="json")


@router.post("/flatten")
async def flatten(request: Request) -> dict:
    engine = _engine(request)
    try:
        await engine.flatten()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/orders")
async def get_orders(request: Request) -> list[dict]:
    return _engine(request).orders_payload()


class ManualOrderBody(BaseModel):
    symbol: str
    side: OrderSide
    qty: float = Field(gt=0, le=100_000)
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = Field(default=None, gt=0)
    reason: str | None = Field(default=None, description="Operator reason for manual order")


@router.post("/orders")
async def submit_order(body: ManualOrderBody, request: Request) -> dict:
    engine = _engine(request)
    if body.order_type == OrderType.LIMIT and body.limit_price is None:
        raise HTTPException(status_code=400, detail="limit_price required for limit orders")
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    order_reason = f"manual order: {body.reason.strip()}" if body.reason and body.reason.strip() else "manual order"
    req = OrderRequest(
        symbol=body.symbol.upper(),
        side=body.side,
        qty=body.qty,
        order_type=body.order_type,
        limit_price=body.limit_price,
        strategy="manual",
        reason=order_reason,
    )
    try:
        order = await engine.manual_order(req)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return order.model_dump(mode="json")


@router.get("/trades")
async def get_trades(request: Request, limit: int = 200) -> list[dict]:
    engine = _engine(request)
    if engine.portfolio is not None and engine.portfolio.trades:
        live = [t.model_dump(mode="json") for t in reversed(engine.portfolio.trades[-limit:])]
        return live
    return _db(request).recent_trades(mode=engine.mode, limit=limit)


# --------------------------------------------------------- observability --

@router.get("/logs")
async def get_logs(request: Request, limit: int = 200) -> list[dict]:
    return [e.model_dump(mode="json") for e in applog.recent(limit)]


@router.get("/shadow-ledger")
async def get_shadow_ledger(request: Request, limit: int = 100, verdict: str = "all") -> dict:
    """Every rejected entry signal with full detail + forward-return resolution.

    verdict filter: all | MISSED_WINNER | AVOIDED_LOSER | NEUTRAL | NO_BARS
    """
    from app.core.shadow_ledger import shadow_ledger
    return shadow_ledger.query(limit=limit, verdict=verdict)


@router.post("/shadow-ledger/resolve")
async def resolve_shadow_ledger(request: Request) -> dict:
    """Force one forward-return resolution cycle now (normally runs every 60s)."""
    from app.core.shadow_ledger import shadow_ledger
    n = await shadow_ledger.resolve_pending(_engine(request).data)
    return {"resolved": n}


@router.get("/insights")
async def get_insights(request: Request, limit: int = 30) -> list[dict]:
    return [i.model_dump(mode="json") for i in _engine(request).insights.recent(limit)]


@router.get("/metrics")
async def get_metrics(request: Request) -> dict:
    return _engine(request).metrics_payload()


# ---------------------------------------------------------------- config --

@router.get("/settings")
async def get_settings_route(request: Request) -> dict:
    engine = _engine(request)
    s = engine.settings
    return {
        "mode": engine.mode,
        "risk": engine.risk_config.model_dump(),
        "data_provider": s.resolve_data_provider(engine.mode),
        "broker": s.broker,
        "live_trading_enabled": s.enable_live_trading,
        "has_alpaca_keys": s.has_alpaca_keys,
        "poll_interval_sec": s.poll_interval_sec,
        "paper_starting_cash": s.paper_starting_cash,
    }


class RiskConfigUpdateBody(BaseModel):
    """Whitelisted, typed partial risk-config update.

    A raw dict body previously forwarded arbitrary JSON into the engine's
    RiskConfig constructor; unknown keys surfaced as opaque 500s and there was
    no request-level contract. Field-level bounds are enforced by RiskConfig
    itself inside update_risk_config().
    """
    model_config = {"extra": "forbid"}

    entry_order_type: str | None = None
    risk_per_trade_pct: float | None = None
    max_position_pct: float | None = None
    max_open_positions: int | None = None
    max_daily_loss_pct: float | None = None
    daily_profit_lock_pct: float | None = None
    daily_profit_defend_pct: float | None = None
    max_drawdown_pct: float | None = None
    symbol_cooldown_minutes: int | None = None
    strategy_consecutive_loss_limit: int | None = None
    strategy_cooldown_minutes: int | None = None
    no_entry_minutes_before_close: int | None = None
    flatten_minutes_before_close: int | None = None
    min_price: float | None = None
    min_avg_dollar_volume: float | None = None
    data_gap_threshold_secs: float | None = None
    ml_confidence_threshold: float | None = None
    min_specialist_auc: float | None = None
    min_stop_pct: float | None = None
    score_threshold_trend: float | None = None
    score_threshold_range: float | None = None
    score_threshold_volatile: float | None = None
    score_threshold_event: float | None = None
    kelly_fraction_cap: float | None = None
    max_positions_per_sector: int | None = None
    max_correlation_penalty: float | None = None
    harvest_risk_multiplier: float | None = None
    scanner_watchlist_ttl: int | None = None
    max_daily_turnover_multiple: float | None = None


@router.put("/settings/risk")
async def put_risk(request: Request, body: RiskConfigUpdateBody) -> dict:
    partial = {k: v for k, v in body.model_dump().items() if v is not None}
    if not partial:
        raise HTTPException(status_code=400, detail="No valid risk fields supplied")
    try:
        cfg = _engine(request).update_risk_config(partial)
    except Exception as exc:  # noqa: BLE001 - pydantic validation detail
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return cfg.model_dump()


@router.get("/strategies")
async def get_strategies(request: Request) -> list[dict]:
    return [s.describe() for s in _engine(request).strategies]


class StrategyBody(BaseModel):
    enabled: bool | None = None
    params: dict | None = None


@router.put("/strategies/{name}")
async def put_strategy(name: str, body: StrategyBody, request: Request) -> dict:
    try:
        return _engine(request).update_strategy(name, body.enabled, body.params)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# -------------------------------------------------------------------- ai --

@router.get("/ai/model")
async def get_model(request: Request) -> dict:
    return _engine(request).model.info()


class TrainBody(BaseModel):
    symbols: list[str] | None = None
    days: int = Field(default=30, ge=2, le=365)


@router.post("/ai/train")
async def train_model(body: TrainBody, request: Request) -> dict:
    try:
        return await _engine(request).train_model(body.symbols, body.days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/ai/trainer/status")
async def get_trainer_status(request: Request) -> dict:
    engine = _engine(request)
    if engine.trainer is None:
        return {"training_in_progress": False, "last_trained": None, "last_result": {}}
    return engine.trainer.status()


@router.post("/ai/trainer/run")
async def run_trainer(request: Request) -> dict:
    """Manually trigger a continuous learning cycle on the current watchlist."""
    engine = _engine(request)
    if engine.trainer is None:
        try:
            data = await engine.ensure_data()
            from app.ai.trainer import ContinuousTrainer
            from app.config import get_models_dir
            engine.trainer = ContinuousTrainer(
                data,
                engine.model,
                get_models_dir(),
                engine.db.get_setting("strategies") or {},
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Trainer not initialized and failed to start: {exc}") from exc
    if engine.trainer.training_in_progress:
        raise HTTPException(status_code=409, detail="Training already in progress")
    await engine.trainer.trigger(engine.watchlist)
    return {"status": "started", "symbols": engine.watchlist}


# -------------------------------------------------------------- backtest --

# =============================================================================
# [MODIFIED 2026-10-14]: L50 - Pass BacktestBody starting_cash and slippage_bps to Replay
# ORIGINAL INTENT: BacktestBody lacked slippage_bps, and starting_cash was not forwarded to run_replay_fast_async.
# REASON FOR CHANGE: Ensure full contract compliance between API request and ReplayRuntime execution.
# SAFE ROLLBACK: The original code is removed; rollback via git history.
# -----------------------------------------------------------------------------
# [NEW CODE]:
class BacktestBody(BaseModel):
    symbols: list[str]
    days: int = Field(default=5, ge=1, le=90)
    starting_cash: float = Field(default=100_000.0, gt=0)
    slippage_bps: float = Field(default=0.0, ge=0.0)
    use_ml_gate: bool = True
    use_dynamic_scanner: bool = False
    ignore_temporal_leakage: bool = False
    debug_mode: bool = False
    strategies: dict[str, dict] | None = None
    start_date: str | None = None
    end_date: str | None = None
# =============================================================================


class ValidationBody(BaseModel):
    symbols: list[str] | None = None
    start_date: str | None = None
    end_date: str | None = None


@router.post("/backtest")
async def post_backtest(body: BacktestBody, request: Request) -> dict:
    """Run a historical backtest through the PRODUCTION REPLAY runtime.

    Single truth path: the request is served by ``ReplayRuntime``, which drives
    the authoritative ``TradingEngine._tick`` under deterministic clock/UUID/
    data bindings against an immutable DB+model snapshot. The legacy parallel
    orchestration loop (app/backtest/engine.py) was removed -- it duplicated
    live logic and had already drifted.

    Replay is fully isolated (own DB clone, own broker/data adapters), so it
    may run while the live engine is up; the lock only serializes heavy CPU.
    """
    hub = request.app.state.hub
    lock: asyncio.Lock = request.app.state.backtest_lock
    try:
        await asyncio.wait_for(lock.acquire(), timeout=1e-6)
    except TimeoutError:
        raise HTTPException(status_code=409, detail="A backtest/replay is already running")

    try:
        if body.start_date and body.end_date:
            start_date, end_date = body.start_date, body.end_date
        else:
            end_dt = utcnow() - timedelta(days=1)
            start_date = (end_dt - timedelta(days=body.days)).strftime("%Y-%m-%d")
            end_date = end_dt.strftime("%Y-%m-%d")

        from app.replay.runtime import ReplayRuntime
        runtime = ReplayRuntime(self_settings(request), _db(request))
        on_progress = lambda pct: hub.publish("backtest_progress", {"progress": round(pct / 100.0, 3)})  # noqa: E731
        runtime.session_manager.update_session = _wrap_progress(runtime.session_manager.update_session, on_progress)

        # =============================================================================
        # [MODIFIED 2026-10-14]: L50 - Pass starting_cash and slippage_bps to Replay Runtime
        # ORIGINAL INTENT: Hardcoded arguments omitted starting_cash and slippage_bps.
        # REASON FOR CHANGE: Ensure starting capital and slippage settings from request body bind to replay.
        # SAFE ROLLBACK: The original code is removed; rollback via git history.
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        replay = await runtime.run_replay_fast_async(
            start_date=start_date,
            end_date=end_date,
            symbols=[s.upper() for s in body.symbols] or None,
            warmup_bars=600,
            starting_cash=body.starting_cash,
            slippage_bps=getattr(body, "slippage_bps", 0.0),
        )
        # =============================================================================

        summary_in = replay.get("session", {})
        # Fast path returns day-shard results directly (validated byte-exact
        # against the continuous pass at window boundaries, 2026-08-23).
        trades = list(replay.get("trades", []))
        curve = [[p[0], p[1]] for p in replay.get("equity_curve", [])]
        wins = [t for t in trades if float(t.get("pnl") or 0) > 0]
        losses = [t for t in trades if float(t.get("pnl") or 0) <= 0]
        gp = sum(float(t.get("pnl") or 0) for t in wins)
        gl = abs(sum(float(t.get("pnl") or 0) for t in losses))
        eq0 = curve[0][1] if curve else None
        eqN = curve[-1][1] if curve else None
        metrics = {
            "total_trades": len(trades),
            "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
            "profit_factor": round(gp / gl, 3) if gl > 0 else None,
            "net_pnl": round(gp - gl, 2),
            "total_return_pct": round((eqN / eq0 - 1) * 100, 4) if eq0 and eqN else None,
        }
        payload = {
            "id": summary_in.get("replay_id"),
            "created_at": datetime.now(UTC).isoformat(),
            "params": {"symbols": body.symbols, "start_date": start_date, "end_date": end_date,
                       "engine": "replay_fast"},
            "metrics": metrics,
            "trades": trades,
            "equity_curve": curve,
            "manifest": replay.get("manifest", {}),
        }
        try:
            _db(request).save_backtest(payload["id"], datetime.now(UTC), metrics, payload)
        except Exception:
            pass
        hub.publish("backtest_progress", {"progress": 1.0})
        return payload
    finally:
        lock.release()


def self_settings(request: Request):
    return request.app.state.settings


def _wrap_progress(update_fn, on_progress):
    def wrapped(replay_id, **kwargs):
        pct = kwargs.get("progress_pct")
        if pct is not None:
            on_progress(pct)
        return update_fn(replay_id, **kwargs)
    return wrapped


@router.get("/backtests")
async def list_backtests(request: Request) -> list[dict]:
    return _db(request).list_backtests()


@router.get("/backtests/{run_id}")
async def get_backtest(run_id: str, request: Request) -> dict:
    payload = _db(request).get_backtest(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="backtest not found")
    return payload


@router.post("/validation/profit-optimization")
async def run_profit_validation_suite(body: ValidationBody, request: Request) -> dict:
    import sys
    from pathlib import Path
    lock: asyncio.Lock = request.app.state.backtest_lock
    if lock.locked():
        raise HTTPException(status_code=409, detail="A backtest or validation is already running")

    async with lock:
        # Build command-line arguments dynamically from request body
        args = ["run_profit_validation.py"]
        if body.start_date:
            args.extend(["--start", body.start_date])
        if body.end_date:
            args.extend(["--end", body.end_date])
        if body.symbols:
            args.extend(["--symbols", ",".join(body.symbols)])

        # Run run_profit_validation.py via subprocess to ensure clean process isolation
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err_msg = stderr.decode(errors="ignore") or stdout.decode(errors="ignore")
            raise HTTPException(status_code=500, detail=f"Validation failed: {err_msg}")

        report_path = Path("analysis_results.md")
        if report_path.exists():
            content = report_path.read_text(encoding="utf-8")
        else:
            content = "Validation completed, but analysis_results.md was not found."

        return {"status": "success", "report": content}


@router.post("/scanner/run")
async def run_scanner(request: Request) -> dict:
    try:
        new_watchlist = await _engine(request).run_market_scanner()
        return {"status": "success", "watchlist": new_watchlist}
    except Exception as exc:
        applog.error(
            "routes",
            f"Scanner endpoint error: {exc}",
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class ProfitAllocatorConfig(BaseModel):
    compound_capital_pct: float
    protected_reserve_pct: float
    research_capital_pct: float


@router.get("/strategies/profit-allocator")
async def get_profit_allocator(request: Request) -> dict:
    db = _db(request)
    config = db.get_setting("profit_allocator_config")
    if not config:
        config = {
            "compound_capital_pct": 50.0,
            "protected_reserve_pct": 30.0,
            "research_capital_pct": 20.0
        }

    engine = _engine(request)
    total_pnl = db.get_total_realized_pnl(mode=engine.mode)

    cc_pct = float(config.get("compound_capital_pct", 50.0))
    pr_pct = float(config.get("protected_reserve_pct", 30.0))
    rc_pct = float(config.get("research_capital_pct", 20.0))

    return {
        "compound_capital_pct": cc_pct,
        "protected_reserve_pct": pr_pct,
        "research_capital_pct": rc_pct,
        "compound_capital_value": round(total_pnl * cc_pct / 100.0, 2),
        "protected_reserve_value": round(total_pnl * pr_pct / 100.0, 2),
        "research_capital_value": round(total_pnl * rc_pct / 100.0, 2),
        "total_realized_pnl": round(total_pnl, 2),
        "mode": engine.mode
    }


@router.post("/strategies/profit-allocator")
async def post_profit_allocator(body: ProfitAllocatorConfig, request: Request) -> dict:
    import math
    total_pct = body.compound_capital_pct + body.protected_reserve_pct + body.research_capital_pct
    if not math.isclose(total_pct, 100.0, rel_tol=1e-9):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid configuration: percentages must sum exactly to 100.0 (got {total_pct:.2f}%)."
        )

    if body.compound_capital_pct < 0.0 or body.protected_reserve_pct < 0.0 or body.research_capital_pct < 0.0:
        raise HTTPException(
            status_code=400,
            detail="Invalid configuration: percentages cannot be negative."
        )

    db = _db(request)
    config = {
        "compound_capital_pct": body.compound_capital_pct,
        "protected_reserve_pct": body.protected_reserve_pct,
        "research_capital_pct": body.research_capital_pct
    }
    db.set_setting("profit_allocator_config", config)

    engine = _engine(request)
    total_pnl = db.get_total_realized_pnl(mode=engine.mode)

    return {
        "status": "success",
        "message": "Profit allocator configuration updated successfully.",
        "config": config,
        "allocations": {
            "compound_capital_value": round(total_pnl * body.compound_capital_pct / 100.0, 2),
            "protected_reserve_value": round(total_pnl * body.protected_reserve_pct / 100.0, 2),
            "research_capital_value": round(total_pnl * body.research_capital_pct / 100.0, 2),
            "total_realized_pnl": round(total_pnl, 2)
        }
    }

