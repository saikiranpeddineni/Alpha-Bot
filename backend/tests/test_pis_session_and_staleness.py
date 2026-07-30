from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.pis.session_context import session_context
from app.pis.staleness import assess_evidence_freshness


ET = ZoneInfo("America/New_York")


def test_opening_phase_is_identified():
    ctx = session_context(datetime(2026, 7, 30, 9, 45, tzinfo=ET))
    assert ctx.opening_phase
    assert ctx.minutes_from_open == 15


def test_closing_phase_is_identified():
    ctx = session_context(datetime(2026, 7, 30, 15, 45, tzinfo=ET))
    assert ctx.closing_phase
    assert ctx.minutes_to_close == 15


def test_stale_evidence_is_explicitly_rejected():
    now = datetime.now(timezone.utc)
    result = assess_evidence_freshness(evidence_time=now - timedelta(minutes=4), now=now)
    assert not result.fresh
    assert result.reason == "PIS_EVIDENCE_STALE"


def test_recent_completed_bar_is_fresh():
    now = datetime.now(timezone.utc)
    result = assess_evidence_freshness(evidence_time=now - timedelta(seconds=60), now=now)
    assert result.fresh
