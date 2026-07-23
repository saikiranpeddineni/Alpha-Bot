import pytest
import math
import pandas as pd
from app.core.canonical_evidence import CanonicalOutcomeResolver, CanonicalEvidence

class TestCanonicalTelemetryInvariants:
    def test_no_percentage_return_fallback_to_r(self):
        """Invariant: Percentage return (simulated_pnl) must NEVER fall back to R-multiple when stop_loss is missing."""
        record = {
            "symbol": "AMZN",
            "strategy": "orb",
            "direction": "long",
            "replayed": True,
            "simulated_pnl": 1.35, # 1.35% return
            "price": 240.0,
            "stop_loss": None, # missing stop loss
            "mfe_pct": 1.50,
            "mae_pct": 0.20
        }
        
        evidence = CanonicalOutcomeResolver.resolve(record)
        assert evidence.realized_r is None
        assert evidence.valid is False
        assert evidence.invalid_reason == "MISSING_CANONICAL_R"

    def test_valid_risk_distance_computes_exact_r(self):
        """Invariant: Valid initial risk distance computes exact R-multiple (pnl_dollar / dollar_risk)."""
        # For a $10,000 baseline account, a 1.5% return = $150.0 PnL
        # With 1.0% stop risk ($100.0 dollar risk), realized R = $150 / $100 = 1.50R
        record = {
            "symbol": "NOW",
            "strategy": "orb",
            "direction": "short",
            "replayed": True,
            "simulated_pnl": 150.0, # $150 PnL
            "price": 100.0,
            "stop_loss": 101.0, # 1.0% stop risk ($1.00 per share = $100 risk on $10k)
            "mfe_pct": 1.50,
            "mae_pct": 0.0
        }

        evidence = CanonicalOutcomeResolver.resolve(record)
        assert evidence.realized_r == pytest.approx(1.50)
        assert evidence.valid is True

    def test_sanity_invariant_realized_r_cannot_exceed_mfe_r(self):
        """Sanity Invariant: realized_r cannot exceed mfe_r + epsilon for the same path."""
        # Simulated record where realized_r > mfe_r
        record = {
            "symbol": "XLP",
            "strategy": "bb_squeeze",
            "direction": "short",
            "replayed": True,
            "realized_r": 15.0, # invalid 15R realized
            "mfe_pct": 0.10, # MFE is only 0.10% (0.10R)
            "price": 80.0,
            "stop_loss": 80.80
        }

        evidence = CanonicalOutcomeResolver.resolve(record)
        assert evidence.valid is False
        assert evidence.invalid_reason == "REALIZED_R_EXCEEDS_MFE_R"

    def test_canonical_episode_id_generation(self):
        """Invariant: Deterministic canonical episode ID generation independent of policy variant."""
        record = {
            "symbol": "NOW",
            "strategy": "orb",
            "direction": "short",
            "ts": "2026-07-23T13:36:54.981706Z"
        }

        ep_id = CanonicalOutcomeResolver.get_canonical_episode_id(record)
        assert ep_id == "NOW_orb_short_20260723_09_30" # 13:36 UTC = 09:36 ET -> 09:30 15m bucket
