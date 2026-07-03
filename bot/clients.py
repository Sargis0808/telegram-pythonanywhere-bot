import threading
import time
from urllib.parse import urlparse

import telebot
from openai import OpenAI

from bot.config import AI_API_KEY, AI_BASE_URL, SQLITE_PATH, TELEGRAM_TOKEN
from bot.store import SqliteStore

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)
ai = OpenAI(base_url=AI_BASE_URL, api_key=AI_API_KEY)


# Persistent storage is optional. Set SQLITE_PATH to the absolute path
# of a SQLite file to enable history / rate limiting / preferences /
# dedupe. Without it the bot runs in stateless mode — every consumer
# in bot/ checks `store is None` and falls back to safe defaults.
def _init_store(path: str):
    """Initialize the SQLite store or fall back to stateless mode.

    A bad SQLITE_PATH (unwritable dir, locked file, corrupt DB) used to
    crash worker boot. Now we degrade to None instead — the bot still
    answers messages, just without history / rate-limit / dedupe / prefs.
    """
    if not path:
        print(
            "Storage not configured — running in stateless mode (no memory, no rate limit)."
        )
        return None
    try:
        s = SqliteStore(path)
        print(f"Using SQLite store at {path}.")
        return s
    except Exception as e:
        print(
            f"SqliteStore init failed for {path!r} ({e}) — "
            "falling back to stateless mode."
        )
        return None


store = _init_store(SQLITE_PATH)


class _LazyBotInfo:
    """Defers `bot.get_me()` until first attribute access; caches the
    result; retries transient failures.

    Why this exists: PA's outbound HTTPS proxy occasionally returns 503
    for a few seconds. If `get_me()` ran at module load, a single
    proxy blip would prevent the WSGI worker from booting at all. With
    lazy access, the worker comes up regardless and individual requests
    that need the bot's username retry up to 3 times with backoff.

    Thread safety: `_load()` is guarded by a class-level lock with a
    double-check so concurrent first-accesses don't both call get_me().
    """

    _info = None
    _lock = threading.Lock()

    def _load(self) -> None:
        if self._info is not None:
            return
        with self.__class__._lock:
            if self.__class__._info is not None:
                return
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    self.__class__._info = bot.get_me()
                    return
                except Exception as e:
                    last_exc = e
                    print(f"bot.get_me() attempt {attempt + 1}/3 failed: {e}")
                    if attempt < 2:
                        time.sleep(1)
            raise RuntimeError(
                f"Could not fetch bot info from Telegram after 3 attempts: {last_exc}"
            )

    def __getattr__(self, name):
        self._load()
        return getattr(self.__class__._info, name)


BOT_INFO = _LazyBotInfo()


def register_webhook() -> str:
    """Register the Telegram webhook against WEBHOOK_URL.

    Idempotent — Telegram treats repeated setWebhook calls with the
    same URL as no-ops. Returns a status string for logging. Never
    raises: failures are caught so a bad token or network blip doesn't
    crash worker boot.

    Validates WEBHOOK_URL before calling Telegram so a typo (missing
    scheme, http instead of https, missing path) fails fast with a
    clear message instead of a confusing Telegram error.
    """
    from bot.config import WEBHOOK_SECRET, WEBHOOK_URL

    if not WEBHOOK_URL:
        return "WEBHOOK_URL unset — auto-registration skipped"

    parsed = urlparse(WEBHOOK_URL)
    if parsed.scheme != "https":
        return f"WEBHOOK_URL must use https:// (got {parsed.scheme or '<no scheme>'}://) — skipping"
    if not parsed.netloc:
        return f"WEBHOOK_URL has no host — skipping ({WEBHOOK_URL!r})"
    if not parsed.path:
        return f"WEBHOOK_URL has no path; Telegram needs a real endpoint — skipping ({WEBHOOK_URL!r})"

    # max_connections=1 serializes Telegram deliveries to the worker.
    # bot/history.py + bot/preferences.py do read-modify-write against
    # the SQLite store; without this, two quick messages from the same
    # user can interleave and lose a turn. PA's single-worker free tier
    # makes this cheap — at most one update in flight at a time anyway.
    kwargs = {"url": WEBHOOK_URL, "max_connections": 1}
    if WEBHOOK_SECRET:
        kwargs["secret_token"] = WEBHOOK_SECRET

    # PA's outbound proxy 503-blips several times a day; a couple of
    # retries ride it out. Seen live (2026-06-29): a boot-time
    # registration failed on a blip and the bot ran on whatever webhook
    # state Telegram already had until the next deploy re-asserted it.
    result = None
    for attempt in range(3):
        try:
            result = bot.set_webhook(**kwargs)
            break
        except Exception as e:
            if attempt == 2:
                return f"Webhook registration failed: {e}"
            print(f"set_webhook attempt {attempt + 1}/3 failed, retrying: {e}")
            time.sleep(1 + attempt)

    # pyTelegramBotAPI returns True on success, False otherwise. Surface
    # the difference so caller logs are honest.
    if result is False:
        return f"Webhook registration: Telegram returned False for {WEBHOOK_URL}"
    return f"Webhook registered: {WEBHOOK_URL}"
