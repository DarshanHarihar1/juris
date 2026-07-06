"""S2 Precedent Check (LLD §5-S2) — retrieval-first short-circuits before investigation.
1. pgvector cosine search over the verified-claim cache (claims JOIN verdicts,
   confidence≥70); nearest hit ≥ cache_similarity → reuse that verdict (path=cache).
2. Google Fact Check / IFCN site search; a high-credibility human debunk → path=precedent.
Returns a short-circuit dict or None (→ continue to S3 investigation in a later phase).
Embeddings are written by the orchestrator; this module only reads for the lookup."""
import json

from ..config import thresholds
from ..services import credibility, search

CREDIBLE = 0.7                                  # min domain score to trust a human fact-check


def vec(embedding: list[float]) -> str:
    """pgvector text literal for an asyncpg $n::vector param."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


async def check(con, claim_id, embedding: list[float], text_norm: str) -> dict | None:
    hit = await _cache(con, claim_id, embedding)
    if hit:
        return hit
    return await _precedent(text_norm)


async def _cache(con, claim_id, embedding: list[float]) -> dict | None:
    row = await con.fetchrow(
        """
        select v.slug, v.verdict, v.confidence, v.card,
               1 - (c.embedding <=> $1::vector) as similarity
        from claims c join verdicts v on v.claim_id = c.id
        where v.confidence >= 70 and c.embedding is not null and c.id <> $2
        order by c.embedding <=> $1::vector
        limit 1
        """,
        vec(embedding), claim_id,
    )
    if not row or row["similarity"] < thresholds()["cache_similarity"]:
        return None
    return {
        "path": "cache", "slug": row["slug"], "verdict": row["verdict"],
        "confidence": row["confidence"], "similarity": float(row["similarity"]),
        "card": json.loads(row["card"]),
    }


async def _precedent(text_norm: str) -> dict | None:
    hits = await search.factcheck_search(text_norm)
    # Only short-circuit on a RATED human fact-check (the Google Fact Check API carries a
    # verdict). IFCN site-search hits have no rating — the topic merely appears on a
    # fact-checker site — so let those fall through to full investigation (S3→S4) rather
    # than emit an UNVERIFIABLE precedent verdict.
    rated = [h for h in hits
             if h.get("url") and h.get("rating") and credibility.score(h.get("domain", "")) >= CREDIBLE]
    if not rated:
        return None
    return {"path": "precedent", "fact_check": rated[0], "all_matches": rated[:5]}
