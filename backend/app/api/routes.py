"""REST API.

Everything the dashboard does goes through these routes; the WebSocket at
/ws streams live updates. All state lives in app.state (engine, db, hub).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.backtest.engine import BacktestParams, BacktestResult, run_backtest
from app.core.domain import OrderRequest, OrderSide, OrderType, utcnow
from app.core.logging import applog

router = APIRouter(prefix="/api")
_promotion_lock = asyncio.Lock()


def _engine(request: Request):  # noqa: ANN202
    return request.app.state.engine


def _db(request: Request):  # noqa: ANN202
    return request.app.state.db


# ---------------------------------------------------------------- status --

@router.get("/status")
async def get_status(request: Request) -> dict:
    return _engine(request).status()


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
    return [p.to_payload() for p in await engine.broker.positions()]


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


@router.post("/orders")
async def submit_order(body: ManualOrderBody, request: Request) -> dict:
    engine = _engine(request)
    if body.order_type == OrderType.LIMIT and body.limit_price is None:
        raise HTTPException(status_code=400, detail="limit_price required for limit orders")
    req = OrderRequest(
        symbol=body.symbol.upper(),
        side=body.side,
        qty=body.qty,
        order_type=body.order_type,
        limit_price=body.limit_price,
        strategy="manual",
        reason="manual order",
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


@router.put("/settings/risk")
async def put_risk(request: Request, body: dict) -> dict:
    try:
        cfg = _engine(request).update_risk_config(body)
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
    return engine.trainer.status


@router.post("/ai/trainer/run")
async def run_trainer(request: Request) -> dict:
    """Manually trigger a continuous learning cycle on the current watchlist."""
    engine = _engine(request)
    if engine.trainer is None:
        try:
            data = await engine.ensure_data()
            from app.ai.trainer import ContinuousTrainer
            from app.config import MODELS_DIR
            engine.trainer = ContinuousTrainer(
                data,
                engine.model,
                MODELS_DIR,
                engine.db.get_setting("strategies") or {},
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Trainer not initialized and failed to start: {exc}") from exc
    if engine.trainer.training_in_progress:
        raise HTTPException(status_code=409, detail="Training already in progress")
    await engine.trainer.trigger(engine.watchlist)
    return {"status": "started", "symbols": engine.watchlist}


# -------------------------------------------------------------- backtest --

class BacktestBody(BaseModel):
    symbols: list[str]
    days: int = Field(default=5, ge=1, le=90)
    starting_cash: float = Field(default=100_000.0, gt=0)
    use_ml_gate: bool = True
    use_dynamic_scanner: bool = False
    ignore_temporal_leakage: bool = False
    debug_mode: bool = False
    strategies: dict[str, dict] | None = None
    start_date: str | None = None
    end_date: str | None = None


class ValidationBody(BaseModel):
    symbols: list[str] | None = None
    start_date: str | None = None
    end_date: str | None = None


@router.post("/backtest")
async def post_backtest(body: BacktestBody, request: Request) -> dict:
    """Run a historical backtest.

    Concurrency contract
    --------------------
    Only one backtest may execute at a time because ``run_backtest`` reads and
    temporarily mutates shared ``DataManager`` and ``SignalModel`` state.

    The guard uses a non-blocking ``asyncio.Lock.acquire()`` via
    ``asyncio.wait_for(..., timeout=0)`` so that the *check* and the
    *acquisition* are a single indivisible operation with no ``await`` gap
    between them.  A second concurrent request that arrives while the lock is
    held receives **409 Conflict** immediately rather than queuing and later
    running against potentially mutated engine state.
    """
    engine = _engine(request)
    hub = request.app.state.hub
    lock: asyncio.Lock = request.app.state.backtest_lock

    # Atomically attempt to acquire the lock without queuing.
    # timeout=0 means: succeed immediately if free, raise TimeoutError if not.
    try:
        await asyncio.wait_for(lock.acquire(), timeout=1e-6)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=409, detail="A backtest is already running")

    # Lock is now held by this coroutine. All shared engine state access
    # (ensure_data, model reads, run_backtest) happens inside this try/finally
    # so the lock is always released, even on exception.
    try:
        # ensure_data() is called inside the lock: no shared state is touched
        # before acquisition, eliminating the pre-lock shared-state race.
        data = await engine.ensure_data()

        if body.start_date and body.end_date:
            try:
                from datetime import timezone
                start = datetime.strptime(body.start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                end = datetime.strptime(body.end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Invalid date format (must be YYYY-MM-DD): {exc}") from exc
        else:
            end = utcnow()
            start = end - timedelta(days=body.days)

        from app.strategies.base import REGISTRY
        db_strats = engine.db.get_setting("strategies") or {}
        submitted = body.strategies or {}
        merged_strategies = {}
        for name in REGISTRY.keys():
            if name in submitted:
                merged_strategies[name] = submitted[name]
            elif name in db_strats:
                merged_strategies[name] = db_strats[name]
            else:
                default_enabled = False if name == "rsi2_reversion" else True
                merged_strategies[name] = {"enabled": default_enabled, "params": {}}

        params = BacktestParams(
            symbols=[s.upper() for s in body.symbols],
            start=start,
            end=end,
            starting_cash=body.starting_cash,
            use_ml_gate=body.use_ml_gate,
            use_dynamic_scanner=body.use_dynamic_scanner,
            ignore_temporal_leakage=body.ignore_temporal_leakage,
            debug_mode=body.debug_mode,
            strategies=merged_strategies,
            risk=engine.risk_config,
        )
        try:
            result: BacktestResult = await run_backtest(
                data,
                engine.model,
                params,
                on_progress=lambda frac: hub.publish("backtest_progress", {"progress": round(frac, 3)}),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            # --- FIX #69: Belt-and-suspenders assertion. run_backtest() deep-copies
            # engine.model and never writes to the live singleton, but we assert
            # and forcibly reset here to guarantee the invariant holds even if a
            # future refactor accidentally passes the singleton without copying.
            if getattr(engine.model, "backtest_start_time", None) is not None:
                applog.warning(
                    "backtest",
                    "BUG #69: engine.model.backtest_start_time was non-None after "
                    "run_backtest() returned — forcibly resetting to None to protect "
                    "live ML temporal gate.",
                )
                engine.model.backtest_start_time = None
        payload = result.model_dump(mode="json")
        summary = {
            "symbols": params.symbols,
            "days": body.days,
            "total_return_pct": result.metrics.get("total_return_pct"),
            "max_drawdown_pct": result.metrics.get("max_drawdown_pct"),
            "total_trades": result.metrics.get("total_trades"),
            "win_rate": result.metrics.get("win_rate"),
        }
        _db(request).save_backtest(result.id, result.created_at, summary, payload)
        hub.publish("backtest_progress", {"progress": 1.0})
        return payload
    finally:
        lock.release()


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
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/shadow-ledger")
async def get_shadow_ledger(request: Request, limit: int = 200) -> list[dict]:
    engine = _engine(request)
    return _db(request).get_shadow_signals(mode=engine.mode, limit=limit)


@router.get("/shadow-ledger/analytics")
async def get_shadow_ledger_analytics(request: Request) -> dict:
    engine = _engine(request)
    signals = _db(request).get_shadow_signals(mode=engine.mode, limit=5000)
    
    total_signals = len(signals)
    rejected_signals = [s for s in signals if s.get("final_decision") == "rejected"]
    accepted_signals = [s for s in signals if s.get("final_decision") == "accepted"]
    replayed_signals = [s for s in signals if s.get("replayed")]
    
    winning_replayed = [s for s in replayed_signals if (s.get("simulated_pnl") or 0.0) > 0.0]
    
    total_replayed = len(replayed_signals)
    win_rate = (len(winning_replayed) / total_replayed * 100.0) if total_replayed > 0 else 0.0
    
    avg_pnl = (sum((s.get("simulated_pnl") or 0.0) for s in replayed_signals) / total_replayed) if total_replayed > 0 else 0.0
    avg_mfe = (sum((s.get("mfe_pct") or 0.0) for s in replayed_signals) / total_replayed) if total_replayed > 0 else 0.0
    avg_mae = (sum((s.get("mae_pct") or 0.0) for s in replayed_signals) / total_replayed) if total_replayed > 0 else 0.0
    
    by_strategy = {}
    for s in signals:
        strat = s.get("strategy", "unknown")
        if strat not in by_strategy:
            by_strategy[strat] = {
                "total": 0,
                "rejected": 0,
                "accepted": 0,
                "replayed": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": 0.0,
                "total_mfe": 0.0,
                "total_mae": 0.0,
            }
        
        stat = by_strategy[strat]
        stat["total"] += 1
        dec = s.get("final_decision")
        if dec == "rejected":
            stat["rejected"] += 1
        elif dec == "accepted":
            stat["accepted"] += 1
            
        if s.get("replayed"):
            stat["replayed"] += 1
            pnl = s.get("simulated_pnl") or 0.0
            stat["total_pnl"] += pnl
            stat["total_mfe"] += s.get("mfe_pct") or 0.0
            stat["total_mae"] += s.get("mae_pct") or 0.0
            if pnl > 0:
                stat["wins"] += 1
            else:
                stat["losses"] += 1

    strategy_breakdown = {}
    for strat, data in by_strategy.items():
        rep = data["replayed"]
        strategy_breakdown[strat] = {
            "total_signals": data["total"],
            "rejected_signals": data["rejected"],
            "accepted_signals": data["accepted"],
            "replayed_signals": rep,
            "win_rate": round(data["wins"] / rep * 100.0, 2) if rep > 0 else 0.0,
            "avg_simulated_return": round(data["total_pnl"] / rep, 4) if rep > 0 else 0.0,
            "avg_mfe": round(data["total_mfe"] / rep, 4) if rep > 0 else 0.0,
            "avg_mae": round(data["total_mae"] / rep, 4) if rep > 0 else 0.0,
        }

    by_rejection = {}
    for s in signals:
        if s.get("final_decision") != "rejected":
            continue
        stage = s.get("rejection_stage") or "unknown"
        if stage not in by_rejection:
            by_rejection[stage] = {
                "count": 0,
                "replayed": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": 0.0,
                "total_mfe": 0.0,
                "total_mae": 0.0,
            }
        
        stat = by_rejection[stage]
        stat["count"] += 1
        if s.get("replayed"):
            stat["replayed"] += 1
            pnl = s.get("simulated_pnl") or 0.0
            stat["total_pnl"] += pnl
            stat["total_mfe"] += s.get("mfe_pct") or 0.0
            stat["total_mae"] += s.get("mae_pct") or 0.0
            if pnl > 0:
                stat["wins"] += 1
            else:
                stat["losses"] += 1

    rejection_breakdown = {}
    for stage, data in by_rejection.items():
        rep = data["replayed"]
        rejection_breakdown[stage] = {
            "count": data["count"],
            "replayed_count": rep,
            "win_rate": round(data["wins"] / rep * 100.0, 2) if rep > 0 else 0.0,
            "avg_simulated_return_if_traded": round(data["total_pnl"] / rep, 4) if rep > 0 else 0.0,
            "avg_mfe": round(data["total_mfe"] / rep, 4) if rep > 0 else 0.0,
            "avg_mae": round(data["total_mae"] / rep, 4) if rep > 0 else 0.0,
        }

    return {
        "summary": {
            "total_signals": total_signals,
            "rejected_signals": len(rejected_signals),
            "accepted_signals": len(accepted_signals),
            "replayed_signals": total_replayed,
            "win_rate_pct": round(win_rate, 2),
            "avg_simulated_return_pct": round(avg_pnl, 4),
            "avg_mfe_pct": round(avg_mfe, 4),
            "avg_mae_pct": round(avg_mae, 4),
        },
        "by_strategy": strategy_breakdown,
        "by_rejection_stage": rejection_breakdown,
    }


class PromoteRequest(BaseModel):
    policy_id: str


@router.get("/strategies/challengers")
async def get_challengers(request: Request) -> list[dict]:
    from app.strategies.challenger import get_active_challengers
    res = []
    for c in get_active_challengers():
        res.append({
            "policy_id": c.policy_id,
            "ml_threshold": c.ml_threshold,
            "risk_multiplier": getattr(c, "risk_multiplier", 1.0),
            "description": f"Challenger policy with threshold={c.ml_threshold}"
        })
    return res


@router.get("/strategies/shadow-performance")
async def get_shadow_performance(request: Request) -> list[dict]:
    db = _db(request)
    signals = db.get_shadow_signals(limit=5000)
    
    by_policy = {}
    for sig in signals:
        policy_id = sig.get("policy_id", "champion")
        if policy_id not in by_policy:
            by_policy[policy_id] = []
        by_policy[policy_id].append(sig)
        
    res = []
    for policy_id, sigs in by_policy.items():
        total_signals = len(sigs)
        accepted = [s for s in sigs if s.get("final_decision") == "pipeline_approved"]
        rejected = [s for s in sigs if s.get("final_decision") == "rejected"]
        
        trades = [s for s in sigs if s.get("replayed")]
        wins = [s for s in trades if (s.get("simulated_pnl") or 0.0) > 0.0]
        total_realized = sum((s.get("simulated_pnl") or 0.0) for s in trades)
        
        win_rate = (len(wins) / len(trades) * 100.0) if trades else 0.0
        avg_expectancy = (total_realized / len(trades)) if trades else 0.0
        
        res.append({
            "policy_id": policy_id,
            "total_signals": total_signals,
            "accepted_count": len(accepted),
            "rejected_count": len(rejected),
            "total_trades": len(trades),
            "win_rate": round(win_rate, 2),
            "total_realized_r": round(total_realized, 4),
            "average_expectancy": round(avg_expectancy, 4),
        })
    return res


@router.get("/strategies/nominees")
async def get_policy_nominees(request: Request) -> list[dict]:
    """Returns all pending Challenger promotion nominations.
    Nominations are generated by EOD reflection analysis when a Challenger satisfies
    statistical criteria. No production config is changed until POST /strategies/promote is called.
    """
    db = _db(request)
    return db.get_pending_nominees()


@router.post("/strategies/promote")
async def promote_policy(request: Request, body: PromoteRequest) -> dict:
    # 1. Bearer Authentication Guard
    from app.config import get_settings
    settings_obj = get_settings()
    if settings_obj.admin_token:
        auth_header = request.headers.get("Authorization")
        x_token = request.headers.get("X-Admin-Token")
        token = None
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]
        elif x_token:
            token = x_token

        if not token:
            raise HTTPException(status_code=401, detail="Authentication token required.")
        if token != settings_obj.admin_token:
            raise HTTPException(status_code=403, detail="Forbidden: Invalid authentication token.")

    # 2. Strict Input Schema Validation
    policy_id = body.policy_id
    if not isinstance(policy_id, str) or not policy_id.strip():
        raise HTTPException(status_code=400, detail="Invalid policy_id payload.")
    policy_id = policy_id.strip()

    if policy_id == "champion":
        raise HTTPException(status_code=400, detail="Champion is already production.")

    engine = _engine(request)
    db = _db(request)

    # 3. Serialization Lock
    async with _promotion_lock:
        # 4. Idempotency Check
        active_snapshot = db.get_setting("active_policy_snapshot")
        if active_snapshot and active_snapshot.get("policy_id") == policy_id:
            applog.info("engine.promotion", f"Idempotent request: policy {policy_id} is already Champion.")
            return {
                "status": "success",
                "message": f"Promoted policy {policy_id!r} successfully.",
                "config": {
                    "ml_threshold": active_snapshot["ml_threshold"],
                    "score_threshold_offset": float(active_snapshot.get("score_threshold_offset", 0.0)),
                },
                "snapshot": active_snapshot,
            }

        # 5. Verify Challenger Config Exists
        from app.strategies.challenger import get_active_challengers
        challengers = get_active_challengers()
        target_challenger = next((c for c in challengers if c.policy_id == policy_id), None)
        if not target_challenger:
            raise HTTPException(status_code=404, detail=f"Challenger policy {policy_id!r} not found in active configurations.")

        # 6. Verify Pending Nomination Exists
        pending_nominee = db.get_pending_nominee_for_policy(policy_id)
        if not pending_nominee:
            raise HTTPException(status_code=400, detail=f"No pending nomination found for Challenger {policy_id!r}.")

        import hashlib
        import json
        def get_config_fingerprint(snapshot_dict: dict) -> str:
            cleaned = {k: v for k, v in snapshot_dict.items() if k not in ("snapshot_at", "snapshot_version")}
            serialized = json.dumps(cleaned, sort_keys=True, default=str)
            return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]

        snapshot = target_challenger.to_snapshot()
        current_chall_fp = get_config_fingerprint(snapshot)
        if pending_nominee.get("config_fingerprint") != current_chall_fp:
            raise HTTPException(
                status_code=400,
                detail=f"Nomination configuration mismatch for {policy_id}. Challenger settings have changed since nomination."
            )

        # 7. Compare-And-Swap (CAS): Verify current Champion configuration fingerprint matches nomination expectation
        def get_champion_config_fingerprint(db_inst) -> str:
            snapshot_dict = db_inst.get_setting("active_policy_snapshot")
            if not snapshot_dict:
                snapshot_dict = {
                    "policy_id": "champion",
                    "ml_threshold": settings_obj.ml_confidence_threshold,
                    "score_threshold_offset": 0.0,
                    "risk_config": settings_obj.risk_config().model_dump()
                }
            return get_config_fingerprint(snapshot_dict)

        current_champ_fp = get_champion_config_fingerprint(db)
        if pending_nominee.get("champion_config_fingerprint") != current_champ_fp:
            raise HTTPException(
                status_code=409,
                detail="Conflict: Champion configuration has changed since this nomination was generated. Re-evaluation required."
            )

        # 8. Atomic / Compensating Transaction Setup
        # Capture pre-promotion DB values
        old_snapshot = db.get_setting("active_policy_snapshot")
        old_ml_threshold = db.get_setting("ml_threshold")
        old_risk_config = db.get_setting("risk_config")

        # Capture pre-promotion engine runtime state
        old_engine_risk_config = engine.risk_config
        old_pipeline_ml_threshold = getattr(engine.pipeline, "ml_threshold", None) if getattr(engine, "pipeline", None) else None
        old_evaluator_ml_threshold = getattr(engine.evidence_evaluator, "ml_threshold", None) if getattr(engine, "evidence_evaluator", None) else None
        old_trade_score_engine_config = getattr(engine.pipeline.trade_score_engine, "risk_config", None) if (getattr(engine, "pipeline", None) and getattr(engine.pipeline, "trade_score_engine", None)) else None

        try:
            # Apply DB writes
            db.set_setting("active_policy_snapshot", snapshot)
            db.set_setting("ml_threshold", snapshot["ml_threshold"])
            
            # Reset engine risk config back to base settings from the DB first (without prior offsets)
            stored_risk = db.get_setting("risk_config") or {}
            default_risk = engine.settings.risk_config().model_dump()
            merged_risk = {**default_risk, **stored_risk}
            
            # Apply the new offset to the merged base risk config in-memory
            from app.config import RiskConfig
            engine.risk_config = RiskConfig(**merged_risk)
            
            offset = float(snapshot.get("score_threshold_offset", 0.0))
            if offset != 0.0:
                engine.risk_config.score_threshold_event = max(0.0, engine.risk_config.score_threshold_event + offset)
                engine.risk_config.score_threshold_trend = max(0.0, engine.risk_config.score_threshold_trend + offset)
                engine.risk_config.score_threshold_range = max(0.0, engine.risk_config.score_threshold_range + offset)
                engine.risk_config.score_threshold_volatile = max(0.0, engine.risk_config.score_threshold_volatile + offset)
                
            # Synchronize risk manager and pipeline engines
            if engine.risk:
                engine.risk.config = engine.risk_config
                
            # Update running engine properties in-place from the snapshot
            pipeline = getattr(engine, "pipeline", None)
            if pipeline:
                pipeline.ml_threshold = snapshot["ml_threshold"]
                if hasattr(pipeline, "trade_score_engine") and pipeline.trade_score_engine is not None:
                    pipeline.trade_score_engine.risk_config = engine.risk_config
                
            evaluator = getattr(engine, "evidence_evaluator", None)
            if evaluator:
                evaluator.ml_threshold = snapshot["ml_threshold"]

            # Consume nomination
            db.mark_nomination_approved(policy_id, pending_nominee["nominated_at"])
            
            applog.info(
                "engine.promotion",
                f"SUCCESS: Promoted policy {policy_id!r} to Champion production state in-place. "
                f"Snapshot: {snapshot}"
            )
            
            return {
                "status": "success",
                "message": f"Promoted policy {policy_id!r} successfully.",
                "config": {
                    "ml_threshold": snapshot["ml_threshold"],
                    "score_threshold_offset": offset,
                },
                "snapshot": snapshot,
            }
        except Exception as exc:
            # Compensating Rollback - DB State
            db.set_setting("active_policy_snapshot", old_snapshot)
            db.set_setting("ml_threshold", old_ml_threshold)
            db.set_setting("risk_config", old_risk_config)

            # Compensating Rollback - Engine Runtime State
            engine.risk_config = old_engine_risk_config
            if engine.risk:
                engine.risk.config = old_engine_risk_config
            if getattr(engine, "pipeline", None):
                engine.pipeline.ml_threshold = old_pipeline_ml_threshold
                if getattr(engine.pipeline, "trade_score_engine", None):
                    engine.pipeline.trade_score_engine.risk_config = old_trade_score_engine_config
            if getattr(engine, "evidence_evaluator", None):
                engine.evidence_evaluator.ml_threshold = old_evaluator_ml_threshold

            applog.error("engine.promotion", f"FAILED: Rolled back promotion mutation for {policy_id}: {exc}")
            raise HTTPException(status_code=500, detail=f"Failed to promote policy: {exc}") from exc
