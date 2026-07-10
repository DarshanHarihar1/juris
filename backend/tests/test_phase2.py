"""Search/service tests retained for v2.

Covers the search/fetch helpers that underpin the Verify loop.
"""
import logging

import httpx
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
    monkeypatch.setattr(search, "_extract_trafilatura", lambda url: None)

    page = await search.fetch_page("https://www.cricbuzz.com/live-cricket-scorecard/123")
    assert page == {
        "url": "https://www.cricbuzz.com/live-cricket-scorecard/123",
        "text": "# rendered markdown",
    }
    assert seen["url"] == "https://r.jina.ai/https://www.cricbuzz.com/live-cricket-scorecard/123"
    assert seen["headers"]["Authorization"] == "Bearer test-key"
    assert seen["headers"]["X-Return-Format"] == "markdown"


async def test_fetch_page_prefers_trafilatura(monkeypatch):
    from app.services import search

    monkeypatch.setattr(search, "_extract_trafilatura", lambda url: "a" * 300)

    async def boom(url):
        raise AssertionError("should not fall back to Jina when Trafilatura succeeds")

    monkeypatch.setattr(search, "_fetch_page_jina", boom)

    page = await search.fetch_page("https://example.com/article")
    assert page == {"url": "https://example.com/article", "text": "a" * 300}


async def test_fetch_page_falls_back_when_trafilatura_too_thin(monkeypatch):
    from app.services import search

    monkeypatch.setattr(search, "_extract_trafilatura", lambda url: "too short")

    async def fake_jina(url):
        return {"url": url, "text": "jina rendered it"}

    monkeypatch.setattr(search, "_fetch_page_jina", fake_jina)

    page = await search.fetch_page("https://example.com/js-heavy")
    assert page == {"url": "https://example.com/js-heavy", "text": "jina rendered it"}


async def test_web_search_hostport_gets_http_scheme(monkeypatch):
    from app.services import search

    seen = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None):
            seen["url"] = url
            return _Resp()

    monkeypatch.setenv("SEARXNG_URL", "juris-searxng:8080")
    monkeypatch.setattr(search.httpx, "AsyncClient", _Client)

    await search.web_search("test")
    assert seen["url"] == "http://juris-searxng:8080/search"


async def test_web_search_logs_request_errors(monkeypatch, caplog):
    from app.services import search

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None):
            raise httpx.ConnectError("connection refused", request=httpx.Request("GET", url))

    monkeypatch.setenv("SEARXNG_URL", "juris-searxng:8080")
    monkeypatch.setattr(search.httpx, "AsyncClient", _Client)

    with caplog.at_level(logging.ERROR, logger="juris.search"):
        rows = await search.web_search("test query")

    assert rows == []
    assert any("SearXNG request failed" in r.message for r in caplog.records)


async def test_web_search_retries_502(monkeypatch):
    from app.services import search

    calls = {"n": 0}

    class _Resp:
        def __init__(self, status_code: int, payload: dict | None = None):
            self.status_code = status_code
            self.text = "<html>502</html>"
            self._payload = payload or {"results": [{"url": "https://example.com", "title": "ok", "content": "x", "score": 1}]}

        def raise_for_status(self):
            if self.status_code >= 400:
                req = httpx.Request("GET", "https://searx.example/search")
                raise httpx.HTTPStatusError("err", request=req, response=self)

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _Resp(502)
            return _Resp(200)

    monkeypatch.setenv("SEARXNG_URL", "https://searx.example")
    monkeypatch.setattr(search, "_SEARCH_RETRY_DELAY", 0.01)
    monkeypatch.setattr(search.httpx, "AsyncClient", _Client)

    rows = await search.web_search("retry test")
    assert calls["n"] == 2
    assert len(rows) == 1
    assert rows[0]["url"] == "https://example.com"


async def test_warm_searxng_ok(monkeypatch):
    from app.services import search

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

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

    assert await search.warm_searxng() is True


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
