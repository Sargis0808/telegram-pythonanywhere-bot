from unittest.mock import MagicMock, patch

import requests

import bot.football_news as fn


def _article(title, source="BBC Sport", published="2026-07-02T17:30:00Z", url="https://example.com/x"):
    return {
        "title": title,
        "description": "…",
        "url": url,
        "publishedAt": published,
        "source": {"name": source},
    }


def test_format_news_empty():
    out = fn.format_news({"articles": []})
    assert "No fresh football news" in out


def test_format_news_lists_articles():
    data = {
        "articles": [
            _article("Messi scores twice", source="ESPN"),
            _article("Big transfer confirmed", source="Sky Sports"),
        ]
    }
    out = fn.format_news(data)
    assert "📰 Latest Football News" in out
    assert "Messi scores twice" in out
    assert "Big transfer confirmed" in out
    assert "ESPN" in out
    assert "https://example.com/x" in out


def test_get_football_news_text_handles_network_error():
    with patch.object(fn.requests, "get", side_effect=requests.exceptions.ConnectionError()):
        out = fn.get_football_news_text()
    assert "Couldn't reach" in out


def test_get_football_news_text_handles_401():
    resp = MagicMock(status_code=401)
    err = requests.exceptions.HTTPError(response=resp)
    with patch.object(fn, "_fetch_news", side_effect=err):
        out = fn.get_football_news_text()
    assert "invalid" in out.lower()


def test_get_football_news_text_handles_429():
    resp = MagicMock(status_code=429)
    err = requests.exceptions.HTTPError(response=resp)
    with patch.object(fn, "_fetch_news", side_effect=err):
        out = fn.get_football_news_text()
    assert "Too many requests" in out


def test_get_football_news_text_success():
    data = {"articles": [_article("Header news")]}
    with patch.object(fn, "_fetch_news", return_value=data):
        out = fn.get_football_news_text()
    assert "Header news" in out
