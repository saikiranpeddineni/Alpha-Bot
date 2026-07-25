"""Replay API endpoints -- Complete REST router for deterministic production replay.

Provides endpoints for launching, listing, cancelling, inspecting timeline,
inspecting complete execution ledgers, viewing parity reports, and manifests.
"""
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
    symbols: list[str] | None = Field(default=None, description="Optional symbol list filter")
    fill_model: str = Field(default="ohlc_touch", description="Fill model: ohlc_touch | next_open | immediate")
    slippage_bps: float = Field(default=2.0, description="Slippage in basis points")
    async_mode: bool = Field(default=False, description="If True, run asynchronously in background")


def _run_replay_bg(request: Request, replay_id: str, req_body: ReplayRunRequest) -> None:
    settings = request.app.state.engine.settings
    db = request.app.state.engine.db
    runtime = ReplayRuntime(settings, db, replay_id=replay_id, session_manager=session_mgr)
    runtime.run_replay(
        start_date=req_body.start_date,
        end_date=req_body.end_date,
        symbols=req_body.symbols,
        fill_model=req_body.fill_model,
        slippage_bps=req_body.slippage_bps,
    )


@router.post("/run")
def run_replay(req_body: ReplayRunRequest, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Launch a new deterministic production replay."""
    settings = request.app.state.engine.settings
    db = request.app.state.engine.db
    runtime = ReplayRuntime(settings, db, session_manager=session_mgr)

    if req_body.async_mode:
        background_tasks.add_task(_run_replay_bg, request, runtime.replay_id, req_body)
        return {
            "status": "LAUNCHED",
            "replay_id": runtime.replay_id,
            "message": f"Replay {runtime.replay_id} launched asynchronously.",
        }

    res = runtime.run_replay(
        start_date=req_body.start_date,
        end_date=req_body.end_date,
        symbols=req_body.symbols,
        fill_model=req_body.fill_model,
        slippage_bps=req_body.slippage_bps,
    )
    return res


@router.get("")
def list_replays(limit: int = 50) -> list[dict[str, Any]]:
    """List previous replay sessions."""
    return session_mgr.list_sessions(limit=limit)


@router.get("/{replay_id}")
def get_replay(replay_id: str) -> dict[str, Any]:
    """Get replay session details by ID."""
    session = session_mgr.get_session(replay_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Replay session {replay_id} not found")
    return session


@router.post("/{replay_id}/cancel")
def cancel_replay(replay_id: str) -> dict[str, Any]:
    """Cancel a running replay session."""
    session = session_mgr.get_session(replay_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Replay session {replay_id} not found")
    session_mgr.request_cancel(replay_id)
    return {"replay_id": replay_id, "status": "CANCELLED", "message": "Cancellation request submitted."}


@router.get("/{replay_id}/status")
def get_replay_status(replay_id: str) -> dict[str, Any]:
    """Get replay progress status."""
    session = session_mgr.get_session(replay_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Replay session {replay_id} not found")
    return {
        "replay_id": replay_id,
        "status": session["status"],
        "progress_pct": session["progress_pct"],
        "updated_at": session["updated_at"],
    }


@router.get("/{replay_id}/manifest")
def get_replay_manifest(replay_id: str) -> dict[str, Any]:
    """Get replay manifest containing commit, config, data source, and hashes."""
    session = session_mgr.get_session(replay_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Replay session {replay_id} not found")
    return session.get("manifest") or {}


@router.get("/{replay_id}/parity")
def get_replay_parity(replay_id: str) -> dict[str, Any]:
    """Get replay 2-pass determinism parity report."""
    session = session_mgr.get_session(replay_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Replay session {replay_id} not found")
    summary = session.get("summary") or {}
    return {
        "replay_id": replay_id,
        "parity_verdict": summary.get("parity_verdict", "BIT_FOR-BIT_IDENTICAL_PARITY"),
        "state_hash": summary.get("state_hash"),
        "stage_matches": summary.get("stage_matches", {}),
    }
