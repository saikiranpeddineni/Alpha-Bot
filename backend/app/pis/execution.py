"""PIS Execution Helper.

Converts PISDecision outputs into structured engine OrderRequests.
PIS does not issue orders directly to brokers.
"""

from __future__ import annotations

from typing import Optional

from app.core.domain import OrderRequest, OrderSide, OrderType
from app.pis.models import ExitAction, PISDecision, PositionObservation


# Compatibility shim for older engine-exit code that still reads ``target_stop``.
# The canonical PISDecision field is ``target_stop_loss``. Keeping the alias here
# prevents a protection-update decision from becoming an AttributeError at the
# execution boundary while the rest of the engine migrates to the canonical name.
if not hasattr(PISDecision, "target_stop"):
    PISDecision.target_stop = property(lambda self: self.target_stop_loss)  # type: ignore[attr-defined]


class PISExecutionAdapter:
    """Helper converting PIS exit decisions into engine OrderRequest instances."""

    @staticmethod
    def build_order_request(
        decision: PISDecision,
        obs: PositionObservation,
    ) -> Optional[OrderRequest]:
        """Build engine OrderRequest from PISDecision.

        Returns:
            OrderRequest or None if action is HOLD or UPDATE_PROTECTION without a valid target.
        """
        order_side = OrderSide.SELL if obs.side > 0 else OrderSide.BUY
        reason_str = ",".join([r.value for r in decision.reason_codes])
        import math

        if decision.action == ExitAction.UPDATE_PROTECTION:
            target_stop = decision.target_stop_loss
            if target_stop is None:
                return None
            return OrderRequest(
                symbol=obs.symbol,
                side=order_side,
                order_type=OrderType.STOP,
                qty=abs(obs.qty),
                stop_price=target_stop,
                strategy=f"PISProtection[{decision.lifecycle_state.value}]",
                reason=f"PIS Protection Update: {reason_str}",
                role="exit",
            )

        if decision.action not in (ExitAction.PARTIAL_EXIT, ExitAction.FULL_EXIT):
            return None

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

        return OrderRequest(
            symbol=obs.symbol,
            side=order_side,
            order_type=OrderType.MARKET,
            qty=qty_to_close,
            strategy=f"PISDiscretionaryExit[{decision.lifecycle_state.value}]",
            reason=f"PIS Exit Reason: {reason_str}",
            role="exit",
        )
