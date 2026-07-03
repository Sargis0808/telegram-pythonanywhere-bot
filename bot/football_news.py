"""Latest football news for the /footballnews command.

Pulls REAL, current football headlines from GNews (https://gnews.io) rather
than the AI: the model has a training cutoff and no internet, so asking it for
"latest news" would invent stories. This module never does that.

Requires GNEWS_API_KEY (bot/config.py). The /footballnews handler checks for
the key and degrades gracefully when it's unset. Every network or parse error
is turned into a friendly user-facing string via get_football_news_text(), so
the command never raises. gnews.io is on PythonAnywhere's outbound whitelist.
"""

from datetime import datetime, timezone

import requests

from bot.config import GNEWS_API_KEY

_API_URL = "https://gnews.io/api/v4/search"
# Query biased to football (soccer). Sorted by publish time so the freshest
# headlines come first.
_QUERY = "football"
_LANG = "en"
_MAX_ARTICLES = 8
# PA's webhook budget is ~60s and the worker may also be serving AI requests,
# so keep this well under that.
_TIMEOUT = 15


def _fetch_news() -> dict:
    """GET the latest football articles from GNews. Raises on HTTP/network error.

    requests routes through PA's outbound proxy automatically via the standard
    proxy env vars.
    """
    resp = requests.get(
        _API_URL,
        params={
            "q": _QUERY,
            "lang": _LANG,
            "max": _MAX_ARTICLES,
            "sortby": "publishedAt",
            "apikey": GNEWS_API_KEY,
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _date_str(published_at: str) -> str:
    """Convert an ISO-8601 UTC timestamp (…Z) to 'DD Mon HH:MM' (UTC)."""
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    return dt.astimezone(timezone.utc).strftime("%d %b %H:%M")


def format_news(data: dict) -> str:
    """Render the GNews payload into an emoji-rich Telegram message."""
    articles = data.get("articles") or []
    if not articles:
        return "📰 No fresh football news right now. Check back in a bit! 😌"

    lines = ["📰 Latest Football News 🔥", ""]
    for a in articles:
        title = (a.get("title") or "").strip() or "Untitled"
        source = ((a.get("source") or {}).get("name") or "").strip()
        when = _date_str(a.get("publishedAt", ""))
        meta = " · ".join(part for part in (source, when) if part)
        lines.append(f"⚽ {title}")
        if meta:
            lines.append(f"   {meta}")
        url = (a.get("url") or "").strip()
        if url:
            lines.append(f"   {url}")
        lines.append("")
    return "\n".join(lines).strip()


def get_football_news_text() -> str:
    """Fetch + format the latest football news; friendly string on any error."""
    try:
        data = _fetch_news()
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        if code == 429:
            return "📰 Too many requests right now — give it a minute and try /footballnews again. ⏳"
        if code in (401, 403):
            return "📰 The news key looks invalid — the bot owner should check GNEWS_API_KEY. 🔑"
        return f"📰 Couldn't reach the news service (HTTP {code}). Try again shortly. 🙏"
    except requests.exceptions.RequestException:
        return "📰 Couldn't reach the news service right now. Try again in a bit. 🙏"
    except Exception as e:  # noqa: BLE001 - never let /footballnews crash the worker
        print(f"/footballnews error: {e}", flush=True)
        return "📰 Something went wrong fetching the news. Try again shortly. 🙏"
    return format_news(data)
