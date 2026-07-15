import sys
from datetime import datetime, timezone
from urllib.error import URLError

import pytest

import scripts.validate_engineering_ledger as ledger
from scripts.validate_engineering_ledger import LedgerError, parse_events, validate

HEAD = "a" * 40
NOW = datetime(2026, 7, 15, 7, 0, tzinfo=timezone.utc)


def comment(body, comment_id=1):
    return {"id": comment_id, "body": "```yaml\n" + body + "\n```"}


def claim(run="2026-07-15T06:40:00Z", lease="2026-07-15T07:35:00Z", head=HEAD, role="lead"):
    return comment(f"""CLAIM
worker-run: {run}
lease-until: {lease}
role: {role}
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


def test_role_array_fails_closed():
    with pytest.raises(LedgerError, match="SCHEMA_INVALID"):
        parse_events([claim(role="[lead, failure]")])


def test_timezone_offset_normalizes_for_lease_duration():
    parse_events([claim(run="2026-07-15T10:40:00+04:00", lease="2026-07-15T07:35:00Z")])


def test_tombstone_before_claim_does_not_terminate_later_claim():
    abort = comment("""ABORT
worker-run: 2026-07-15T06:40:00Z
role: lead
slice: warmup
reason: stale
surviving-defect: none
next-action: ready""", 1)
    later = claim()
    later["id"] = 2
    with pytest.raises(LedgerError, match="STATE_DIVERGENCE"):
        validate(parse_events([abort, later]), {"state:needs-validation"}, HEAD, NOW)


def test_api_failure_is_canonical_infra_failure(monkeypatch, capsys):
    def fail(path, token):
        raise URLError("rate limited")

    monkeypatch.setattr(ledger, "github", fail)
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(sys, "argv", ["validator", "--repo", "o/r"])
    assert ledger.main() == 2
    assert capsys.readouterr().out.splitlines()[:2] == ["WATCHDOG_INFRA_FAILURE", "code=INFRA_FAILURE"]
