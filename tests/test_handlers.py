from unittest.mock import patch, MagicMock


def make_message(text="hello", user_id=123, chat_id=456, chat_type="private"):
    msg = MagicMock()
    msg.text = text
    msg.from_user.id = user_id
    msg.chat.id = chat_id
    msg.chat.type = chat_type
    msg.reply_to_message = None
    return msg


HANDLER_PATCHES = {
    "bot.handlers.should_respond": True,
    "bot.handlers.is_rate_limited": False,
    "bot.handlers.BOT_INFO": MagicMock(id=42, username="testbot"),
}


def test_handle_message_calls_ask_ai():
    with (
        patch("bot.handlers.should_respond", return_value=True),
        patch("bot.handlers.is_rate_limited", return_value=False),
        patch("bot.handlers.BOT_INFO", MagicMock(username="testbot")),
        patch("bot.handlers.ask_ai", return_value="AI reply") as mock_ask,
        patch("bot.handlers.send_reply") as mock_send,
        patch("bot.handlers.bot"),
    ):
        from bot.handlers import handle_message

        msg = make_message(text="hello")
        handle_message(msg)
        mock_ask.assert_called_once_with(123, "hello")
        mock_send.assert_called_once_with(msg, "AI reply")


def test_handle_message_skips_when_not_responding():
    with (
        patch("bot.handlers.should_respond", return_value=False),
        patch("bot.handlers.ask_ai") as mock_ask,
    ):
        from bot.handlers import handle_message

        handle_message(make_message())
        mock_ask.assert_not_called()


def test_handle_message_rate_limited():
    with (
        patch("bot.handlers.should_respond", return_value=True),
        patch("bot.handlers.is_rate_limited", return_value=True),
        patch("bot.handlers.BOT_INFO", MagicMock(username="testbot")),
        patch("bot.handlers.ask_ai") as mock_ask,
        patch("bot.handlers.bot") as mock_bot,
    ):
        from bot.handlers import handle_message

        handle_message(make_message())
        mock_ask.assert_not_called()
        mock_bot.send_message.assert_called_once()
        assert "daily limit" in mock_bot.send_message.call_args[0][1]


def test_handle_message_sends_generic_error():
    with (
        patch("bot.handlers.should_respond", return_value=True),
        patch("bot.handlers.is_rate_limited", return_value=False),
        patch("bot.handlers.BOT_INFO", MagicMock(username="testbot")),
        patch("bot.handlers.ask_ai", side_effect=Exception("API key invalid")),
        patch("bot.handlers.bot") as mock_bot,
    ):
        from bot.handlers import handle_message

        handle_message(make_message())
        error_msg = mock_bot.send_message.call_args[0][1]
        assert "Something went wrong" in error_msg
        assert "API key" not in error_msg


def test_handle_message_none_text_skipped():
    """Stickers/photos/edits arriving with text=None must NOT call ask_ai
    (would burn rate limit and AI quota for no reason)."""
    with (
        patch("bot.handlers.should_respond", return_value=True),
        patch("bot.handlers.is_rate_limited", return_value=False),
        patch("bot.handlers.BOT_INFO", MagicMock(username="testbot")),
        patch("bot.handlers.ask_ai") as mock_ask,
        patch("bot.handlers.send_reply") as mock_send,
        patch("bot.handlers.bot"),
    ):
        from bot.handlers import handle_message

        msg = make_message()
        msg.text = None
        handle_message(msg)
        mock_ask.assert_not_called()
        mock_send.assert_not_called()


def test_handle_message_mention_only_skipped():
    """In a group, '@testbot' alone strips to empty — don't call ask_ai."""
    with (
        patch("bot.handlers.should_respond", return_value=True),
        patch("bot.handlers.is_rate_limited", return_value=False),
        patch("bot.handlers.BOT_INFO", MagicMock(username="testbot")),
        patch("bot.handlers.ask_ai") as mock_ask,
        patch("bot.handlers.send_reply"),
        patch("bot.handlers.bot"),
    ):
        from bot.handlers import handle_message

        msg = make_message(text="@testbot")
        handle_message(msg)
        mock_ask.assert_not_called()


# ── /help ─────────────────────────────────────────────────────────────────────


def test_cmd_help_sends_dynamic_reply():
    """/help renders the command list in the bot's voice and sends it back to
    the requesting chat."""
    with (
        patch("bot.handlers.bot") as mock_bot,
        patch("bot.handlers.HF_SPACE_ID", ""),
        patch("bot.handlers.generate", return_value="⚽ My footy menu!") as mock_gen,
        patch("bot.handlers.keep_typing"),
    ):
        from bot.handlers import cmd_help

        cmd_help(make_message(user_id=123, chat_id=456))
        mock_bot.send_message.assert_called_once_with(456, "⚽ My footy menu!")
        # The real command names are handed to the AI so it can't invent them.
        prompt = mock_gen.call_args[0][1][1]["content"]
        assert "/start" in prompt and "/reset" in prompt
        assert "/model" not in prompt  # HF disabled


def test_cmd_help_is_history_safe():
    """/help must NOT use ask_ai (which would write the turn into the user's
    conversation history) — it uses a one-off stateless call instead."""
    with (
        patch("bot.handlers.bot"),
        patch("bot.handlers.generate", return_value="menu"),
        patch("bot.handlers.keep_typing"),
        patch("bot.handlers.ask_ai") as mock_ask,
    ):
        from bot.handlers import cmd_help

        cmd_help(make_message())
        mock_ask.assert_not_called()


def test_cmd_help_includes_model_command_when_hf_enabled():
    """When HF_SPACE_ID is set, /model is part of the command list passed
    to the AI."""
    with (
        patch("bot.handlers.bot"),
        patch("bot.handlers.HF_SPACE_ID", "fake/space"),
        patch("bot.handlers.generate", return_value="menu") as mock_gen,
        patch("bot.handlers.keep_typing"),
    ):
        from bot.handlers import cmd_help

        cmd_help(make_message())
        prompt = mock_gen.call_args[0][1][1]["content"]
        assert "/model" in prompt


def test_cmd_help_falls_back_when_ai_fails():
    """If the AI call raises, /help still sends the static command list."""
    with (
        patch("bot.handlers.bot") as mock_bot,
        patch("bot.handlers.HF_SPACE_ID", ""),
        patch("bot.handlers.generate", side_effect=RuntimeError("boom")),
        patch("bot.handlers.keep_typing"),
    ):
        from bot.handlers import cmd_help

        cmd_help(make_message())
        sent = mock_bot.send_message.call_args[0][1]
        assert "/start" in sent and "/about" in sent


# ── /about ────────────────────────────────────────────────────────────────────


def test_cmd_about_with_sqlite():
    """When SQLite is configured, /about should reference SQLite."""
    with (
        patch("bot.handlers.bot") as mock_bot,
        patch("bot.handlers.store", MagicMock()),
        patch("bot.handlers.HF_SPACE_ID", ""),
        patch("bot.handlers._dynamic_intro", return_value="INTRO"),
    ):
        from bot.handlers import cmd_about

        cmd_about(make_message())
        sent = mock_bot.send_message.call_args[0][1]
        assert "SQLite" in sent
        assert "stateless" not in sent


def test_cmd_about_includes_commit_sha_when_set():
    """When COMMIT_SHA is populated (worker booted inside a git repo),
    /about exposes a Version line so users can validate which commit is
    live."""
    with (
        patch("bot.handlers.bot") as mock_bot,
        patch("bot.handlers.store", MagicMock()),
        patch("bot.handlers.HF_SPACE_ID", ""),
        patch("bot.handlers.COMMIT_SHA", "abc1234"),
        patch("bot.handlers._dynamic_intro", return_value="INTRO"),
    ):
        from bot.handlers import cmd_about

        cmd_about(make_message())
        sent = mock_bot.send_message.call_args[0][1]
        assert "Version: abc1234" in sent


def test_cmd_about_omits_version_line_when_sha_unknown():
    """If git rev-parse failed at boot, the Version line is dropped
    entirely rather than showing 'unknown' — clearer for the user."""
    with (
        patch("bot.handlers.bot") as mock_bot,
        patch("bot.handlers.store", MagicMock()),
        patch("bot.handlers.HF_SPACE_ID", ""),
        patch("bot.handlers.COMMIT_SHA", ""),
        patch("bot.handlers._dynamic_intro", return_value="INTRO"),
    ):
        from bot.handlers import cmd_about

        cmd_about(make_message())
        sent = mock_bot.send_message.call_args[0][1]
        assert "Version" not in sent


def test_cmd_about_without_store():
    """When no backend is configured, /about must say stateless. Regression
    guard for the NameError that occurred when `store` was missing from
    bot.handlers' imports."""
    with (
        patch("bot.handlers.bot") as mock_bot,
        patch("bot.handlers.store", None),
        patch("bot.handlers.HF_SPACE_ID", ""),
        patch("bot.handlers._dynamic_intro", return_value="INTRO"),
    ):
        from bot.handlers import cmd_about

        cmd_about(make_message())
        sent = mock_bot.send_message.call_args[0][1]
        assert "stateless" in sent


def test_cmd_about_uses_dynamic_ai_intro():
    """/about asks the AI to introduce itself and embeds the reply, without
    touching conversation history."""
    with (
        patch("bot.handlers.bot") as mock_bot,
        patch("bot.handlers.store", MagicMock()),
        patch("bot.handlers.HF_SPACE_ID", ""),
        patch("bot.handlers.generate", return_value="⚽ I'm FootyOracle!") as mock_gen,
        patch("bot.handlers.keep_typing"),
        patch("bot.handlers.ask_ai") as mock_ask,
    ):
        from bot.handlers import cmd_about

        cmd_about(make_message())
        sent = mock_bot.send_message.call_args[0][1]
        assert "⚽ I'm FootyOracle!" in sent
        # _dynamic_intro sends the system prompt as a one-off, stateless call:
        # the system prompt is present and history (ask_ai) is never touched.
        messages = mock_gen.call_args[0][1]
        assert messages[0]["role"] == "system"
        mock_ask.assert_not_called()


def test_cmd_about_falls_back_when_ai_fails():
    """If the AI call raises, /about still renders with the static fallback."""
    with (
        patch("bot.handlers.bot") as mock_bot,
        patch("bot.handlers.store", MagicMock()),
        patch("bot.handlers.HF_SPACE_ID", ""),
        patch("bot.handlers.generate", side_effect=RuntimeError("boom")),
        patch("bot.handlers.keep_typing"),
    ):
        from bot.handlers import cmd_about, _FALLBACK_INTRO

        cmd_about(make_message())
        sent = mock_bot.send_message.call_args[0][1]
        assert _FALLBACK_INTRO in sent


# ── /model command ────────────────────────────────────────────────────────────


def _import_cmd_model_with_hf_enabled():
    """Re-import handlers module with HF_SPACE_ID set so cmd_model exists."""
    import importlib
    import bot.config
    import bot.handlers

    original = bot.config.HF_SPACE_ID
    bot.config.HF_SPACE_ID = "fake/space"
    # Also patch the import in handlers module (already imported via `from ... import HF_SPACE_ID`)
    bot.handlers.HF_SPACE_ID = "fake/space"
    importlib.reload(bot.handlers)
    cmd_model = getattr(bot.handlers, "cmd_model", None)
    # Restore
    bot.config.HF_SPACE_ID = original
    bot.handlers.HF_SPACE_ID = original
    return cmd_model


def test_cmd_model_no_args_shows_current():
    cmd_model = _import_cmd_model_with_hf_enabled()
    assert cmd_model is not None
    with (
        patch("bot.handlers.get_provider", return_value="main"),
        patch("bot.handlers.bot") as mock_bot,
    ):
        msg = make_message(text="/model")
        cmd_model(msg)
        sent = mock_bot.send_message.call_args[0][1]
        assert "Current provider: main" in sent
        assert "/model main" in sent
        assert "/model hf" in sent


def test_cmd_model_switch_to_hf():
    cmd_model = _import_cmd_model_with_hf_enabled()
    with (
        patch("bot.handlers.set_provider", return_value=True) as mock_set,
        patch("bot.handlers.bot") as mock_bot,
    ):
        msg = make_message(text="/model hf")
        cmd_model(msg)
        mock_set.assert_called_once_with(123, "hf")
        sent = mock_bot.send_message.call_args[0][1]
        assert "hf" in sent
        assert "Armenian" in sent


def test_cmd_model_switch_to_main():
    cmd_model = _import_cmd_model_with_hf_enabled()
    with (
        patch("bot.handlers.set_provider", return_value=True) as mock_set,
        patch("bot.handlers.bot") as mock_bot,
    ):
        msg = make_message(text="/model main")
        cmd_model(msg)
        mock_set.assert_called_once_with(123, "main")
        sent = mock_bot.send_message.call_args[0][1]
        assert "Main" in sent


def test_cmd_model_invalid_choice():
    cmd_model = _import_cmd_model_with_hf_enabled()
    with (
        patch("bot.handlers.set_provider") as mock_set,
        patch("bot.handlers.bot") as mock_bot,
    ):
        msg = make_message(text="/model bogus")
        cmd_model(msg)
        mock_set.assert_not_called()
        assert "Invalid" in mock_bot.send_message.call_args[0][1]


def test_cmd_model_redis_error_reports_failure():
    cmd_model = _import_cmd_model_with_hf_enabled()
    with (
        patch("bot.handlers.set_provider", return_value=False),
        patch("bot.handlers.bot") as mock_bot,
    ):
        msg = make_message(text="/model hf")
        cmd_model(msg)
        assert "Could not save" in mock_bot.send_message.call_args[0][1]


def test_cmd_model_not_registered_without_hf_space_id():
    """When HF_SPACE_ID is empty, cmd_model should not exist."""
    import importlib
    import bot.config
    import bot.handlers

    bot.config.HF_SPACE_ID = ""
    bot.handlers.HF_SPACE_ID = ""
    # reload() doesn't delete existing attributes, so clear it first
    if hasattr(bot.handlers, "cmd_model"):
        delattr(bot.handlers, "cmd_model")
    importlib.reload(bot.handlers)
    assert not hasattr(bot.handlers, "cmd_model")


# ── /quote, /fact, /compliment (AI) ─────────────────────────────────────────────


def test_cmd_quote_calls_ask_ai():
    with (
        patch("bot.handlers.ask_ai", return_value="Quote reply") as mock_ask,
        patch("bot.handlers.bot") as mock_bot,
    ):
        from bot.handlers import cmd_quote

        cmd_quote(make_message(user_id=123, chat_id=456))
        assert mock_ask.call_args[0][0] == 123
        mock_bot.send_message.assert_called_once_with(456, "Quote reply")


def test_cmd_fact_calls_ask_ai():
    with (
        patch("bot.handlers.ask_ai", return_value="Fact reply") as mock_ask,
        patch("bot.handlers.bot") as mock_bot,
    ):
        from bot.handlers import cmd_fact

        cmd_fact(make_message(user_id=123, chat_id=456))
        assert mock_ask.call_args[0][0] == 123
        mock_bot.send_message.assert_called_once_with(456, "Fact reply")


def test_cmd_compliment_calls_ask_ai():
    with (
        patch("bot.handlers.ask_ai", return_value="Compliment reply") as mock_ask,
        patch("bot.handlers.bot") as mock_bot,
    ):
        from bot.handlers import cmd_compliment

        cmd_compliment(make_message(user_id=123, chat_id=456))
        assert mock_ask.call_args[0][0] == 123
        mock_bot.send_message.assert_called_once_with(456, "Compliment reply")


# ── /roll (pure Python, no AI) ──────────────────────────────────────────────────


def test_cmd_roll_uses_random_not_ai():
    """/roll must be pure Python — never touch the AI."""
    with (
        patch("bot.handlers.ask_ai") as mock_ask,
        patch("bot.handlers.random.randint", return_value=4) as mock_rand,
        patch("bot.handlers.bot") as mock_bot,
    ):
        from bot.handlers import cmd_roll

        cmd_roll(make_message(chat_id=456))
        mock_ask.assert_not_called()
        mock_rand.assert_called_once_with(1, 6)
        assert "4" in mock_bot.send_message.call_args[0][1]


def test_cmd_roll_within_range():
    """A real roll always lands on 1-6."""
    with patch("bot.handlers.bot") as mock_bot:
        from bot.handlers import cmd_roll

        for _ in range(50):
            cmd_roll(make_message(chat_id=456))
            sent = mock_bot.send_message.call_args[0][1]
            value = int("".join(c for c in sent if c.isdigit()))
            assert 1 <= value <= 6


# ── /predict (football forecast) ─────────────────────────────────────────────


def test_cmd_predict_with_match_calls_ai():
    """/predict <match> runs a stateless AI forecast and sends the reply."""
    with (
        patch("bot.handlers._ask_once", return_value="Forecast reply") as mock_ask,
        patch("bot.handlers.bot") as mock_bot,
    ):
        from bot.handlers import cmd_predict

        cmd_predict(make_message(text="/predict Barcelona vs Real Madrid", user_id=123, chat_id=456))
        # user_id and chat_id are forwarded to _ask_once; the fixture text is in the prompt.
        assert mock_ask.call_args[0][0] == 123
        assert mock_ask.call_args[0][1] == 456
        assert "Barcelona vs Real Madrid" in mock_ask.call_args[0][2]
        mock_bot.send_message.assert_called_once_with(456, "Forecast reply")


def test_cmd_predict_without_match_shows_usage():
    """/predict with no fixture must prompt for usage and never hit the AI."""
    with (
        patch("bot.handlers._ask_once") as mock_ask,
        patch("bot.handlers.bot") as mock_bot,
    ):
        from bot.handlers import cmd_predict

        cmd_predict(make_message(text="/predict", chat_id=456))
        mock_ask.assert_not_called()
        assert "/predict" in mock_bot.send_message.call_args[0][1]


def test_handle_message_uses_keep_typing():
    """handle_message should wrap ask_ai in the keep_typing context."""
    with (
        patch("bot.handlers.should_respond", return_value=True),
        patch("bot.handlers.is_rate_limited", return_value=False),
        patch("bot.handlers.BOT_INFO", MagicMock(username="testbot")),
        patch("bot.handlers.ask_ai", return_value="reply"),
        patch("bot.handlers.send_reply"),
        patch("bot.handlers.keep_typing") as mock_keep,
        patch("bot.handlers.bot"),
    ):
        mock_keep.return_value.__enter__ = MagicMock(return_value=None)
        mock_keep.return_value.__exit__ = MagicMock(return_value=None)
        from bot.handlers import handle_message

        msg = make_message()
        handle_message(msg)
        mock_keep.assert_called_once_with(456)
