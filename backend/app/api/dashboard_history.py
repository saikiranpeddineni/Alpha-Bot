"""Durable dashboard history views.

The execution engine keeps bounded in-memory collections for runtime work.  The
operator dashboard must not treat those volatile collections as the historical
ledger: restarts would otherwise make Orders appear empty and Trades appear
truncated to the portfolio checkpoint.  These read-only endpoints use the
persisted SQLite ledger as the authority for dashboard history.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from app.db.database import OrderRow, TradeRow

router = APIRouter(prefix="/api")


def _mode(request: Request) -> str:
    return request.app.state.engine.mode


def durable_orders(db, mode: str, limit: int = 200) -> list[dict]:  # noqa: ANN001
    """Return newest persisted orders for one execution mode."""
    with db._session() as session:
        stmt = (
            select(OrderRow)
            .where(OrderRow.mode == mode)
            .order_by(OrderRow.ts.desc())
            .limit(limit)
        )
        rows = list(session.scalars(stmt))
    return [json.loads(row.payload) for row in rows]


def durable_trades(db, mode: str, limit: int = 300) -> list[dict]:  # noqa: ANN001
    """Return newest persisted closed trades for one execution mode."""
    return db.recent_trades(mode=mode, limit=limit)


@router.get("/orders")
async def get_durable_orders(
    request: Request,
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[dict]:
    return durable_orders(request.app.state.db, _mode(request), limit)


@router.get("/trades")
async def get_durable_trades(
    request: Request,
    limit: int = Query(default=300, ge=1, le=2000),
) -> list[dict]:
    return durable_trades(request.app.state.db, _mode(request), limit)
