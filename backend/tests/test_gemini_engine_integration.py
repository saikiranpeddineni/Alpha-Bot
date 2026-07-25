import inspect
from pathlib import Path

from app.config import Settings
from app.core.engine import TradingEngine
from app.db.database import Database


def test_engine_execution_path_never_reads_shadow_rankings_or_scores():
    source = inspect.getsource(TradingEngine._tick)
    assert "gemini_shadow.ranker.rank" not in source
    assert "gemini_shadow.task" not in source
    assert ".rankings" not in source
    assert "shadow_score" not in source


def test_periodic_llm_narrative_spend_is_removed_from_engine():
    source = Path(inspect.getsourcefile(TradingEngine)).read_text()
    assert "narrative_interval" not in source
    assert "_narrative_task" not in source
    assert "_generate_narrative" not in source
    assert "llm_narrative(" not in source


def test_shadow_observation_persistence_is_bounded_and_telemetry_is_exposed(tmp_path):
    db = Database(tmp_path / "test.db")
    engine = TradingEngine(Settings(_env_file=None), db)
    for index in range(205):
        engine._persist_gemini_shadow_observation({"index": index})
    stored = db.get_setting("gemini_shadow_observations")
    assert len(stored) == 200
    assert stored[0] == {"index": 5}
    assert stored[-1] == {"index": 204}
    assert engine.status()["gemini_shadow"] == {
        "calls_today": 0,
        "candidates_ranked": 0,
        "cache_hits": 0,
        "failures": 0,
        "budget_remaining": 10,
    }
    db.close()
