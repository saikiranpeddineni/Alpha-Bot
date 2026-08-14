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
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from app.core.domain import OrderRequest, Position
from app.pis.execution import PISExecutionAdapter
from app.pis.models import PISConfig, PISDecision, PositionObservation, ExitAction
from app.pis.service import PISService

logger = logging.getLogger(__name__)


class PISEngineAdapter:
    """Adapter bridging TradingEngine events and PISService."""

    def __init__(
        self,
        service: Optional[PISService] = None,
        config: Optional[PISConfig] = None,
        mode: str = "live",
    ) -> None:
        self.config = config or PISConfig()
        self.service = service or PISService(self.config)
        self.mode = mode

    async def evaluate_position_exit(
        self,
        position: Position,
        bar_df: Any,  # pandas DataFrame with high, low, close, volume, atr
        initial_stop: Optional[float] = None,
        take_profit: Optional[float] = None,
        current_stop: Optional[float] = None,
        timestamp: Optional[datetime] = None,
        news_sentiment: float = 0.0,
    ) -> Tuple[Optional[OrderRequest], PISDecision]:
        """Evaluate a position for discretionary exit and stop loss updates.

        Args:
            position: Engine Position domain object.
            bar_df: Completed price bar DataFrame.
            initial_stop: Initial risk stop loss price.
            take_profit: Optional target price.
            current_stop: Current broker protective stop.
            timestamp: Evaluation timestamp (defaults to UTC now).

        Returns:
            Tuple of (Optional[OrderRequest], PISDecision).
        """
        symbol = position.symbol.upper()

        # Priority 10: Handle zero quantity or invalid position
        if position.qty == 0 or position.avg_entry_price <= 0:
            self.service.close_brain(symbol)
            from app.db.database import Database
            db = Database.active()
            if db:
                db.delete_pis_profit_memory(mode=self.mode, symbol=symbol)
            raise ValueError(f"Invalid position state for PIS evaluation: qty={position.qty}, avg_entry={position.avg_entry_price}")

        # Extract latest bar price attributes safely
        try:
            latest = bar_df.iloc[-1]
            close_px = float(latest["close"])
            high_px = float(latest["high"])
            low_px = float(latest["low"])
            vol_px = float(latest.get("volume", 0.0))
            atr_px = float(latest.get("atr14", latest.get("atr", latest.get("atr_14", close_px * 0.02))))
            market_timestamp = latest.name if hasattr(latest, "name") and isinstance(latest.name, datetime) else latest.get("timestamp", None)
            if market_timestamp and isinstance(market_timestamp, str):
                market_timestamp = datetime.fromisoformat(market_timestamp.replace('Z', '+00:00'))
            bid_px = float(latest.get("bid", getattr(position, "bid", None) or close_px))
            ask_px = float(latest.get("ask", getattr(position, "ask", None) or close_px))
        except Exception as e:
            logger.warning(f"PIS bar extraction fallback for {symbol}: {e}")
            close_px = float(position.current_price or position.avg_entry_price)
            high_px = close_px
            low_px = close_px
            vol_px = 0.0
            atr_px = close_px * 0.02
            market_timestamp = None
            bid_px = None
            ask_px = None

        # Sanitize ATR and prices
        if math.isnan(atr_px) or math.isinf(atr_px) or atr_px <= 0:
            atr_px = max(0.01, close_px * 0.02)

        entry_px = float(position.avg_entry_price)
        cur_px = float(getattr(position, "current_price", None) or getattr(position, "market_price", None) or close_px)
        side = 1.0 if position.qty > 0 else -1.0

        # Handle missing initial stop loss (Priority 10)
        if initial_stop is None or math.isnan(initial_stop) or initial_stop <= 0:
            initial_stop = entry_px - (side * 2.0 * atr_px)

        risk_per_share = abs(entry_px - initial_stop)
        if risk_per_share <= 1e-8 or math.isnan(risk_per_share):
            risk_per_share = max(0.01, atr_px)

        r_multiple = (cur_px - entry_px) / risk_per_share * side
        if math.isnan(r_multiple) or math.isinf(r_multiple):
            r_multiple = 0.0

        eval_ts = timestamp or datetime.now(timezone.utc)

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

        # Intra-bar excursion metrics
        def _calc_r(px: float) -> float:
            r = (px - entry_px) / risk_per_share * side
            if math.isnan(r) or math.isinf(r):
                return 0.0
            return r

        bar_high_r = _calc_r(high_px)
        bar_low_r = _calc_r(low_px)
        bar_mae_r = bar_low_r if side > 0 else bar_high_r
        live_r = _calc_r(cur_px)

        # Build clean observation
        obs = PositionObservation(
            timestamp=eval_ts,
            symbol=symbol,
            side=side,
            qty=abs(float(position.qty)),
            entry_price=entry_px,
            current_price=cur_px,
            high=max(high_px, cur_px),
            low=min(low_px, cur_px),
            close=close_px,
            volume=vol_px,
            atr=atr_px,
            initial_stop=initial_stop,
            risk_per_share=risk_per_share,
            current_stop=current_stop,
            take_profit=take_profit,
            r_multiple=r_multiple,
            news_sentiment_score=news_sentiment,
            bar_high_r=bar_high_r,
            bar_low_r=bar_low_r,
            bar_mae_r=bar_mae_r,
            live_r=live_r,
            market_timestamp=market_timestamp or eval_ts,
            decision_timestamp=eval_ts,
            bid=bid_px,
            ask=ask_px,
            price_source="bar_close" if cur_px == close_px else "broker_position",
            strategy=getattr(position, "strategy", "engine") or "engine",
            candidate_id=getattr(position, "candidate_id", None),
            thesis_fingerprint=getattr(position, "thesis_fingerprint", None)
        )

        from app.db.database import Database
        from app.pis.profit_memory import ProfitMemoryState
        
        db = Database.active()
        
        restored_state = None
        mode = self.mode
        
        if db and self.service.get_brain(symbol) is None:
            # We are about to create a new brain, try to restore full state
            mem_dict = await asyncio.to_thread(db.get_pis_profit_memory, mode=mode, symbol=symbol)
            if mem_dict:
                restored_state = mem_dict

        # Dispatch through service
        decision = self.service.observe(obs, restored_state=restored_state)

        if db:
            brain = self.service.get_brain(symbol)
            if brain:
                await asyncio.to_thread(
                    db.save_pis_profit_memory,
                    mode=mode,
                    symbol=symbol,
                    payload=brain.serialize_state()
                )

        # Convert to order request if exit triggered
        order_req = PISExecutionAdapter.build_order_request(decision, obs)

        # Inject PIS tracking metrics into position
        metrics = getattr(position, "metrics", {})
        if decision.action in (ExitAction.FULL_EXIT, ExitAction.PARTIAL_EXIT):
            metrics["pis_exit_decision"] = decision.action.value
            metrics["pis_exit_reason"] = ",".join(r.value for r in decision.reason_codes)
        
        if "adverse_velocity" in decision.metadata:
            metrics["adverse_velocity"] = decision.metadata["adverse_velocity"]
            metrics["adverse_momentum"] = decision.metadata["adverse_momentum"]
            metrics["severe_adverse_override_triggered"] = decision.metadata["severe_adverse_override_triggered"]

        return order_req, decision

    def handle_position_closed(self, symbol: str) -> None:
        """Handle position close event from engine and retire position-scoped PIS memory."""
        symbol_key = symbol.upper()
        self.service.close_brain(symbol_key)
        from app.db.database import Database
        db = Database.active()
        if db:
            db.delete_pis_profit_memory(mode=self.mode, symbol=symbol_key)

    def get_telemetry(self) -> Dict[str, Any]:
        """Get service telemetry."""
        return self.service.get_telemetry()
