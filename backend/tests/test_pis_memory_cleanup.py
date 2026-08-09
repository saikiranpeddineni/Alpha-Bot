from unittest.mock import Mock, patch

from app.pis.engine_adapter import PISEngineAdapter


def test_handle_position_closed_clears_position_scoped_pis_memory():
    service = Mock()
    db = Mock()
    adapter = PISEngineAdapter(service=service, mode="live")

    with patch("app.db.database.Database.active", return_value=db):
        adapter.handle_position_closed("aapl")

    service.close_brain.assert_called_once_with("AAPL")
    db.delete_pis_profit_memory.assert_called_once_with(mode="live", symbol="AAPL")


def test_invalid_position_clears_position_scoped_pis_memory():
    service = Mock()
    db = Mock()
    adapter = PISEngineAdapter(service=service, mode="paper")
    position = Mock(symbol="MSFT", qty=0, avg_entry_price=100.0)

    with patch("app.db.database.Database.active", return_value=db):
        try:
            import asyncio
            asyncio.run(adapter.evaluate_position_exit(position, None))
        except ValueError:
            pass
        else:
            raise AssertionError("Expected invalid position to raise ValueError")

    service.close_brain.assert_called_once_with("MSFT")
    db.delete_pis_profit_memory.assert_called_once_with(mode="paper", symbol="MSFT")
