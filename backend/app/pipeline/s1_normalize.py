"""S1 Normalize & Decompose (LLD §5-S1). One structured `normalizer` NIM call:
detect language, extract check-worthy claims, drop opinion/greetings, rewrite each
as a self-contained sentence (native + English pivot), classify, split compound → ≤3."""
from ..models import NormalizerOutput
from ..services import nim

SYSTEM = """You normalize messages for a fact-checking system. Extract only CHECK-WORTHY factual claims.

Rules:
1. Detect the message language. Use codes like "en", "hi" (Devanagari Hindi), "hi-Latn" (romanized Hindi/Hinglish). Put it in detected_lang.
2. DROP greetings, opinions, feelings, jokes, questions, and calls-to-action ("forward this", "share now", "must watch"). These are not claims.
3. For each real claim, rewrite it as a SELF-CONTAINED sentence: resolve pronouns, add the implied context, strip emojis / hashtags / urgency words. Produce both:
   - text_norm: the claim in clear English.
   - text_norm_native: the same claim in the message's original language (identical to text_norm when the message is English).
4. Classify claim_type as one of: "factual", "numeric" (quantities/stats/dates), "media_context" (a photo/video presented out of context), "quote" (words attributed to someone). Do not use "opinion_skip" — just omit opinions entirely.
5. Split a compound message into separate atomic claims. Emit AT MOST 3.
6. If there are no check-worthy factual claims, return an empty claims list.

Return ONLY JSON: {"detected_lang": string, "claims": [{"text_norm": string, "text_norm_native": string, "claim_type": string}]}"""


async def normalize(text: str, lang_hint: str | None = None) -> NormalizerOutput:
    user = text if not lang_hint else f"[lang hint: {lang_hint}]\n{text}"
    resp = await nim.call(
        "normalizer",
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        response_schema=NormalizerOutput,
    )
    # nim.call retries once then falls back on schema failure; parsed is set on success.
    assert resp.parsed is not None
    out = resp.parsed
    out.claims = out.claims[:3]                     # enforce ≤3 atomic (LLD §5-S1)
    return out
