from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.replay.market_feed import ReplayDataManager
from app.replay.runtime import ReplayRuntime


def _bar(symbol: str, ts: datetime, close: float) -> dict:
    return {
        "kind": "bar",
        "symbol": symbol,
        "ts_dt": ts,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 100.0,
        "in_replay_window": True,
    }


def test_replay_vix_is_carried_forward_asof_without_lookahead() -> None:
    start = datetime(2026, 7, 24, 13, 30, tzinfo=timezone.utc)
    bars = []
    for minute in range(61):
        ts = start + timedelta(minutes=minute)
        bars.append(_bar("SPY", ts, 600.0 + minute / 100.0))
        bars.append(_bar("QQQ", ts, 550.0 + minute / 100.0))
    bars.append(_bar("VIX", start, 18.0))
    bars.append(_bar("VIX", start + timedelta(minutes=60), 20.0))

    data = ReplayDataManager(bars)
    midpoint = start + timedelta(minutes=45)
    data.set_time(midpoint)

    vix = data.frame("VIX", "1Min", limit=1)
    assert not vix.empty
    assert vix.index[-1].to_pydatetime() == midpoint
    assert float(vix["close"].iloc[-1]) == 18.0
    assert float(vix["volume"].iloc[-1]) == 0.0

    # The future 20.0 observation must remain invisible until its timestamp.
    assert float(data.frame("VIX", "1Min")["close"].max()) == 18.0

    data.set_time(start + timedelta(minutes=60))
    assert float(data.frame("VIX", "1Min", limit=1)["close"].iloc[-1]) == 20.0


def test_snapshot_fingerprint_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.db"
    db_path.write_bytes(b"database-v1")
    models = tmp_path / "models"
    models.mkdir()
    (models / "signal_model.meta.json").write_text('{"version":1}', encoding="utf-8")
    specialists = models / "specialists"
    specialists.mkdir()
    (specialists / "spy.pkl").write_bytes(b"model-v1")

    first = ReplayRuntime._snapshot_fingerprint(db_path, models)
    second = ReplayRuntime._snapshot_fingerprint(db_path, models)
    assert first == second

    (specialists / "spy.pkl").write_bytes(b"model-v2")
    assert ReplayRuntime._snapshot_fingerprint(db_path, models) != first
