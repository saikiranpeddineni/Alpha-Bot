import pytest
import math
import pandas as pd
from app.core.canonical_evidence import CanonicalOutcomeResolver, CanonicalEvidence

class TestCanonicalTelemetryInvariants:
    def test_long_direction_valid_r_target_and_stop_hit(self):
        """Invariant 1: LONG direction valid R = (exit_price - entry_price) / (entry_price - stop_loss)."""
        # Entry = 100.0, Stop = 98.0 (Risk = 2.0). Target = 103.0 (+1.5R)
        record = {
            "symbol": "NOW",
            "strategy": "orb",
            "direction": "long",
            "replayed": True,
            "simulated_pnl": 3.0, # 3.0% return = +3.0 PnL on $100 entry
            "price": 100.0,
            "stop_loss": 98.0,
            "mfe_pct": 3.0,
            "mae_pct": 0.5
        }
        
        evidence = CanonicalOutcomeResolver.resolve(record)
        assert evidence.valid is True
        assert evidence.realized_r == pytest.approx(1.50)
        assert evidence.mfe == pytest.approx(3.0)

    def test_short_direction_valid_r_target_and_stop_hit(self):
        """Invariant 2: SHORT direction valid R = (entry_price - exit_price) / (stop_loss - entry_price)."""
        # Entry = 100.0, Stop = 102.0 (Risk = 2.0). Exit = 97.0 (+1.5R)
        record = {
            "symbol": "NOW",
            "strategy": "orb",
            "direction": "short",
            "replayed": True,
            "simulated_pnl": 3.0, # 3.0% return
            "price": 100.0,
            "stop_loss": 102.0,
            "mfe_pct": 3.0,
            "mae_pct": 0.5
        }

        evidence = CanonicalOutcomeResolver.resolve(record)
        assert evidence.valid is True
        assert evidence.realized_r == pytest.approx(1.50)

    def test_missing_stop_returns_none(self):
        """Invariant 3: Missing stop_loss produces realized_r = None (invalid, MISSING_CANONICAL_R)."""
        record = {
            "symbol": "AMZN",
            "strategy": "orb",
            "direction": "long",
            "replayed": True,
            "simulated_pnl": 1.35, # 1.35% return
            "price": 240.0,
            "stop_loss": None,
            "mfe_pct": 1.50,
            "mae_pct": 0.20
        }
        
        evidence = CanonicalOutcomeResolver.resolve(record)
        assert evidence.realized_r is None
        assert evidence.valid is False
        assert evidence.invalid_reason == "MISSING_CANONICAL_R"

    def test_zero_distance_stop_returns_none(self):
        """Invariant 4: Zero-distance stop (entry == stop) produces realized_r = None (invalid, ZERO_DISTANCE_STOP)."""
        record = {
            "symbol": "AMZN",
            "strategy": "orb",
            "direction": "long",
            "replayed": True,
            "simulated_pnl": 1.35,
            "price": 100.0,
            "stop_loss": 100.0,
            "mfe_pct": 1.50,
            "mae_pct": 0.20
        }

        evidence = CanonicalOutcomeResolver.resolve(record)
        assert evidence.realized_r is None
        assert evidence.valid is False
        assert evidence.invalid_reason == "ZERO_DISTANCE_STOP"

    def test_wrong_side_stop_long_returns_none(self):
        """Invariant 5: Wrong-side stop for LONG (stop >= entry) produces realized_r = None (invalid, WRONG_SIDE_STOP)."""
        record = {
            "symbol": "AMZN",
            "strategy": "orb",
            "direction": "long",
            "replayed": True,
            "simulated_pnl": 1.35,
            "price": 100.0,
            "stop_loss": 102.0, # Stop ABOVE entry for LONG -> invalid!
            "mfe_pct": 1.50,
            "mae_pct": 0.20
        }

        evidence = CanonicalOutcomeResolver.resolve(record)
        assert evidence.realized_r is None
        assert evidence.valid is False
        assert evidence.invalid_reason == "WRONG_SIDE_STOP"

    def test_wrong_side_stop_short_returns_none(self):
        """Invariant 6: Wrong-side stop for SHORT (stop <= entry) produces realized_r = None (invalid, WRONG_SIDE_STOP)."""
        record = {
            "symbol": "AMZN",
            "strategy": "orb",
            "direction": "short",
            "replayed": True,
            "simulated_pnl": 1.35,
            "price": 100.0,
            "stop_loss": 98.0, # Stop BELOW entry for SHORT -> invalid!
            "mfe_pct": 1.50,
            "mae_pct": 0.20
        }

        evidence = CanonicalOutcomeResolver.resolve(record)
        assert evidence.realized_r is None
        assert evidence.valid is False
        assert evidence.invalid_reason == "WRONG_SIDE_STOP"

    def test_sanity_invariant_realized_r_cannot_exceed_mfe_r(self):
        """Invariant 7: realized_r cannot exceed mfe_r + epsilon for the same path."""
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

    def test_setup_crossing_15m_boundary_episode_id(self):
        """Invariant 8: Canonical episode ID generation for setups crossing 15m window boundaries."""
        rec1 = {
            "symbol": "NOW",
            "strategy": "orb",
            "direction": "short",
            "ts": "2026-07-23T13:44:59.000000Z"
        }
        rec2 = {
            "symbol": "NOW",
            "strategy": "orb",
            "direction": "short",
            "ts": "2026-07-23T13:45:01.000000Z"
        }

        ep_id1 = CanonicalOutcomeResolver.get_canonical_episode_id(rec1)
        ep_id2 = CanonicalOutcomeResolver.get_canonical_episode_id(rec2)
        
        # 13:44:59 ET (09:44 ET) -> 09:30 bucket
        # 13:45:01 ET (09:45 ET) -> 09:45 bucket
        assert ep_id1 == "NOW_orb_short_20260723_09_30"
        assert ep_id2 == "NOW_orb_short_20260723_09_45"
        assert ep_id1 != ep_id2
