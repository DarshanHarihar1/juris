"""Phase 2 verification (design/phase-2-precedent-search.md):
- Credibility bands for 5 known domains; unknown → 0.35.
- Tools run standalone; search degrades to [] when providers are down.
- Embeddings are 1024-dim; cache hit on identical claim; no false hit on different claims.
DB/NIM tests skip cleanly when DATABASE_URL / NIM_API_KEY are absent."""
import pytest

from conftest import needs_db, needs_nim

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
    assert tools.schemas(["web_search"])[0]["function"]["name"] == "web_search"


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


# --- embeddings ----------------------------------------------------------------
@needs_nim
async def test_embed_dims():
    from app.services import nim

    v = await nim.embed(["The Great Wall of China is visible from space."])
    assert len(v) == 1 and len(v[0]) == 1024


# --- precedent semantic gate (offline) ------------------------------------------
async def test_precedent_semantic_gate(monkeypatch):
    """A rated fact-check is only trusted if its claim text is embedding-similar to THIS
    claim — guards against Google FactCheck's fuzzy keyword matches (e.g. 'Modi is PM'
    wrongly matching a 'Modi is first OBC PM' debunk)."""
    from app.pipeline import s2_precedent as s2
    from app.services import search

    hit = {"url": "https://altnews.in/x", "domain": "altnews.in",
           "rating": "False", "claim": "Modi is India's first OBC PM"}

    async def fake_fc(_q):
        return [hit]
    monkeypatch.setattr(search, "factcheck_search", fake_fc)

    # dissimilar fact-check (orthogonal embedding) → rejected, fall through
    async def emb_far(_texts):
        return [[0.0, 1.0, 0.0]]
    monkeypatch.setattr(s2.nim, "embed", emb_far)
    assert await s2._precedent("Modi is PM", [1.0, 0.0, 0.0], "c1") is None

    # same claim (aligned embedding) → accepted
    async def emb_near(_texts):
        return [[1.0, 0.0, 0.0]]
    monkeypatch.setattr(s2.nim, "embed", emb_near)
    res = await s2._precedent("Modi is PM", [1.0, 0.0, 0.0], "c2")
    assert res is not None and res["path"] == "precedent" and res["similarity"] > 0.99


async def test_check_skips_cache_and_uses_precedent_only(monkeypatch):
    """Phase A removes verdict cache reuse; S2 should go straight to precedent lookup."""
    from app.pipeline import s2_precedent as s2

    async def fake_precedent(text_norm, embedding, claim_id):
        return {"path": "precedent", "fact_check": {"url": "https://altnews.in/x"}, "similarity": 0.9}

    assert not hasattr(s2, "_cache")
    monkeypatch.setattr(s2, "_precedent", fake_precedent)

    res = await s2.check(None, "claim-1", [1.0, 0.0, 0.0], "Some claim")
    assert res is not None and res["path"] == "precedent"
