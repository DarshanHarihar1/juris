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


# --- embeddings + cache ---------------------------------------------------------
@needs_nim
async def test_embed_dims():
    from app.services import nim

    v = await nim.embed(["The Great Wall of China is visible from space."])
    assert len(v) == 1 and len(v[0]) == 1024


@needs_db
@needs_nim
async def test_cache_hit_and_miss():
    from app import db
    from app.pipeline import s2_precedent
    from app.services import nim

    same = "Drinking cow urine cures COVID-19."
    other = "The Eiffel Tower is located in Paris, France."
    [e_same1, e_same2, e_other] = await nim.embed([same, same, other])

    con = await (await db.pool()).acquire()
    try:
        sub = await con.fetchval(
            "insert into submissions (channel, user_hash, media_type, raw_text) "
            "values ('web','test','text',$1) returning id", same)
        # a completed, cached verdict on the first claim
        c1 = await con.fetchval(
            "insert into claims (submission_id, text_original, text_norm, text_norm_native, claim_type, embedding) "
            "values ($1,$2,$2,$2,'factual',$3::vector) returning id", sub, same, s2_precedent.vec(e_same1))
        await con.execute(
            "insert into verdicts (claim_id, slug, verdict, confidence, card, path) "
            "values ($1,$2,'FALSE',90,'{}'::jsonb,'trial')", c1, f"test-{c1}")

        # identical claim → cache hit
        c2 = await con.fetchval(
            "insert into claims (submission_id, text_original, text_norm, text_norm_native, claim_type, embedding) "
            "values ($1,$2,$2,$2,'factual',$3::vector) returning id", sub, same, s2_precedent.vec(e_same2))
        hit = await s2_precedent._cache(con, c2, e_same2)
        assert hit and hit["path"] == "cache" and hit["slug"] == f"test-{c1}"

        # different claim → NO false cache hit
        c3 = await con.fetchval(
            "insert into claims (submission_id, text_original, text_norm, text_norm_native, claim_type, embedding) "
            "values ($1,$2,$2,$2,'factual',$3::vector) returning id", sub, other, s2_precedent.vec(e_other))
        assert await s2_precedent._cache(con, c3, e_other) is None
    finally:
        await con.execute("delete from submissions where id = $1", sub)  # cascades claims+verdicts
        await (await db.pool()).release(con)
