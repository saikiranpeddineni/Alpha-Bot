"""PIS Execution Helper.

Converts PISDecision outputs into structured engine OrderRequests.
PIS does not issue orders directly to brokers.
"""

from __future__ import annotations

from typing import Optional

from app.core.domain import OrderRequest, OrderSide, OrderType, utcnow
from app.pis.models import ExitAction, PISDecision, PositionObservation


class PISExecutionAdapter:
    """Helper converting PIS exit decisions into engine OrderRequest instances."""

    @staticmethod
    def build_order_request(
        decision: PISDecision,
        obs: PositionObservation,
    ) -> Optional[OrderRequest]:
        """Build engine OrderRequest from PISDecision.

        Args:
            decision: PISDecision.
            obs: PositionObservation.

        Returns:
            OrderRequest or None if action is HOLD or UPDATE_PROTECTION.
        """
        if decision.action not in (ExitAction.PARTIAL_EXIT, ExitAction.FULL_EXIT):
            return None

        # Determine side (opposite of position side)
        order_side = OrderSide.SELL if obs.side > 0 else OrderSide.BUY

        import math

        # Quantity to close
        if decision.action == ExitAction.FULL_EXIT or decision.exit_ratio >= 1.0:
            qty_to_close = abs(obs.qty)
        else:
            raw_qty = obs.qty * decision.exit_ratio
            if float(obs.qty).is_integer():
                qty_to_close = float(math.floor(raw_qty))
                if qty_to_close <= 0:
                    qty_to_close = min(1.0, float(obs.qty))
            else:
                qty_to_close = round(raw_qty, 4)
        if qty_to_close <= 0:
            return None

        reason_str = ",".join([r.value for r in decision.reason_codes])

        return OrderRequest(
            symbol=obs.symbol,
            side=order_side,
            order_type=OrderType.MARKET,
            qty=qty_to_close,
            strategy=f"PISDiscretionaryExit[{decision.lifecycle_state.value}]",
            reason=f"PIS Exit Reason: {reason_str}",
        )
