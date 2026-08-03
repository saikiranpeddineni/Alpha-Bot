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


def test_shadow_research_is_removed_from_engine_status(tmp_path):
    db = Database(tmp_path / "test.db")
    engine = TradingEngine(Settings(_env_file=None), db)
    status = engine.status()
    assert "shadow_research" not in status
    assert "gemini_shadow" not in status
    db.close()

