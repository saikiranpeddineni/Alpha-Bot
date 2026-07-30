from app.pis.promotion import PromotionMetrics, evaluate_promotion
from app.pis.shadow_compare import compare_exits


def test_pis_cannot_promote_on_small_sample():
    decision = evaluate_promotion(PromotionMetrics(50, 0.4, 12.0, -20.0, 0.0))
    assert not decision.promote
    assert "PIS_PROMOTION_INSUFFICIENT_SAMPLE" in decision.reasons


def test_pis_cannot_trade_more_drawdown_for_apparent_profit():
    decision = evaluate_promotion(PromotionMetrics(500, 0.3, 10.0, -15.0, 0.8))
    assert not decision.promote
    assert "PIS_PROMOTION_DRAWDOWN_REGRESSION" in decision.reasons


def test_strong_validated_improvement_can_promote():
    decision = evaluate_promotion(PromotionMetrics(500, 0.35, 18.0, -25.0, 0.1))
    assert decision.promote


def test_shadow_comparison_quantifies_saved_r():
    result = compare_exits(legacy_realized_r=1.0, pis_realized_r=4.0, future_mfe_r=5.0)
    assert result.realized_delta_r == 3.0
    assert result.missed_r_delta == -3.0
    assert result.pis_better
