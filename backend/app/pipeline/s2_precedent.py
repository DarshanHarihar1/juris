"""S2 Precedent Check (LLD §5-S2) — retrieval-first short-circuits before investigation.
1. pgvector cosine search over the verified-claim cache (claims JOIN verdicts,
   confidence≥70); nearest hit ≥ cache_similarity → reuse that verdict (path=cache).
2. Google Fact Check / IFCN site search; a high-credibility human debunk → path=precedent.
Returns a short-circuit dict or None (→ continue to S3 investigation in a later phase).
Embeddings are written by the orchestrator; this module only reads for the lookup."""
import json
import logging
import math

from ..config import thresholds
from ..services import credibility, nim, search

log = logging.getLogger("juris.s2")

CREDIBLE = 0.7                                  # min domain score to trust a human fact-check


def vec(embedding: list[float]) -> str:
    """pgvector text literal for an asyncpg $n::vector param."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def check(con, claim_id, embedding: list[float], text_norm: str) -> dict | None:
    return await _precedent(text_norm, embedding, claim_id)


async def _precedent(text_norm: str, claim_emb: list[float], claim_id) -> dict | None:
    hits = await search.factcheck_search(text_norm)
    # Only short-circuit on a RATED human fact-check (the Google Fact Check API carries a
    # verdict). IFCN site-search hits have no rating — the topic merely appears on a
    # fact-checker site — so let those fall through to full investigation (S3→S4).
    rated = [h for h in hits
             if h.get("url") and h.get("rating") and credibility.score(h.get("domain", "")) >= CREDIBLE]
    if not rated:
        log.info("S2 precedent: no rated fact-check for claim=%s (%d total hits)", claim_id, len(hits))
        return None

    # Semantic gate: Google Fact Check search is fuzzy keyword matching, so a rated hit may be
    # about a DIFFERENT claim that merely shares words (e.g. "Modi is PM" vs "Modi is first OBC
    # PM"). Require the fact-check's claim text to be embedding-similar to THIS claim before
    # trusting its verdict; otherwise fall through to investigation.
    theta = thresholds().get("precedent_similarity", 0.85)
    texts = [(h.get("claim") or h.get("title") or "") for h in rated]
    embs = await nim.embed(texts)
    best: tuple[float, dict] | None = None
    for h, emb, t in zip(rated, embs, texts):
        sim = _cosine(claim_emb, emb)
        log.info("S2 precedent candidate claim=%s sim=%.3f rating=%r fc=%r url=%s",
                 claim_id, sim, h.get("rating"), t[:80], h.get("url"))
        if sim >= theta and (best is None or sim > best[0]):
            best = (sim, h)

    if best is None:
        log.info("S2 precedent REJECTED claim=%s — no fact-check >= %.2f similarity (falling through to S3)",
                 claim_id, theta)
        return None
    sim, h = best
    log.info("S2 precedent ACCEPTED claim=%s sim=%.3f rating=%r url=%s",
             claim_id, sim, h.get("rating"), h.get("url"))
    return {"path": "precedent", "fact_check": h, "similarity": sim, "all_matches": rated[:5]}
