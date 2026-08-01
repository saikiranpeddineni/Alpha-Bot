"""REST API for deterministic production replay."""
from __future__ import annotations

from typing import Any
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

from app.replay.runtime import ReplayRuntime
from app.replay.session import ReplaySessionManager

router = APIRouter(prefix="/api/replay", tags=["replay"])
session_mgr = ReplaySessionManager()


class ReplayRunRequest(BaseModel):
    start_date: str = Field(..., description="Start date (YYYY-MM-DD)")
    end_date: str = Field(..., description="End date (YYYY-MM-DD)")
    symbols: list[str] | None = None
    fill_model: str = Field(default="ohlc_touch", pattern="^(ohlc_touch|next_open|immediate)$")
    slippage_bps: float = Field(default=0.0, ge=0.0, le=100.0)
    async_mode: bool = False


def _persist_result(replay_id: str, result: dict[str, Any]) -> None:
    session_mgr.save_artifact(replay_id, "manifest", result.get("manifest", {}))
    session_mgr.save_artifact(replay_id, "parity", result.get("parity", {}))
    ledger = result.get("ledger", {})
    session_mgr.save_artifact(replay_id, "ledger", ledger)
    session_mgr.save_artifact(replay_id, "timeline", ledger.get("timeline", []))


def _run_replay_bg(request: Request, replay_id: str, req: ReplayRunRequest) -> None:
    engine = request.app.state.engine
    runtime = ReplayRuntime(engine.settings, engine.db, replay_id=replay_id, session_manager=session_mgr)
    try:
        result = runtime.run_replay(req.start_date, req.end_date, req.symbols, req.fill_model, req.slippage_bps)
        _persist_result(replay_id, result)
    except Exception:
        # ReplayRuntime already updates session status to FAILED and logs full diagnostics
        pass


@router.post("/run")
def run_replay(req: ReplayRunRequest, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    engine = request.app.state.engine
    runtime = ReplayRuntime(engine.settings, engine.db, session_manager=session_mgr)
    if req.async_mode:
        background_tasks.add_task(_run_replay_bg, request, runtime.replay_id, req)
        return {"status": "LAUNCHED", "replay_id": runtime.replay_id}
    try:
        result = runtime.run_replay(req.start_date, req.end_date, req.symbols, req.fill_model, req.slippage_bps)
        _persist_result(runtime.replay_id, result)
        return result
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Replay execution failed: {exc}")


@router.get("")
def list_replays(limit: int = 50) -> list[dict[str, Any]]:
    return session_mgr.list_sessions(limit=max(1, min(limit, 200)))


def _session_or_404(replay_id: str) -> dict[str, Any]:
    session = session_mgr.get_session(replay_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Replay session {replay_id} not found")
    return session


@router.get("/{replay_id}")
def get_replay(replay_id: str) -> dict[str, Any]:
    return _session_or_404(replay_id)


@router.post("/{replay_id}/cancel")
def cancel_replay(replay_id: str) -> dict[str, Any]:
    _session_or_404(replay_id)
    session_mgr.request_cancel(replay_id)
    return {"replay_id": replay_id, "status": "CANCELLED"}


@router.delete("/{replay_id}")
def delete_replay(replay_id: str) -> dict[str, Any]:
    _session_or_404(replay_id)
    success = session_mgr.delete_session(replay_id)
    return {"replay_id": replay_id, "deleted": success}


@router.get("/{replay_id}/status")
def get_replay_status(replay_id: str) -> dict[str, Any]:
    s = _session_or_404(replay_id)
    return {"replay_id": replay_id, "status": s["status"], "progress_pct": s["progress_pct"], "updated_at": s["updated_at"]}


def _artifact_or_404(replay_id: str, name: str) -> Any:
    _session_or_404(replay_id)
    artifact = session_mgr.get_artifact(replay_id, name)
    if artifact is None:
        raise HTTPException(status_code=409, detail=f"Replay artifact {name} is not available yet")
    return artifact


@router.get("/{replay_id}/manifest")
def get_replay_manifest(replay_id: str) -> dict[str, Any]:
    artifact = session_mgr.get_artifact(replay_id, "manifest")
    return artifact if artifact is not None else (_session_or_404(replay_id).get("manifest") or {})


@router.get("/{replay_id}/parity")
def get_replay_parity(replay_id: str) -> dict[str, Any]:
    return _artifact_or_404(replay_id, "parity")


@router.get("/{replay_id}/ledger")
def get_replay_ledger(replay_id: str) -> dict[str, Any]:
    return _artifact_or_404(replay_id, "ledger")


@router.get("/{replay_id}/timeline")
def get_replay_timeline(replay_id: str) -> list[dict[str, Any]]:
    return _artifact_or_404(replay_id, "timeline")
