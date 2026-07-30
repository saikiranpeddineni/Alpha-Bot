"""PIS Engine Adapter.

Interfaces between TradingEngine and PISService. Handles broker reconciliation edge cases,
casing normalizations, missing data fallbacks, and order generation.

Broker-Only Adapter Invariant:
    PIS core has zero broker knowledge. This adapter converts engine/broker position structures
    into normalized PositionObservation objects and handles missing fields safely.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from app.core.domain import OrderRequest, Position
from app.pis.execution import PISExecutionAdapter
from app.pis.models import PISConfig, PISDecision, PositionObservation
from app.pis.service import PISService

logger = logging.getLogger(__name__)


class PISEngineAdapter:
    """Adapter bridging TradingEngine events and PISService."""

    def __init__(
        self,
        service: Optional[PISService] = None,
        config: Optional[PISConfig] = None,
    ) -> None:
        self.config = config or PISConfig()
        self.service = service or PISService(self.config)

    def evaluate_position_exit(
        self,
        position: Position,
        bar_df: Any,  # pandas DataFrame with high, low, close, volume, atr
        initial_stop: Optional[float] = None,
        take_profit: Optional[float] = None,
        current_stop: Optional[float] = None,
        timestamp: Optional[datetime] = None,
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
            raise ValueError(f"Invalid position state for PIS evaluation: qty={position.qty}, avg_entry={position.avg_entry_price}")

        # Extract latest bar price attributes safely
        try:
            latest = bar_df.iloc[-1]
            close_px = float(latest["close"])
            high_px = float(latest["high"])
            low_px = float(latest["low"])
            vol_px = float(latest.get("volume", 0.0))
            atr_px = float(latest.get("atr", close_px * 0.02))  # Safe 2% ATR default fallback
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

        risk_per_share = abs(entry_px - initial_stop)
        if risk_per_share <= 1e-8 or math.isnan(risk_per_share):
            risk_per_share = max(0.01, atr_px)

        r_multiple = (cur_px - entry_px) / risk_per_share * side
        if math.isnan(r_multiple) or math.isinf(r_multiple):
            r_multiple = 0.0

        eval_ts = timestamp or datetime.now(timezone.utc)

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
            volume=max(0.0, vol_px),
            atr=atr_px,
            initial_stop=initial_stop,
            risk_per_share=risk_per_share,
            current_stop=current_stop,
            r_multiple=r_multiple,
        )

        # Dispatch through service
        decision = self.service.observe(obs)

        # Convert to order request if exit triggered
        order_req = PISExecutionAdapter.build_order_request(decision, obs)

        return order_req, decision

    def handle_position_closed(self, symbol: str) -> None:
        """Handle position close event from engine."""
        self.service.close_brain(symbol.upper())

    def get_telemetry(self) -> Dict[str, Any]:
        """Get service telemetry."""
        return self.service.get_telemetry()
