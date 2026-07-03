import json
from unittest.mock import patch


def test_get_history_empty():
    with patch("bot.history.store") as mock_store:
        mock_store.get.return_value = None
        from bot.history import get_history
        assert get_history(123) == []


def test_get_history_returns_parsed_data():
    history = [{"role": "user", "content": "Hello"}]
    with patch("bot.history.store") as mock_store:
        mock_store.get.return_value = json.dumps(history)
        from bot.history import get_history
        assert get_history(123) == history


def test_save_history_trims_to_max():
    with patch("bot.history.store") as mock_store:
        with patch("bot.history.MAX_HISTORY", 2):
            from bot.history import save_history
            long_history = [{"role": "user", "content": str(i)} for i in range(10)]
            save_history(123, long_history)
            saved = json.loads(mock_store.set.call_args[0][1])
            assert len(saved) == 2
            assert saved[-1]["content"] == "9"


def test_clear_history():
    with patch("bot.history.store") as mock_store:
        from bot.history import clear_history
        clear_history(123)
        mock_store.delete.assert_called_once_with("chat:123")


def test_save_history_sets_ttl():
    with patch("bot.history.store") as mock_store:
        from bot.history import save_history
        save_history(123, [{"role": "user", "content": "hi"}])
        call_kwargs = mock_store.set.call_args[1]
        assert "ex" in call_kwargs
        assert call_kwargs["ex"] == 2592000


# ── Graceful degradation ──────────────────────────────────────────────────────

def test_get_history_returns_empty_on_redis_error():
    with patch("bot.history.store") as mock_store:
        mock_store.get.side_effect = Exception("connection refused")
        from bot.history import get_history
        assert get_history(123) == []


def test_save_history_survives_redis_error():
    with patch("bot.history.store") as mock_store:
        mock_store.set.side_effect = Exception("connection refused")
        from bot.history import save_history
        save_history(123, [{"role": "user", "content": "hi"}])  # should not raise


def test_clear_history_survives_redis_error():
    with patch("bot.history.store") as mock_store:
        mock_store.delete.side_effect = Exception("connection refused")
        from bot.history import clear_history
        clear_history(123)  # should not raise
