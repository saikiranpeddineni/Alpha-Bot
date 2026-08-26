"""PIS Position Lifecycle State Machine.

Implements full 10-state lifecycle with explicit transition matrix enforcement,
illegal transition rejection, and state auditing.

Lifecycle States:
    NEW: Newly opened position, unconfirmed initial observations.
    DISCOVERY: Initial trade development, establishing initial R trajectory.
    VALIDATED: Position moving favorably, confirmed thesis validation.
    ACCELERATING: Strong trend momentum, approaching runner criteria.
    RUNNER: High-performance position qualified for extended profit capture.
    MATURING: Position reaching peak target zones or extended duration.
    WEAKENING: Declining momentum or initial thesis degradation detected.
    DISTRIBUTION: Heavy distribution, sharp giveback, or structural weakness.
    EXIT_CANDIDATE: Flagged for immediate partial or full discretionary exit.
    CLOSED: Position fully closed and finalized. Terminal state.
"""

from __future__ import annotations

from app.pis.models import LifecycleState


class InvalidLifecycleTransitionError(ValueError):
    """Raised when an illegal lifecycle state transition is attempted."""

    def __init__(self, current_state: LifecycleState, target_state: LifecycleState, reason: str = "") -> None:
        msg = f"Illegal lifecycle transition from '{current_state.value}' to '{target_state.value}'"
        if reason:
            msg += f": {reason}"
        super().__init__(msg)
        self.current_state = current_state
        self.target_state = target_state


class PositionLifecycleStateMachine:
    """Enforces explicit lifecycle state transition matrix for PIS positions."""

    # Explicit allowed transition matrix
    # State -> Set of allowed target states
    VALID_TRANSITIONS: dict[LifecycleState, set[LifecycleState]] = {
        LifecycleState.NEW: {
            LifecycleState.NEW,
            LifecycleState.DISCOVERY,
            LifecycleState.WEAKENING,
            LifecycleState.EXIT_CANDIDATE,
            LifecycleState.CLOSED,
        },
        LifecycleState.DISCOVERY: {
            LifecycleState.DISCOVERY,
            LifecycleState.VALIDATED,
            LifecycleState.WEAKENING,
            LifecycleState.EXIT_CANDIDATE,
            LifecycleState.CLOSED,
        },
        LifecycleState.VALIDATED: {
            LifecycleState.VALIDATED,
            LifecycleState.ACCELERATING,
            LifecycleState.MATURING,
            LifecycleState.WEAKENING,
            LifecycleState.EXIT_CANDIDATE,
            LifecycleState.CLOSED,
        },
        LifecycleState.ACCELERATING: {
            LifecycleState.ACCELERATING,
            LifecycleState.RUNNER,
            LifecycleState.MATURING,
            LifecycleState.WEAKENING,
            LifecycleState.EXIT_CANDIDATE,
            LifecycleState.CLOSED,
        },
        LifecycleState.RUNNER: {
            LifecycleState.RUNNER,
            LifecycleState.MATURING,
            LifecycleState.WEAKENING,
            LifecycleState.DISTRIBUTION,
            LifecycleState.EXIT_CANDIDATE,
            LifecycleState.CLOSED,
        },
        LifecycleState.MATURING: {
            LifecycleState.MATURING,
            LifecycleState.WEAKENING,
            LifecycleState.DISTRIBUTION,
            LifecycleState.EXIT_CANDIDATE,
            LifecycleState.CLOSED,
        },
        LifecycleState.WEAKENING: {
            LifecycleState.WEAKENING,
            LifecycleState.VALIDATED,
            LifecycleState.DISTRIBUTION,
            LifecycleState.EXIT_CANDIDATE,
            LifecycleState.CLOSED,
        },
        LifecycleState.DISTRIBUTION: {
            LifecycleState.DISTRIBUTION,
            LifecycleState.EXIT_CANDIDATE,
            LifecycleState.CLOSED,
        },
        LifecycleState.EXIT_CANDIDATE: {
            LifecycleState.EXIT_CANDIDATE,
            LifecycleState.CLOSED,
            # Can revert to weakening or validated if market recovers before execution
            LifecycleState.WEAKENING,
            LifecycleState.VALIDATED,
        },
        LifecycleState.CLOSED: {
            LifecycleState.CLOSED,  # Terminal state: only self-transition allowed
        },
    }

    def __init__(self, initial_state: LifecycleState = LifecycleState.NEW) -> None:
        self._current_state = initial_state
        self._history: list[tuple[LifecycleState, LifecycleState, str]] = []

    @property
    def current_state(self) -> LifecycleState:
        """Get current lifecycle state."""
        return self._current_state

    @property
    def history(self) -> list[tuple[LifecycleState, LifecycleState, str]]:
        """Get copy of state transition history."""
        return list(self._history)

    def can_transition_to(self, target_state: LifecycleState) -> bool:
        """Check if transition from current state to target state is legal."""
        allowed = self.VALID_TRANSITIONS.get(self._current_state, set())
        return target_state in allowed

    def transition_to(self, target_state: LifecycleState, reason: str = "") -> LifecycleState:
        """Attempt to transition to target_state.

        Args:
            target_state: Destination LifecycleState.
            reason: Optional description of trigger.

        Returns:
            The new LifecycleState.

        Raises:
            InvalidLifecycleTransitionError: If transition violates transition matrix.
        """
        if not self.can_transition_to(target_state):
            raise InvalidLifecycleTransitionError(
                current_state=self._current_state,
                target_state=target_state,
                reason=reason or "Transition not allowed by state matrix",
            )

        if self._current_state != target_state:
            self._history.append((self._current_state, target_state, reason))
            self._current_state = target_state

        return self._current_state

    @classmethod
    def validate_matrix_completeness(cls) -> bool:
        """Verify that every state in LifecycleState is present in transition matrix."""
        all_states = set(LifecycleState)
        defined_states = set(cls.VALID_TRANSITIONS.keys())
        missing = all_states - defined_states
        if missing:
            raise ValueError(f"Transition matrix missing states: {missing}")

        for source, targets in cls.VALID_TRANSITIONS.items():
            for t in targets:
                if t not in all_states:
                    raise ValueError(f"Invalid target state {t} from {source}")

        return True
