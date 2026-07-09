"""S1 Normalize & Decompose (LLD §5-S1). One structured `normalizer` NIM call:
detect language, extract check-worthy claims, drop opinion/greetings, rewrite each
as a self-contained sentence (native + English pivot), classify, split compound → ≤3."""
from datetime import date

from ..config import thresholds
from ..models import NormalizerOutput
from ..services import nim

SYSTEM = """You normalize messages for a fact-checking system. Extract only CHECK-WORTHY factual claims.
Today's date: {today}.

Rules:
1. Detect the message language. Use codes like "en", "hi" (Devanagari Hindi), "hi-Latn" (romanized Hindi/Hinglish). Put it in detected_lang.
2. DROP greetings, opinions, feelings, jokes, questions, and calls-to-action ("forward this", "share now", "must watch", "forward to 10 people"). Drop ONLY the CTA/greeting sentence — if a factual claim appears in the SAME message, you MUST still extract it. Never return an empty list just because the message also contains a call-to-action.
3. For each real claim, produce text_norm and text_norm_native:
   - If the claim is already a complete, self-contained factual sentence, copy it VERBATIM — do NOT paraphrase, shorten, or summarize it.
   - Only rewrite when needed: resolve pronouns ("he" → the named person), add missing context for fragments, strip emojis/hashtags/urgency words.
   - For PRESENT-TENSE or relative-time claims ("is", "current", "now", "today", "this year"), ground the claim to today's date in the normalized text so retrieval is anchored in time. Example: "X is the CM" → "As of July 2026, X is the current CM."
   - text_norm: the claim in clear English (verbatim if already complete).
   - text_norm_native: the same claim in the message's original language (identical to text_norm when the message is English).
4. Classify claim_type as one of: "factual", "numeric" (quantities/stats/dates), "media_context" (a photo/video presented out of context), "quote" (words attributed to someone). Do not use "opinion_skip" — just omit opinions entirely.
5. Set is_time_sensitive=true when the claim depends on current office-holders, current prices, election outcomes, rankings, or any fact that can change over time. Otherwise false.
6. Set as_of_date to the date the claim is anchored to, if stated or implied ("as of today", "in the 2024 budget", "yesterday"). If is_time_sensitive=true and no anchor is stated, set as_of_date to today's date in YYYY-MM-DD form. Otherwise null.
7. Set volatility to exactly one of:
   - "static" for facts whose truth cannot change (historical facts, science, geography).
   - "slow" for facts that change over months/years (officeholders, populations, laws).
   - "breaking" for facts that change over hours/days (ongoing events, disasters, sports, markets).
8. Set checkworthiness_score to a number from 0.0 to 1.0. Clear, factual, independently verifiable claims should score high. Vague, opinion-adjacent, or weakly factual claims should score low.
9. Split a compound message into separate atomic claims. Emit AT MOST 3.
10. If there are no check-worthy factual claims, return an empty claims list.

Return ONLY JSON: {{"detected_lang": string, "claims": [{{"text_norm": string, "text_norm_native": string, "claim_type": string, "is_time_sensitive": boolean, "as_of_date": string|null, "volatility": "static"|"slow"|"breaking", "checkworthiness_score": number}}]}}"""


async def normalize(text: str, lang_hint: str | None = None) -> NormalizerOutput:
    user = text if not lang_hint else f"[lang hint: {lang_hint}]\n{text}"
    resp = await nim.call(
        "normalizer",
        [{"role": "system", "content": SYSTEM.format(today=date.today().isoformat())},
         {"role": "user", "content": user}],
        response_schema=NormalizerOutput,
    )
    # nim.call retries once then falls back on schema failure; parsed is set on success.
    assert resp.parsed is not None
    out = resp.parsed
    out.claims = out.claims[:3]                     # enforce ≤3 atomic (LLD §5-S1)
    today = date.today().isoformat()
    floor = thresholds().get("checkworthy_floor", 0.6)
    for claim in out.claims:
        if claim.is_time_sensitive and not claim.as_of_date:
            claim.as_of_date = today
    out.claims = [claim for claim in out.claims if claim.checkworthiness_score >= floor]
    return out
