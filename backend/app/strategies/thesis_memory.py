import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC

from app.core.domain import SignalAction

logger = logging.getLogger("alphadesk")


@dataclass
class ThesisAttempt:
    timestamp: datetime
    action: SignalAction
    price: float
    vwap: float
    atr: float


@dataclass
class ThesisState:
    symbol: str
    attempts: list[ThesisAttempt] = field(default_factory=list)


class ThesisMemory:
    """Tracks recent thesis attempts to prevent churning the same idea."""
    
    def __init__(self, memory_window_minutes: int = 45):
        self._memory_window_minutes = memory_window_minutes
        self._state: dict[str, ThesisState] = {}
        
    def _clean_old_attempts(self, symbol: str, now: datetime) -> None:
        if symbol not in self._state:
            return
            
        cutoff = now - timedelta(minutes=self._memory_window_minutes)
        state = self._state[symbol]
        state.attempts = [a for a in state.attempts if a.timestamp >= cutoff]
        if not state.attempts:
            del self._state[symbol]

    def record_attempt(
        self,
        symbol: str,
        action: SignalAction,
        price: float,
        vwap: float,
        atr: float,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(UTC)
        state = self._state.setdefault(symbol, ThesisState(symbol=symbol))
        state.attempts.append(
            ThesisAttempt(
                timestamp=now,
                action=action,
                price=price,
                vwap=vwap,
                atr=atr,
            )
        )
        self._clean_old_attempts(symbol, now)

    def is_valid_new_thesis(
        self,
        symbol: str,
        action: SignalAction,
        price: float,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        """
        Check if we are allowed to enter a new trade for this symbol/direction.
        Returns (is_valid, reason_if_invalid).
        """
        now = now or datetime.now(UTC)
        self._clean_old_attempts(symbol, now)
        
        state = self._state.get(symbol)
        if not state or not state.attempts:
            return True, ""
            
        # Get the most recent attempt for this symbol
        last_attempt = state.attempts[-1]
        
        # Rule 1: Direction lock. Don't flip direction within the memory window 
        # unless price has moved significantly. (A simple implementation just blocks flips).
        if last_attempt.action != action:
            return False, f"Direction lock active: prior {last_attempt.action.value} attempt at {last_attempt.timestamp.isoformat()}"
            
        # Rule 2: Re-entry barrier. Only re-enter if price is better (lower for longs, higher for shorts)
        # OR if price has moved at least 0.75 ATR away from the previous attempt.
        price_diff = abs(price - last_attempt.price)
        required_movement = 0.75 * max(last_attempt.atr, 1e-9)
        
        if action == SignalAction.ENTER_LONG:
            is_better_price = price < last_attempt.price
        else:
            is_better_price = price > last_attempt.price
            
        if not is_better_price and price_diff < required_movement:
            return False, f"Re-entry blocked: price {price} not better and didn't move 0.75 ATR ({required_movement:.2f}) from prior {last_attempt.price}"
            
        return True, ""
