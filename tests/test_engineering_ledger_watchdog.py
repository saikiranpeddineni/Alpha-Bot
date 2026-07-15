from datetime import datetime, timezone

import pytest

from scripts.validate_engineering_ledger import LedgerError, parse_events, validate

HEAD = "a" * 40
NOW = datetime(2026, 7, 15, 7, 0, tzinfo=timezone.utc)


def comment(body, comment_id=1):
    return {"id": comment_id, "body": "```yaml\n" + body + "\n```"}


def claim(run="2026-07-15T06:40:00Z", lease="2026-07-15T07:35:00Z", head=HEAD):
    return comment(f"""CLAIM
worker-run: {run}
lease-until: {lease}
role: lead
work-item: 1
slice: warmup
files:
  - x.py
state: patching
head: {head}""")


def handoff(run="2026-07-15T06:40:00Z"):
    return comment(f"""HANDOFF
worker-run: {run}
role: lead
slice: warmup
action-taken: fixed
test-commands:
  - pytest
test-results:
  - pass
surviving-defect: none
next-action: validation""", 2)


def check(comments, labels={"state:patching"}, head=HEAD):
    validate(parse_events(comments), labels, head, NOW)


def test_valid_claim():
    check([claim()])


def test_claim_handoff_terminates_lock():
    check([claim(), handoff()], {"state:needs-validation"})


def test_exact_55_minute_lease_required():
    with pytest.raises(LedgerError, match="SCHEMA_INVALID"):
        parse_events([claim(lease="2026-07-15T07:34:00Z")])


def test_expired_claim_fails():
    with pytest.raises(LedgerError, match="LEASE_ORPHANED"):
        check([claim(run="2026-07-15T06:00:00Z", lease="2026-07-15T06:55:00Z")])


def test_head_mismatch_fails():
    with pytest.raises(LedgerError, match="HEAD_MISMATCH"):
        check([claim(head="b" * 40)])


def test_overlap_fails():
    second = claim("2026-07-15T06:41:00Z", "2026-07-15T07:36:00Z")
    second["id"] = 3
    with pytest.raises(LedgerError, match="OVERLAPPING_ACTIVE_CLAIMS"):
        check([claim(), second])


def test_state_divergence_fails():
    with pytest.raises(LedgerError, match="STATE_DIVERGENCE"):
        check([claim()], {"state:ready"})


def test_unknown_key_fails_schema():
    bad = claim()
    bad["body"] = bad["body"].replace("state: patching", "state: patching\nextra: bad")
    with pytest.raises(LedgerError, match="SCHEMA_INVALID"):
        parse_events([bad])
