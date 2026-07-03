"""Tests for the auto webhook registration helper (bot.clients.register_webhook).

These cover both the happy-path success message and the no-op + error
branches, since this runs at every PA worker boot and after every
/api/deploy — failures must never bubble up and crash the worker.
"""

from unittest.mock import patch


def test_register_webhook_skips_when_url_unset():
    with (
        patch("bot.config.WEBHOOK_URL", ""),
        patch("bot.config.WEBHOOK_SECRET", ""),
    ):
        from bot.clients import register_webhook

        msg = register_webhook()
        assert "skipped" in msg.lower() or "unset" in msg.lower()


def test_register_webhook_calls_set_webhook_with_url():
    with (
        patch("bot.config.WEBHOOK_URL", "https://example.com/api/webhook"),
        patch("bot.config.WEBHOOK_SECRET", ""),
        patch("bot.clients.bot") as mock_bot,
    ):
        from bot.clients import register_webhook

        msg = register_webhook()
        mock_bot.set_webhook.assert_called_once_with(
            url="https://example.com/api/webhook",
            max_connections=1,
        )
        assert "https://example.com/api/webhook" in msg


def test_register_webhook_includes_secret_when_set():
    with (
        patch("bot.config.WEBHOOK_URL", "https://example.com/api/webhook"),
        patch("bot.config.WEBHOOK_SECRET", "topsecret"),
        patch("bot.clients.bot") as mock_bot,
    ):
        from bot.clients import register_webhook

        register_webhook()
        kwargs = mock_bot.set_webhook.call_args.kwargs
        assert kwargs["secret_token"] == "topsecret"
        assert kwargs["max_connections"] == 1


def test_register_webhook_does_not_raise_on_failure():
    """Failures must never crash the worker — they're logged and swallowed
    (after the retries are exhausted)."""
    with (
        patch("bot.config.WEBHOOK_URL", "https://example.com/api/webhook"),
        patch("bot.config.WEBHOOK_SECRET", ""),
        patch("bot.clients.bot") as mock_bot,
        patch("bot.clients.time.sleep") as mock_sleep,
    ):
        mock_bot.set_webhook.side_effect = RuntimeError("Telegram down")
        from bot.clients import register_webhook

        msg = register_webhook()
        assert "fail" in msg.lower()
        assert mock_bot.set_webhook.call_count == 3
        assert mock_sleep.call_count == 2


def test_register_webhook_retries_through_proxy_blip():
    """PA's outbound proxy 503-blips transiently (seen live 2026-06-29:
    a boot-time registration failed and the bot ran on stale webhook
    state). One retry must ride it out and still report success."""
    with (
        patch("bot.config.WEBHOOK_URL", "https://example.com/api/webhook"),
        patch("bot.config.WEBHOOK_SECRET", ""),
        patch("bot.clients.bot") as mock_bot,
        patch("bot.clients.time.sleep"),
    ):
        mock_bot.set_webhook.side_effect = [RuntimeError("proxy 503"), True]
        from bot.clients import register_webhook

        msg = register_webhook()
        assert "registered" in msg.lower()
        assert mock_bot.set_webhook.call_count == 2


def test_register_webhook_rejects_http_scheme():
    """Telegram only accepts HTTPS webhooks. A misconfigured http:// URL
    should fail fast with a clear message, not a confusing Telegram error."""
    with (
        patch("bot.config.WEBHOOK_URL", "http://example.com/api/webhook"),
        patch("bot.config.WEBHOOK_SECRET", ""),
        patch("bot.clients.bot") as mock_bot,
    ):
        from bot.clients import register_webhook

        msg = register_webhook()
        assert "https" in msg.lower()
        mock_bot.set_webhook.assert_not_called()


def test_register_webhook_rejects_url_with_no_path():
    """A bare domain isn't a useful webhook — Telegram would still accept
    it but route to '/', which our Flask app doesn't handle."""
    with (
        patch("bot.config.WEBHOOK_URL", "https://example.com"),
        patch("bot.config.WEBHOOK_SECRET", ""),
        patch("bot.clients.bot") as mock_bot,
    ):
        from bot.clients import register_webhook

        msg = register_webhook()
        assert "path" in msg.lower()
        mock_bot.set_webhook.assert_not_called()


def test_register_webhook_reports_telegram_false_return():
    """If Telegram returns False (rare but documented), surface it
    instead of falsely reporting success."""
    with (
        patch("bot.config.WEBHOOK_URL", "https://example.com/api/webhook"),
        patch("bot.config.WEBHOOK_SECRET", ""),
        patch("bot.clients.bot") as mock_bot,
    ):
        mock_bot.set_webhook.return_value = False
        from bot.clients import register_webhook

        msg = register_webhook()
        assert "false" in msg.lower() or "fail" in msg.lower()
