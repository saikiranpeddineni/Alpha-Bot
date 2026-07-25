from datetime import datetime, timedelta, timezone
from app.research.spawn.spawn_opportunity_intelligence import build_profiles, candidate_quality


def trade(i=0, side="short", symbol="MU", strategy="momentum", r=1.0, pnl=5.0, minutes=5):
    start = datetime(2026, 7, 24, 13, 30, tzinfo=timezone.utc) + timedelta(minutes=i * 10)
    return {"side":side,"symbol":symbol,"strategy":strategy,"entry_at":start.isoformat(),"exit_at":(start+timedelta(minutes=minutes)).isoformat(),"realized_r":r,"pnl":pnl,"entry_notional":100.0}


def test_small_samples_are_neutral_for_candidate_specialization():
    trades=[trade(i) for i in range(3)]
    score,_=candidate_quality(trades,side="short",symbol="MU",strategy="momentum",ts=datetime(2026,7,24,14,30,tzinfo=timezone.utc),evidence_trades=8)
    assert score == 0.0


def test_completed_profitable_evidence_creates_positive_quality():
    trades=[trade(i) for i in range(8)]
    score,profiles=candidate_quality(trades,side="short",symbol="MU",strategy="momentum",ts=datetime(2026,7,24,13,55,tzinfo=timezone.utc),evidence_trades=8)
    assert score > 0
    assert profiles["SIDE:SHORT"].trades == 8
    assert profiles["SIDE:SHORT"].expectancy_r == 1.0


def test_losing_evidence_is_negative_not_rewarded_by_fast_turnover():
    trades=[trade(i,r=-1.0,pnl=-5.0,minutes=1) for i in range(8)]
    score,_=candidate_quality(trades,side="short",symbol="MU",strategy="momentum",ts=datetime(2026,7,24,13,55,tzinfo=timezone.utc),evidence_trades=8)
    assert score < 0


def test_invalid_duration_does_not_inflate_efficiency():
    trades=[trade(i) for i in range(8)]
    for t in trades: t["exit_at"]="bad"
    profiles=build_profiles(trades,evidence_trades=8)
    assert profiles["SIDE:SHORT"].capital_minutes == 0.0
    assert profiles["SIDE:SHORT"].pnl_per_capital_minute == 0.0
