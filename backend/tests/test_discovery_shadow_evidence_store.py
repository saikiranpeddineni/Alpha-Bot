import json

import pytest

from app.core.discovery_shadow_evidence_store import (
    append_shadow_session,
    decode_shadow_session,
    encode_shadow_corpus,
    encode_shadow_session,
    load_shadow_sessions,
)
from app.core.discovery_shadow_validation import ShadowSessionEvidence


def _row(session="2026-07-15T14:00:00Z"):
    return ShadowSessionEvidence(
        session=session,
        fixed_candidates=frozenset({"AAPL", "MSFT"}),
        dynamic_candidates=frozenset({"MSFT", "NVDA"}),
        fixed_qualified=frozenset({"MSFT"}),
        dynamic_qualified=frozenset({"MSFT", "NVDA"}),
        fixed_requests=3,
        dynamic_requests=2,
        fixed_latency_ms=12.5,
        dynamic_latency_ms=8.25,
        dynamic_missing=1,
        dynamic_stale=0,
    )


def test_codec_round_trip_is_deterministic():
    row = _row()
    encoded = encode_shadow_session(row)
    assert encoded == encode_shadow_session(row)
    assert decode_shadow_session(encoded) == row
    payload = json.loads(encoded)
    assert payload["schema_version"] == 1
    assert payload["fixed_candidates"] == ["AAPL", "MSFT"]


def test_append_and_load_preserve_complete_sessions(tmp_path):
    path = tmp_path / "evidence" / "shadow.jsonl"
    first = _row("s1")
    second = _row("s2")
    append_shadow_session(path, first)
    append_shadow_session(path, second)
    assert load_shadow_sessions(path) == (first, second)


def test_duplicate_session_append_fails_without_changing_corpus(tmp_path):
    path = tmp_path / "shadow.jsonl"
    row = _row("s1")
    append_shadow_session(path, row)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already recorded"):
        append_shadow_session(path, row)
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda payload: payload.update(schema_version=99), "unsupported"),
        (lambda payload: payload.update(extra=True), "fields do not match"),
        (lambda payload: payload.update(session=123), "session must be a string"),
        (lambda payload: payload.update(fixed_candidates=["AAPL", "AAPL"]), "duplicates"),
        (lambda payload: payload.update(dynamic_qualified=["OUTSIDE"]), "dynamic qualified"),
        (lambda payload: payload.update(fixed_requests=True), "non-negative integer"),
        (lambda payload: payload.update(dynamic_missing=1.5), "non-negative integer"),
        (lambda payload: payload.update(fixed_latency_ms=float("nan")), "finite non-negative"),
        (lambda payload: payload.update(dynamic_latency_ms=float("inf")), "finite non-negative"),
    ],
)
def test_malformed_or_semantically_invalid_rows_fail_closed(mutate, message):
    payload = json.loads(encode_shadow_session(_row()))
    mutate(payload)
    with pytest.raises(ValueError, match=message):
        decode_shadow_session(json.dumps(payload))


def test_encoder_rejects_non_finite_latency():
    row = _row()
    object.__setattr__(row, "fixed_latency_ms", float("nan"))
    with pytest.raises(ValueError, match="Out of range float values"):
        encode_shadow_session(row)


def test_one_bad_line_rejects_entire_corpus_with_line_number(tmp_path):
    path = tmp_path / "shadow.jsonl"
    path.write_text(encode_shadow_session(_row("s1")) + "\n{bad json}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        load_shadow_sessions(path)


def test_duplicate_sessions_in_existing_corpus_are_rejected(tmp_path):
    path = tmp_path / "shadow.jsonl"
    line = encode_shadow_session(_row("s1"))
    path.write_text(line + "\n" + line + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate shadow session at line 2"):
        load_shadow_sessions(path)


def test_encode_corpus_rejects_duplicate_session_identity():
    with pytest.raises(ValueError, match="unique"):
        encode_shadow_corpus([_row("s1"), _row("s1")])
