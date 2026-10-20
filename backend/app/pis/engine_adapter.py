"""PIS Engine Adapter.

Interfaces between TradingEngine and PISService. Handles broker reconciliation edge cases,
casing normalizations, missing data fallbacks, and order generation.

Broker-Only Adapter Invariant:
    PIS core has zero broker knowledge. This adapter converts engine/broker position structures
    into normalized PositionObservation objects and handles missing fields safely.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from app.core.domain import OrderRequest, Position
from app.pis.execution import PISExecutionAdapter
from app.pis.models import PISConfig, PISDecision, PositionObservation
from app.pis.service import PISService

logger = logging.getLogger(__name__)


class PISEngineAdapter:
    """Adapter bridging TradingEngine events and PISService."""

    def __init__(
        self,
        service: PISService | None = None,
        config: PISConfig | None = None,
    ) -> None:
        self.config = config or PISConfig()
        self.service = service or PISService(self.config)
        # Replay/backtest harnesses flip this off: excursion memory is
        # per-run state and persisting it per position-bar dominated runtime.
        self.persist_profit_memory: bool = True

    async def evaluate_position_exit(
        self,
        position: Position,
        bar_df: Any,  # pandas DataFrame with high, low, close, volume, atr
        initial_stop: float | None = None,
        take_profit: float | None = None,
        current_stop: float | None = None,
        timestamp: datetime | None = None,
        news_sentiment: float = 0.0,
        mode: str | None = None,
    ) -> tuple[OrderRequest | None, PISDecision]:
        """Evaluate a position for discretionary exit and stop loss updates.

        Args:
            position: Engine Position domain object.
            bar_df: Completed price bar DataFrame.
            initial_stop: Initial risk stop loss price.
            take_profit: Optional target price.
            current_stop: Current broker protective stop.
            timestamp: Evaluation timestamp (defaults to UTC now).
            mode: Operating mode (demo/paper/live) used to namespace persisted
                profit memory. Paper and live excursion histories must never
                contaminate each other.

        Returns:
            Tuple of (Optional[OrderRequest], PISDecision).
        """
        symbol = position.symbol.upper()

        # Priority 10: Handle zero quantity or invalid position
        if position.qty == 0 or position.avg_entry_price <= 0:
            self.service.close_brain(symbol)
            raise ValueError(f"Invalid position state for PIS evaluation: qty={position.qty}, avg_entry={position.avg_entry_price}")

        # Extract latest bar price attributes safely
        try:
            latest = bar_df.iloc[-1]
            close_px = float(latest["close"])
            high_px = float(latest["high"])
            low_px = float(latest["low"])
            vol_px = float(latest.get("volume", 0.0))
            atr_px = float(latest.get("atr14", latest.get("atr", latest.get("atr_14", close_px * 0.02))))
        except Exception as e:
            logger.warning(f"PIS bar extraction fallback for {symbol}: {e}")
            close_px = float(position.current_price or position.avg_entry_price)
            high_px = close_px
            low_px = close_px
            vol_px = 0.0
            atr_px = close_px * 0.02

        # Sanitize ATR and prices
        if math.isnan(atr_px) or math.isinf(atr_px) or atr_px <= 0:
            atr_px = max(0.01, close_px * 0.02)

        entry_px = float(position.avg_entry_price)
        cur_px = float(getattr(position, "current_price", None) or getattr(position, "market_price", None) or close_px)
        side = 1.0 if position.qty > 0 else -1.0

        # Handle missing initial stop loss (Priority 10)
        if initial_stop is None or math.isnan(initial_stop) or initial_stop <= 0:
            initial_stop = entry_px - (side * 2.0 * atr_px)

        # R geometry is defined by the actual initial stop.  Do not widen a tight
        # broker stop with an unrelated percentage floor: doing so changes every
        # downstream R calculation (peak, trough, targets, and thesis drift).
        risk_per_share = abs(entry_px - initial_stop)
        if math.isnan(risk_per_share) or math.isinf(risk_per_share) or risk_per_share <= 0.0:
            # A zero-width explicit stop cannot produce a valid PositionObservation.
            # Fall back to the same ATR-derived stop used for a missing stop rather
            # than inventing a different R denominator while retaining that stop.
            initial_stop = entry_px - (side * 2.0 * atr_px)
            risk_per_share = abs(entry_px - initial_stop)

        r_multiple = (cur_px - entry_px) / risk_per_share * side
        if math.isnan(r_multiple) or math.isinf(r_multiple):
            r_multiple = 0.0

        eval_ts = timestamp or datetime.now(UTC)

        # Sanitize prices against nan/inf
        def _clean_px(val: float, fallback: float) -> float:
            if val is None or math.isnan(val) or math.isinf(val) or val < 0:
                return fallback
            return float(val)

        close_px = _clean_px(close_px, entry_px)
        cur_px = _clean_px(cur_px, close_px)
        high_px = _clean_px(high_px, max(entry_px, cur_px, close_px))
        low_px = _clean_px(low_px, min(entry_px, cur_px, close_px))
        vol_px = max(0.0, _clean_px(vol_px, 0.0))
        atr_px = max(0.01, _clean_px(atr_px, 1.0))

        # =============================================================================
        # [MODIFIED 2026-10-14]: CR-07 - Pass Strategy & RSI2 Context to PIS
        # ORIGINAL INTENT: PositionObservation lacked strategy name and rsi2 indicator value.
        # REASON FOR CHANGE: Enable mean-reversion target hit recognition (RSI2 >= 70) in PIS exit planner.
        # SAFE ROLLBACK: The original code is removed; rollback via git history.
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        strat_name = str(getattr(position, "strategy", "") or "")
        rsi2_val = None
        try:
            if "rsi2" in bar_df.columns and pd.notna(latest.get("rsi2")):
                rsi2_val = float(latest["rsi2"])
        except Exception:
            rsi2_val = None

        obs = PositionObservation(
            timestamp=eval_ts,
            symbol=symbol,
            side=side,
            qty=abs(float(position.qty)),
            entry_price=entry_px,
            current_price=cur_px,
            # Keep bar extremes tied to the completed row.  ``cur_px`` may be a
            # fresher broker quote than the row and must not become lookahead
            # input for peak-R memory or runner protection.
            high=high_px,
            low=low_px,
            close=close_px,
            volume=vol_px,
            atr=atr_px,
            initial_stop=initial_stop,
            risk_per_share=risk_per_share,
            current_stop=current_stop,
            take_profit=take_profit,
            r_multiple=r_multiple,
            news_sentiment_score=news_sentiment,
            strategy=strat_name,
            rsi2=rsi2_val,
        )
        # =============================================================================

        from app.db.database import Database

        db = Database.active()

        restored_state = None
        # Mode-namespaced profit memory: paper and live excursion histories are
        # separate worlds. Falls back to "unknown" for standalone/test usage.
        mem_mode = mode or getattr(self, "mode", None) or "unknown"
        opened_at = getattr(position, "opened_at", None)
        entry_date = opened_at.astimezone(UTC).date().isoformat() if opened_at else None

        if db and self.persist_profit_memory and self.service.get_brain(symbol) is None:
            # About to create a new brain. Restore prior excursion memory only
            # when it belongs to this same position lifetime.  Older payloads
            # are date-bound; newer payloads also carry exact entry/side/stop
            # identity because broker position snapshots do not always expose
            # opened_at after a restart.
            mem_dict = await asyncio.to_thread(db.get_pis_profit_memory, mode=mem_mode, symbol=symbol)
            if isinstance(mem_dict, dict) and mem_dict:
                stored_entry_date = mem_dict.get("entry_date")
                has_position_identity = all(
                    key in mem_dict for key in ("entry_price", "side", "initial_stop")
                )
                same_position_lifetime = (
                    entry_date == stored_entry_date
                    if stored_entry_date is not None
                    else has_position_identity
                )
                if (
                    same_position_lifetime
                    and self._payload_matches_position(
                        mem_dict,
                        symbol=symbol,
                        entry_price=entry_px,
                        side=side,
                        initial_stop=initial_stop,
                    )
                ):
                    # The existing JSON payload store is deliberately reused.
                    # Passing the full mapping lets PositionBrain restore both
                    # legacy excursion memory and the durable runner/lifecycle
                    # state added to that payload.
                    restored_state = dict(mem_dict)

        # Dispatch through service
        decision = self.service.observe(obs, restored_memory=restored_state)

        if db and self.persist_profit_memory:
            brain = self.service.get_brain(symbol)
            if brain:
                state_payload = brain.serialize_state()
                state_payload.update(
                    {
                        "entry_date": entry_date,
                        "entry_price": entry_px,
                        "side": side,
                        "initial_stop": initial_stop,
                    }
                )
                await asyncio.to_thread(
                    db.save_pis_profit_memory,
                    mode=mem_mode,
                    symbol=symbol,
                    payload=state_payload,
                )

        # Convert to order request if exit triggered
        order_req = PISExecutionAdapter.build_order_request(decision, obs)

        return order_req, decision

    @staticmethod
    def _payload_matches_position(
        payload: dict[str, Any],
        *,
        symbol: str,
        entry_price: float,
        side: float,
        initial_stop: float,
    ) -> bool:
        """Reject persisted state that is clearly for another position.

        The pre-existing store is keyed by mode and symbol and historically only
        carried ``entry_date``.  New fields are checked when present, while old
        payloads remain restorable for the same position lifetime.
        """
        stored_symbol = payload.get("symbol")
        if stored_symbol is not None and str(stored_symbol).upper() != symbol:
            return False

        for key, expected in (
            ("entry_price", entry_price),
            ("side", side),
            ("initial_stop", initial_stop),
        ):
            if key not in payload:
                continue
            try:
                stored = float(payload[key])
            except (TypeError, ValueError):
                return False
            if not math.isfinite(stored) or not math.isclose(stored, expected, rel_tol=1e-9, abs_tol=1e-6):
                return False
        return True

    def handle_position_closed(self, symbol: str) -> None:
        """Handle position close event from engine."""
        self.service.close_brain(symbol.upper())

    def revert_scale_out(self, symbol: str) -> None:
        """Undo decision-time scale-out flags after a failed exit execution."""
        self.service.revert_scale_out(symbol.upper())

    def get_telemetry(self) -> dict[str, Any]:
        """Get service telemetry."""
        return self.service.get_telemetry()
