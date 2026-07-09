"""Search/service tests retained for v2.

These cover deterministic credibility scoring plus the standalone search/fetch
helpers that still underpin the Verify loop.
"""
import pytest

pytestmark = pytest.mark.asyncio


# --- credibility (offline) ------------------------------------------------------
async def test_credibility_bands():
    from app.services import credibility

    assert credibility.score("pib.gov.in") >= 0.9          # Tier 1 gov
    assert 0.7 <= credibility.score("boomlive.in") <= 0.85  # Tier 2 IFCN
    assert 0.4 <= credibility.score("timesofindia.indiatimes.com") <= 0.6  # Tier 3
    assert credibility.score("opindia.com") <= 0.3          # Tier 4
    assert credibility.score("some-random-blog.example") == 0.35  # unknown
    assert credibility.score("factcheck.pib.gov.in") >= 0.9  # sub-domain → parent match


# --- tools (offline: providers down → graceful empty) ---------------------------
async def test_tools_standalone(monkeypatch):
    from app.services import search, tools

    monkeypatch.delenv("SEARXNG_URL", raising=False)
    monkeypatch.delenv("GOOGLE_FACTCHECK_API_KEY", raising=False)
    search.factcheckers.cache_clear()

    assert await tools.call_tool("web_search", query="anything") == []          # SearXNG down → []
    assert await tools.call_tool("factcheck_search", query="anything") == []    # no key + no SearXNG → []
    cred = await tools.call_tool("source_credibility", domain="pib.gov.in")
    assert cred["credibility"] >= 0.9
    assert tools.schemas(["search"])[0]["function"]["name"] == "search"


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

