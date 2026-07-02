from unittest.mock import MagicMock, patch

import requests

import bot.football_data as fd


def _match(home, away, status, utc, h=None, a=None, comp="Premier League", code="PL"):
    return {
        "utcDate": utc,
        "status": status,
        "competition": {"name": comp, "code": code},
        "homeTeam": {"shortName": home, "name": home},
        "awayTeam": {"shortName": away, "name": away},
        "score": {"fullTime": {"home": h, "away": a}},
    }


def test_format_matches_empty():
    out = fd.format_matches({"matches": []})
    assert "No football matches" in out


def test_format_matches_groups_and_statuses():
    data = {
        "matches": [
            _match("Arsenal", "Chelsea", "TIMED", "2026-07-02T17:30:00Z"),
            _match("Liverpool", "Everton", "IN_PLAY", "2026-07-02T14:00:00Z", 1, 0),
            _match("Barcelona", "Sevilla", "FINISHED", "2026-07-02T19:00:00Z", 3, 1,
                   comp="La Liga", code="PD"),
        ]
    }
    with patch.object(fd, "FOOTBALL_TZ_OFFSET", 4.0):
        out = fd.format_matches(data)
    # Both competitions present, with their flags
    assert "🏴 Premier League" in out
    assert "🇪🇸 La Liga" in out
    # Scheduled → local kickoff time (17:30 UTC + 4h = 21:30)
    assert "Arsenal vs Chelsea — ⏰ 21:30" in out
    # Live and finished show scores
    assert "🔴 LIVE 1-0" in out
    assert "✅ FT 3-1" in out


def test_time_str_applies_offset():
    with patch.object(fd, "FOOTBALL_TZ_OFFSET", 4.0):
        assert fd._time_str("2026-07-02T17:30:00Z") == "21:30"


def test_get_today_matches_text_handles_network_error():
    with patch.object(fd.requests, "get", side_effect=requests.exceptions.ConnectionError()):
        out = fd.get_today_matches_text()
    assert "Couldn't reach" in out


def test_get_today_matches_text_handles_401():
    resp = MagicMock(status_code=401)
    err = requests.exceptions.HTTPError(response=resp)
    with patch.object(fd, "_fetch_today_matches", side_effect=err):
        out = fd.get_today_matches_text()
    assert "invalid" in out.lower()


def test_get_today_matches_text_handles_429():
    resp = MagicMock(status_code=429)
    err = requests.exceptions.HTTPError(response=resp)
    with patch.object(fd, "_fetch_today_matches", side_effect=err):
        out = fd.get_today_matches_text()
    assert "Too many requests" in out


def test_get_today_matches_text_success():
    data = {"matches": [_match("Arsenal", "Chelsea", "TIMED", "2026-07-02T17:30:00Z")]}
    with patch.object(fd, "_fetch_today_matches", return_value=data):
        out = fd.get_today_matches_text()
    assert "Arsenal vs Chelsea" in out
