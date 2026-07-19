from __future__ import annotations
import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock


def _make_df(n=70, price=100.0):
    idx = pd.date_range("2024-01-15 09:30", periods=n, freq="1min", tz="America/New_York")
    rng = np.random.default_rng(0)
    prices = price + np.cumsum(rng.normal(0, 0.02, n))
    return pd.DataFrame({"open": prices, "high": prices+0.1, "low": prices-0.1,
                           "close": prices, "volume": 10000, "atr14": 0.15}, index=idx)


class TestA1SyntheticStop:
    def test_none_stop_null_r(self):
        from app.core.shadow_ledger import ShadowLedger
        res = ShadowLedger.replay_signal_60m(df=_make_df(), entry_idx=1,
            direction="long", stop_loss=None, take_profit=None, entry_price=100.0)
        assert res["stop_is_synthetic"] is True
        assert res["realized_r"] is None

    def test_tight_stop_not_synthetic(self):
        from app.core.shadow_ledger import ShadowLedger
        res = ShadowLedger.replay_signal_60m(df=_make_df(), entry_idx=1,
            direction="long", stop_loss=99.9, take_profit=None, entry_price=100.0)
        assert res["stop_is_synthetic"] is False
        assert res["realized_r"] is not None

    def test_forward_outcomes_synthetic_null_r(self):
        from app.core.shadow_ledger import ShadowLedger
        outcomes = ShadowLedger.calculate_forward_outcomes(_make_df(), 1, "long", None, 100.0)
        for _, o in outcomes.items():
            assert o["stop_is_synthetic"] is True
            assert o["mfe_r"] is None


class TestA9Persistence:
    def test_db_error_increments_counter(self):
        from app.core import shadow_ledger as sl
        before = sl._persistence_failure_count
        db = MagicMock()
        db.save_shadow_signal.side_effect = RuntimeError("down")
        df = _make_df()
        sl.ShadowLedger.record_candidate(db_engine=db, symbol="T",
            ts=df.index[1].to_pydatetime(), strategy="orb", direction="long",
            signal_price=100.0, stop_loss=99.0, take_profit=None,
            ml_confidence=0.8, final_decision="rejected",
            rejection_stage="ml_gate", rejection_reason="low_",
            df=df, spy_df=pd.DataFrame(), slot_rvol_val=None, vwap_telemetry={})
        assert sl._persistence_failure_count == before + 1


class TestB1MarginalAlpha:
    def test_single_veto_marginal(self):
        from app.strategies.evaluator import ParallelEvidenceEvaluator
        a = ParallelEvidenceEvaluator.calculate_veto_attribution({"ml_gate": True}, 2.0)
        assert a["marginal_alpha"]["ml_gate"] == pytest.approx(-2.0)

    def test_two_gate_shared_marginal_nonzero(self):
        from app.strategies.evaluator import ParallelEvidenceEvaluator
        a = ParallelEvidenceEvaluator.calculate_veto_attribution(
            {"ml_gate": True, "meta_label_gate": True}, 4.0)
        assert a["marginal_alpha"]["ml_gate"] == pytest.approx(-2.0)
        assert a["marginal_alpha"]["meta_label_gate"] == pytest.approx(-2.0)

    def test_three_gate_equal_share(self):
        from app.strategies.evaluator import ParallelEvidenceEvaluator
        a = ParallelEvidenceEvaluator.calculate_veto_attribution(
            {"ml_gate": True, "meta_label_gate": True, "trade_score_gate": True}, 3.0)
        for g in ["ml_gate", "meta_label_gate", "trade_score_gate"]:
            assert a["marginal_alpha"][g] == pytest.approx(-1.0)


class TestC3DecisionEnum:
    def test_both_accepted_values(self):
        from app.api.routes import ACCEPTED_DECISIONS
        assert "accepted" in ACCEPTED_DECISIONS
        assert "pipeline_approved" in ACCEPTED_DECISIONS
        sigs = [{"final_decision": "accepted"}, {"final_decision": "pipeline_approved"},
                {"final_decision": "rejected"}]
        accepted = [s for s in sigs if s["final_decision"] in ACCEPTED_DECISIONS]
        assert len(accepted) == 2
