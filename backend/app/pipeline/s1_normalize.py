"""Stage 1 — Normalize: in-process language detect + one LLM extract/decompose.

Output: `{ language, sub_claims }`. No grounding / claim_type / volatility /
checkworthiness. Grounding (`Today is <as-of-date>`) lives in Verify (Phase 3).
"""
from langsmith import traceable

from ..models import ExtractOutput, NormalizerOutput
from ..services import mesh

# ponytail: hard cap for cost; raise if multi-claim forwards routinely exceed 3.
MAX_SUB_CLAIMS = 3

SYSTEM = """You extract check-worthy factual claims from a forwarded message.
You are an extractor, NOT a fact-checker, biographer, or rewriter.

Rules:
1. DROP greetings, opinions, feelings, jokes, questions, and calls-to-action
   ("forward this", "share now", "must watch", "forward to 10 people", emoji-only noise,
   source attributions like "shared by …"). Drop ONLY the noise — if a factual claim
   appears in the SAME message, you MUST still extract it.
2. COPY claims nearly verbatim. Keep the speaker's wording, names, titles, and polarity
   (do not negate, soften, or "correct" the claim). Light cleanup only: fix obvious typos,
   expand clear abbreviations if needed, resolve pronouns using context from the SAME
   message. Do NOT add background facts, career history, or related claims the message
   did not state.
3. One atomic fact in the message → exactly ONE sub-claim (that fact). Split into multiple
   sub-claims ONLY when the message itself asserts two or more independent facts
   (e.g. "A and B", separate sentences). Never invent extra sub-claims.
4. Emit AT MOST {max_claims} sub-claims. Prefer the most check-worthy facts if more exist.
5. If there are no check-worthy factual claims, return an empty list.

Examples:
- "DK Shivkumar is the CM of karnataka" → ["DK Shivkumar is the CM of Karnataka"]
- "Hi!! Forward this: vaccines cause autism. Share with 10 people"
  → ["Vaccines cause autism."]
- "The Earth is flat and the moon landing was faked"
  → ["The Earth is flat.", "The moon landing was faked."]
- "Modi is the best PM ever, don't you think?" → []

Return ONLY JSON: {{"sub_claims": ["...", "..."]}}"""


def detect_language(text: str, hint: str | None = None) -> str:
    """In-process language detect. No LLM. hint wins when provided and non-empty."""
    if hint and hint.strip():
        return hint.strip().split("-")[0].lower()[:8] or "en"
    sample = (text or "").strip()
    if not sample:
        return "en"
    try:
        from langdetect import DetectorFactory, detect
        DetectorFactory.seed = 0  # stable across runs
        return detect(sample)
    except Exception:
        return "en"


@traceable(name="normalize")
async def normalize(text: str, lang_hint: str | None = None) -> NormalizerOutput:
    language = detect_language(text, lang_hint)
    resp = await mesh.call(
        "normalizer",
        [
            {"role": "system", "content": SYSTEM.format(max_claims=MAX_SUB_CLAIMS)},
            {"role": "user", "content": text},
        ],
        response_schema=ExtractOutput,
    )
    assert resp.parsed is not None
    raw = [c.strip() for c in resp.parsed.sub_claims if c and c.strip()]
    return NormalizerOutput(language=language, sub_claims=raw[:MAX_SUB_CLAIMS])
