import os
import random
from datetime import datetime
from bot.clients import bot, BOT_INFO, store
from bot.config import COMMIT_SHA, HF_SPACE_ID, HOSTING_LABEL, MODEL, RATE_LIMIT, SYSTEM_PROMPT
from bot.ai import ask_ai
from bot.providers import generate
from bot.helpers import is_allowed, keep_typing, send_reply, should_respond
from bot.history import clear_history
from bot.preferences import get_provider, set_provider
from bot.rate_limit import is_rate_limited

# Verbose console logging for local dev and teaching. Enabled by
# BOT_VERBOSE_LOG=1 (run_local.py sets this automatically). Prints one
# line per inbound/outbound message so kids and teachers can see the
# conversation flow in their terminal while the bot is running.
VERBOSE_LOG = os.environ.get("BOT_VERBOSE_LOG", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def _log(message, direction: str, text: str) -> None:
    """Print a one-line trace of a message in verbose mode.

    direction is "in" (user → bot) or "out" (bot → user). Text is
    truncated to 500 characters so long AI replies don't flood the
    terminal. Newlines are collapsed for single-line readability.
    """
    if not VERBOSE_LOG:
        return
    user = message.from_user
    user_name = (
        f"@{user.username}" if user.username else (user.first_name or f"user:{user.id}")
    )
    bot_name = f"@{BOT_INFO.username}"
    snippet = (text or "").replace("\n", " ").replace("\r", " ")
    if len(snippet) > 500:
        snippet = snippet[:500] + "..."
    if direction == "in":
        sender, receiver = user_name, bot_name
    else:
        sender, receiver = bot_name, user_name
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {sender} → {receiver}: {snippet}", flush=True)


@bot.message_handler(commands=["start"], func=is_allowed)
def cmd_start(message):
    bot.send_message(
        message.chat.id,
        "👋 Hello! I'm your AI football predictor. ⚽\n\n"
        "🏆 Welcome to the ultimate Football AI Telegram Bot! Get instant answers to any football question — "
        "📊 player and team stats, 🔍 match analysis, and tournament insights. Receive smart match predictions 🔮, "
        "lineup analysis, and detailed comparisons powered by advanced AI 🤖."
        " Whether you're a passionate fan or a football expert, this bot is your all-in-one football assistant. 🥅\n\n"
        "👉 Use /help to see available commands.",
    )

@bot.message_handler(commands=["joke"], func = is_allowed)
def cmd_joke(message):
    reply = ask_ai(message.from_user.id, "Tell one short, your vibe joke.")
    bot.send_message(message.chat.id, reply)


@bot.message_handler(commands=["quote"], func=is_allowed)
def cmd_quote(message):
    reply = ask_ai(
        message.from_user.id,
        "Share one short, inspiring football quote. Attribute it if you can.",
    )
    bot.send_message(message.chat.id, reply)


@bot.message_handler(commands=["fact"], func=is_allowed)
def cmd_fact(message):
    reply = ask_ai(
        message.from_user.id,
        "Tell me one surprising football fact in a single sentence.",
    )
    bot.send_message(message.chat.id, reply)


@bot.message_handler(commands=["compliment"], func=is_allowed)
def cmd_compliment(message):
    reply = ask_ai(
        message.from_user.id,
        "Give me one short, warm compliment with a football flavour.",
    )
    bot.send_message(message.chat.id, reply)


@bot.message_handler(commands=["roll"], func=is_allowed)
def cmd_roll(message):
    # Pure Python — no AI. Rolls a standard six-sided die.
    result = random.randint(1, 6)
    bot.send_message(message.chat.id, f"🎲 You rolled a {result}!")

@bot.message_handler(commands=["help"], func=is_allowed)
def cmd_help(message):
    reply = _dynamic_help(message.from_user.id, message.chat.id)
    bot.send_message(message.chat.id, reply)

@bot.message_handler(commands=["roast"], func=is_allowed)
def cmd_roast(message):
 name = message.text.split(maxsplit=1)[1] if " " in message.text else "you"
 reply = ask_ai(message.from_user.id, f"Write a short, playful, friendly roast of {name}.")
 bot.send_message(message.chat.id, reply)

@bot.message_handler(commands=["remember"], func=is_allowed)
def cmd_remember(message):
 note = message.text.split(maxsplit=1)[1] if " " in message.text else ""
 store.set(f"note:{message.from_user.id}", note)
 bot.send_message(message.chat.id, "📝 Saved carefully! ✅")

@bot.message_handler(commands=["recall"], func=is_allowed)
def cmd_recall(message):
 # Mirror image of /remember — reads the stored note back.
 note = store.get(f"note:{message.from_user.id}") if store is not None else None
 if not note:
  bot.send_message(message.chat.id, "🤔 I don't have anything saved yet. Use /remember <text> first. 📝")
  return
 bot.send_message(message.chat.id, f"🧠 You asked me to remember this:\n\n{note}")


@bot.message_handler(commands=["predict"], func=is_allowed)
def cmd_predict(message):
    # Core football-predictor command: the user names a fixture and the AI
    # returns a structured forecast. Uses _ask_once (stateless) so a prediction
    # doesn't pollute the user's conversation memory, and keeps the typing
    # indicator alive during the (sometimes slow) generation.
    match = message.text.split(maxsplit=1)[1].strip() if " " in message.text else ""
    if not match:
        bot.send_message(
            message.chat.id,
            "⚽ Tell me which match to forecast, e.g.\n"
            "/predict Barcelona vs Real Madrid",
        )
        return
    prompt = (
        f"Predict this football match: {match}.\n"
        "Reply concisely with:\n"
        "• Most likely outcome (home win / draw / away win)\n"
        "• A predicted scoreline\n"
        "• 2-3 short reasons (recent form, key players, head-to-head)\n"
        "• A confidence level: low / medium / high\n"
        "Do not add any disclaimer or note about betting or it being an AI estimate."
    )
    reply = _ask_once(message.from_user.id, message.chat.id, prompt)
    bot.send_message(message.chat.id, reply)


def _ask_once(user_id: int, chat_id: int, user_prompt: str) -> str:
    """Run a single stateless AI turn (system prompt + one user message).

    Unlike ask_ai(), this never reads or writes conversation history, so
    command replies like /help and /about don't pollute the user's chat
    memory. The typing indicator stays alive for the duration.
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    with keep_typing(chat_id):
        return generate(user_id, messages)


def _commands() -> list:
    """Canonical (command, purpose) list — the single source of truth.

    /model is only included when HF_SPACE_ID is configured, matching the
    handler registration below.
    """
    cmds = [
        ("/start", "👋 welcome message"),
        ("/help", "📖 show this list of commands"),
        ("/reset", "🔄 clear our conversation history"),
        ("/about", "ℹ️ who I am and what's under the hood"),
        ("/predict", "🔮 predict a match result — /predict TeamA vs TeamB"),
        ("/joke", "😂 tell jokes about football"),
        ("/quote", "✨ share an inspiring football & life quote"),
        ("/fact", "🤯 drop a surprising football fact"),
        ("/compliment", "🥰 get a warm football-flavoured compliment"),
        ("/roll", "🎲 roll a six-sided die"),
        ("/roast", "🔥 get a short, playful roast"),
        ("/remember", "📝 save something you want me to remember"),
        ("/recall", "🧠 read back what I asked you to remember"),
    ]
    if HF_SPACE_ID:
        cmds.append(("/model", "🤖 switch the AI provider powering me"))
    return cmds


def _fallback_help() -> str:
    """Plain football-voiced help, used when the AI call fails."""
    lines = ["⚽ Here's how I can help, gaffer:", ""]
    lines += [f"{name} — {purpose}" for name, purpose in _commands()]
    lines += ["", "Or just ask me anything — match forecasts, stats, tactics, history. 🏆"]
    return "\n".join(lines)


def _dynamic_help(user_id: int, chat_id: int) -> str:
    """Render the help text in the bot's voice via a one-off AI call.

    The exact command list is passed in so the AI restyles the descriptions
    without inventing or renaming commands. No history is read or written.
    Falls back to the static list on any error so /help never fails.
    """
    command_block = "\n".join(f"{name} — {purpose}" for name, purpose in _commands())
    prompt = (
        "Write a short Telegram /help message in your own voice. "
        "List EXACTLY these commands with their exact names — do not add, "
        "remove, or rename any — but you may restyle the descriptions and add "
        "a one-line intro and outro. Keep a fitting emoji next to EVERY command "
        "line (the list already has one each — keep or improve them). Keep it "
        "concise and lively.\n\n" + command_block
    )
    try:
        reply = _ask_once(user_id, chat_id, prompt)
        return reply.strip() or _fallback_help()
    except Exception as e:
        print(f"/help dynamic render failed: {e} — using fallback", flush=True)
        return _fallback_help()


@bot.message_handler(commands=["reset"], func=is_allowed)
def cmd_reset(message):
    clear_history(message.from_user.id)
    bot.send_message(message.chat.id, "🔄 Conversation cleared. Starting fresh! ⚽")


@bot.message_handler(commands=["about"], func=is_allowed)
def cmd_about(message):
    if HF_SPACE_ID:
        provider = get_provider(message.from_user.id)
        model_line = f"{MODEL} (main)" if provider == "main" else f"{HF_SPACE_ID} (hf)"
    else:
        model_line = MODEL
    storage_line = "SQLite" if store is not None else "stateless (no memory)"

    # Dynamic intro: ask the AI to introduce itself based on the current
    # SYSTEM_PROMPT so /about always reflects the bot's live personality.
    # This is a one-off call that does NOT touch conversation history.
    intro = _dynamic_intro(message.from_user.id, message.chat.id)

    lines = [
        intro,
        "",
        f"Model  : {model_line}",
        f"Storage: {storage_line}",
        f"Hosting: {HOSTING_LABEL}",
    ]
    if COMMIT_SHA:
        lines.append(f"Version: {COMMIT_SHA}")
    bot.send_message(message.chat.id, "\n".join(lines))


# Static fallback used when the AI intro call fails (network/provider error).
_FALLBACK_INTRO = (
    "⚽ FootyOracle — your AI football analyst. Ask me anything about the game, "
    "or about an upcoming match for a win/draw/win forecast. 🏆📊"
)


def _dynamic_intro(user_id: int, chat_id: int) -> str:
    """Generate a fresh self-introduction from the current SYSTEM_PROMPT.

    Runs a single stateless AI call (no history read or write). Falls back to
    a static blurb on any error so /about never fails. user_id selects the
    provider preference; chat_id drives the typing indicator.
    """
    prompt = (
        "Introduce yourself in 2-3 short sentences for an /about command: who you "
        "are, what you help with, and your vibe. Use a couple of emojis. Do not "
        "greet me or ask a question — just the introduction."
    )
    try:
        reply = _ask_once(user_id, chat_id, prompt)
        return reply.strip() or _FALLBACK_INTRO
    except Exception as e:
        print(f"/about dynamic intro failed: {e} — using fallback", flush=True)
        return _FALLBACK_INTRO


if HF_SPACE_ID:

    @bot.message_handler(commands=["model"], func=is_allowed)
    def cmd_model(message):
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 1:
            current = get_provider(message.from_user.id)
            bot.send_message(
                message.chat.id,
                f"Current provider: {current}\n\n"
                "Options:\n"
                "/model main — Cerebras (fast, multilingual, with memory)\n"
                "/model hf — ArmGPT (Armenian only, slow, no memory)",
            )
            return
        choice = parts[1].strip().lower()
        if choice not in ("main", "hf"):
            bot.send_message(
                message.chat.id, "Invalid choice. Use: /model main or /model hf"
            )
            return
        if not set_provider(message.from_user.id, choice):
            bot.send_message(
                message.chat.id, "Could not save preference. Try again later."
            )
            return
        if choice == "hf":
            bot.send_message(
                message.chat.id,
                "Switched to hf (ArmGPT).\n\n"
                "Note: this is a tiny base completion model trained only on Armenian text. "
                "It will continue whatever you write rather than answer questions, "
                "and it does not understand English. Replies take ~30-60s and there is no memory.",
            )
        else:
            bot.send_message(message.chat.id, "Switched to Main Provider.")


@bot.message_handler(content_types=["text"], func=is_allowed)
def handle_message(message):
    if not should_respond(message):
        return
    text = (message.text or "").replace(f"@{BOT_INFO.username}", "").strip()
    if not text:
        # Edited messages, forwards, or stickers-with-empty-caption can
        # arrive with no usable text. Don't burn rate-limit / AI calls on them.
        return
    _log(message, "in", text)
    if is_rate_limited(message.from_user.id):
        limit_msg = f"You've reached the daily limit of {RATE_LIMIT} messages. Try again tomorrow."
        bot.send_message(message.chat.id, limit_msg)
        _log(message, "out", f"[rate limited] {limit_msg}")
        return
    try:
        with keep_typing(message.chat.id):
            reply = ask_ai(message.from_user.id, text)
        send_reply(message, reply)
        _log(message, "out", reply)
    except Exception as e:
        print(f"Error in handle_message: {e}")
        bot.send_message(message.chat.id, "Something went wrong. Please try again.")
        _log(message, "out", f"[error] {e}")
