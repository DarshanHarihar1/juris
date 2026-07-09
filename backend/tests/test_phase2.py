"""Search/service tests retained for v2.

Covers the search/fetch helpers that underpin the Verify loop.
"""
import pytest

pytestmark = pytest.mark.asyncio


async def test_tools_search_only(monkeypatch):
    from app.services import tools

    monkeypatch.delenv("SEARXNG_URL", raising=False)
    assert set(tools.REGISTRY) == {"search"}
    assert tools.schemas(["search"])[0]["function"]["name"] == "search"
    assert await tools.call_tool("search", query="anything") == []


async def test_fetch_page_uses_jina_reader(monkeypatch):
    from app.services import search

    seen = {}

    class _Resp:
        text = "# rendered markdown"

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None):
            seen["url"] = url
            seen["headers"] = headers or {}
            return _Resp()

    monkeypatch.setenv("JINA_API_KEY", "test-key")
    monkeypatch.setattr(search.httpx, "AsyncClient", _Client)

    page = await search.fetch_page("https://www.cricbuzz.com/live-cricket-scorecard/123")
    assert page == {
        "url": "https://www.cricbuzz.com/live-cricket-scorecard/123",
        "text": "# rendered markdown",
    }
    assert seen["url"] == "https://r.jina.ai/https://www.cricbuzz.com/live-cricket-scorecard/123"
    assert seen["headers"]["Authorization"] == "Bearer test-key"
    assert seen["headers"]["X-Return-Format"] == "markdown"


async def test_web_search_unwraps_google_redirect_urls(monkeypatch):
    from app.services import search

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {
                        "url": "https://www.google.com/url?rct=j&sa=t&url=https://www.hindustantimes.com/india-news/dk-shivakumar-is-the-new-chief-minister-of-karnataka-10162319418868.html&ct=ga",
                        "title": "DK Shivakumar is the new Chief Minister of Karnataka",
                        "content": "DK Shivakumar is the new Chief Minister of Karnataka.",
                        "publishedDate": "2026-07-09",
                        "score": 0.9,
                    }
                ]
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None):
            return _Resp()

    monkeypatch.setenv("SEARXNG_URL", "https://searx.example")
    monkeypatch.setattr(search.httpx, "AsyncClient", _Client)

    rows = await search.web_search("karnataka cm july 2026", recency="week")
    assert len(rows) == 1
    assert rows[0]["url"] == "https://www.hindustantimes.com/india-news/dk-shivakumar-is-the-new-chief-minister-of-karnataka-10162319418868.html"
    assert rows[0]["domain"] == "hindustantimes.com"
    assert rows[0]["published_at"] == "2026-07-09"
