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
    strategy: str = ""
    candidate_id: str = ""
    thesis_fingerprint: str = ""
    status: str = "OPEN"
    pnl: float = 0.0
    r_multiple: float = 0.0


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
        strategy: str = "",
        candidate_id: str = "",
        thesis_fingerprint: str = "",
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
                strategy=strategy,
                candidate_id=candidate_id,
                thesis_fingerprint=thesis_fingerprint,
                status="OPEN"
            )
        )
        self._clean_old_attempts(symbol, now)
        
    def update_attempt_outcome(
        self,
        symbol: str,
        candidate_id: str,
        status: str,
        pnl: float,
        r_multiple: float
    ) -> None:
        state = self._state.get(symbol)
        if not state:
            return
        for attempt in reversed(state.attempts):
            if attempt.candidate_id == candidate_id or (not candidate_id and attempt.status == "OPEN"):
                attempt.status = status
                attempt.pnl = pnl
                attempt.r_multiple = r_multiple
                break

    def is_valid_new_thesis(
        self,
        symbol: str,
        action: SignalAction,
        price: float,
        reversal_confirmations: int = 0,
        now: datetime | None = None,
        thesis_fingerprint: str = "",
    ) -> tuple[bool, str]:
        """
        Check if we are allowed to enter a new trade for this symbol/direction.
        """
        now = now or datetime.now(UTC)
        self._clean_old_attempts(symbol, now)
        
        state = self._state.get(symbol)
        if not state or not state.attempts:
            return True, ""
            
        last_attempt = state.attempts[-1]
        age_minutes = (now - last_attempt.timestamp).total_seconds() / 60.0
        
        # Rule 1: Direction change
        if last_attempt.action != action:
            if last_attempt.status == "LOSS":
                if reversal_confirmations < 2:
                    return False, f"Direction change blocked: prior LOSS requires >=2 reversal confirmations (got {reversal_confirmations})"
            else:
                if reversal_confirmations < 1:
                    return False, f"Direction change blocked: reversal evidence is weak (got {reversal_confirmations})"
            return True, ""
            
        # Rule 2: Same direction re-entry
        if last_attempt.status == "LOSS":
            # Exact fingerprint match means it's the exact same thesis returning for another try.
            # Block it unconditionally unless it's a new setup.
            if thesis_fingerprint and thesis_fingerprint == last_attempt.thesis_fingerprint:
                return False, f"Re-entry blocked: exact same thesis fingerprint ({thesis_fingerprint}) already resulted in LOSS."

            # If the fingerprint is explicitly different, it's a new independent thesis.
            # We can bypass the price distance and cooldown restrictions.
            if thesis_fingerprint and last_attempt.thesis_fingerprint and thesis_fingerprint != last_attempt.thesis_fingerprint:
                return True, ""

            price_distance = abs(price - last_attempt.price)
            if price_distance < 0.75 * max(last_attempt.atr, 1e-9):
                return False, f"Re-entry blocked: price {price} didn't move 0.75 ATR from prior LOSS {last_attempt.price}"
        
        # Cooldown
        if thesis_fingerprint and last_attempt.thesis_fingerprint and thesis_fingerprint != last_attempt.thesis_fingerprint:
            # Independent new thesis bypasses symbol cooldown
            pass
        else:
            configured_symbol_reentry_cooldown = 15
            if age_minutes < configured_symbol_reentry_cooldown:
                return False, f"Re-entry blocked: cooldown active ({age_minutes:.1f}m < {configured_symbol_reentry_cooldown}m)"
            
        return True, ""
