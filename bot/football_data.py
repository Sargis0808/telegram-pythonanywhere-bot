"""Today's football fixtures for the /lastnews command.

Pulls REAL match data (kickoff times, live status, scores) from
football-data.org — a free, PythonAnywhere-whitelisted API. We use a live
data source rather than the AI here on purpose: the model has a training
cutoff and no internet, so asking it for "today's matches" would invent
fixtures and times. This module never does that.

Requires FOOTBALL_DATA_API_KEY (bot/config.py). The /lastnews handler
checks for the key and degrades gracefully when it's unset. Every network
or parse error is turned into a friendly user-facing string via
get_today_matches_text(), so the command never raises.
"""

from datetime import datetime, timedelta, timezone

import requests

from bot.config import FOOTBALL_DATA_API_KEY, FOOTBALL_TZ_OFFSET

_API_URL = "https://api.football-data.org/v4/matches"
# PA's webhook budget is ~60s and the worker may also be doing AI work on
# other requests, so keep this well under that.
_TIMEOUT = 15

# Competition code → flag/cup emoji. Covers football-data.org's free-tier
# competitions; anything else falls back to ⚽.
_COMP_EMOJI = {
    "PL": "🏴", "ELC": "🏴",              # England
    "PD": "🇪🇸",                          # Spain
    "SA": "🇮🇹",                          # Italy
    "BL1": "🇩🇪",                         # Germany
    "FL1": "🇫🇷",                         # France
    "DED": "🇳🇱",                         # Netherlands
    "PPL": "🇵🇹",                         # Portugal
    "BSA": "🇧🇷",                         # Brazil
    "CL": "🏆", "EC": "🏆", "WC": "🏆",   # Champions League / Euros / World Cup
}


def _tz_label() -> str:
    """Human-readable label for the configured offset, e.g. 'UTC+4'."""
    off = FOOTBALL_TZ_OFFSET
    num = int(off) if off == int(off) else off
    return f"UTC{'+' if off >= 0 else ''}{num}"


def _local_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=FOOTBALL_TZ_OFFSET)


def _fetch_today_matches() -> dict:
    """GET today's matches from football-data.org. Raises on HTTP/network error.

    'Today' is the current date in the user's timezone. requests routes
    through PA's outbound proxy automatically via the standard proxy env vars.
    """
    today = _local_now().strftime("%Y-%m-%d")
    resp = requests.get(
        _API_URL,
        headers={"X-Auth-Token": FOOTBALL_DATA_API_KEY},
        params={"dateFrom": today, "dateTo": today},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _time_str(utc_date: str) -> str:
    """Convert an ISO-8601 UTC timestamp (…Z) to HH:MM in the configured tz."""
    dt = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
    return (dt + timedelta(hours=FOOTBALL_TZ_OFFSET)).strftime("%H:%M")


def _match_line(m: dict) -> str:
    home = (m.get("homeTeam") or {}).get("shortName") or (m.get("homeTeam") or {}).get("name") or "TBD"
    away = (m.get("awayTeam") or {}).get("shortName") or (m.get("awayTeam") or {}).get("name") or "TBD"
    status = m.get("status", "")
    ft = (m.get("score") or {}).get("fullTime") or {}
    h, a = ft.get("home"), ft.get("away")
    if status in ("IN_PLAY", "PAUSED"):
        tail = f"🔴 LIVE {h}-{a}"
    elif status == "FINISHED":
        tail = f"✅ FT {h}-{a}"
    elif status in ("SCHEDULED", "TIMED") and m.get("utcDate"):
        tail = f"⏰ {_time_str(m['utcDate'])}"
    else:
        tail = status.replace("_", " ").title() or "TBD"
    return f"  {home} vs {away} — {tail}"


def format_matches(data: dict) -> str:
    """Render the API payload into a grouped, emoji-rich Telegram message."""
    matches = data.get("matches") or []
    if not matches:
        return "⚽ No football matches scheduled for today. Enjoy the breather! 😌"

    # Group by competition, preserving first-seen order.
    groups: dict = {}
    order: list = []
    for m in matches:
        comp = m.get("competition") or {}
        key = (comp.get("name", "Other"), comp.get("code", ""))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(m)

    header = f"⚽ Today's Football — {_local_now().strftime('%a %d %b')} (times {_tz_label()}) 🔥"
    lines = [header, ""]
    for name, code in order:
        lines.append(f"{_COMP_EMOJI.get(code, '⚽')} {name}")
        for m in sorted(groups[(name, code)], key=lambda x: x.get("utcDate", "")):
            lines.append(_match_line(m))
        lines.append("")
    return "\n".join(lines).strip()


def get_today_matches_text() -> str:
    """Fetch + format today's matches, returning a friendly string on any error."""
    try:
        data = _fetch_today_matches()
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        if code == 429:
            return "⚽ Too many requests right now — give it a minute and try /lastnews again. ⏳"
        if code in (401, 403):
            return "⚽ The football data key looks invalid — the bot owner should check FOOTBALL_DATA_API_KEY. 🔑"
        return f"⚽ Couldn't reach the football data service (HTTP {code}). Try again shortly. 🙏"
    except requests.exceptions.RequestException:
        return "⚽ Couldn't reach the football data service right now. Try again in a bit. 🙏"
    except Exception as e:  # noqa: BLE001 - never let /lastnews crash the worker
        print(f"/lastnews error: {e}", flush=True)
        return "⚽ Something went wrong fetching today's matches. Try again shortly. 🙏"
    return format_matches(data)
