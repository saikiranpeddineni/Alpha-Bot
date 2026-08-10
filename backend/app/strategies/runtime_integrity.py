"""Production-path integrity guards for dynamic market intelligence.

This module keeps research-only adaptive values out of the Champion execution
path and provides a compatibility view of the canonical market-risk field.
It intentionally does not introduce new thresholds, widen existing thresholds,
or add another trading gate.
"""

from __future__ import annotations

from functools import wraps


def install_runtime_integrity() -> None:
    """Install idempotent production-path compatibility fixes."""
    _install_safety_decision_alias()
    _install_champion_threshold_boundary()


def _install_safety_decision_alias() -> None:
    from app.core.domain import SafetyDecision

    if not hasattr(SafetyDecision, "market_state"):
        # PortfolioAIGate already supports a dynamic market-state input. The
        # engine's canonical SafetyDecision field is market_risk_state; expose
        # that same value under the portfolio-gate compatibility name instead of
        # inventing a second state source.
        setattr(
            SafetyDecision,
            "market_state",
            property(lambda self: self.market_risk_state),
        )


def _install_champion_threshold_boundary() -> None:
    from app.strategies.pipeline import SignalPipeline

    if getattr(SignalPipeline.process, "_champion_boundary_installed", False):
        return

    original_process = SignalPipeline.process

    @wraps(original_process)
    def champion_process(self, *args, **kwargs):
        # Contextual/adaptive thresholds are research evidence. The Champion
        # pipeline uses its configured runtime threshold, which can still be
        # changed by the existing policy/configuration machinery. No threshold
        # is widened or lowered here.
        kwargs["adaptive_ml_thresholds"] = None
        return original_process(self, *args, **kwargs)

    champion_process._champion_boundary_installed = True
    SignalPipeline.process = champion_process
