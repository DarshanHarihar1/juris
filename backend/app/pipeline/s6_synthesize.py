"""S6 Synthesis. Turns a decided verification result into the user-facing VerdictCard:
a citation-locked explanation in the user's language, manipulation tags from a fixed
taxonomy, and a <=400-char forwardable rebuttal. Persists the verdict row and emits the
final `verdict` event."""
import json
import re

from ..config import role
from ..models import MANIPULATION_TAGS, EvidenceRef, SynthOutput, VerdictCard
from ..services import citations, events, nim

REBUTTAL_MAX = 400

SYSTEM = """You are the SYNTHESIZER for a fact-checking service. The verdict is already
DECIDED — do not change it. Write the user-facing card in the user's language ({lang}).

Rules:
- one_liner_native: ONE short sentence stating the verdict, in {lang}.
- explanation_native: 3–5 sentences in {lang}. EVERY factual sentence MUST cite evidence
  with an [e:id] tag (e.g. [e:e2]). Assert nothing not supported by the evidence snippets.
- manipulation_tags: identify manipulation techniques ACTUALLY present in the ORIGINAL
  message below. Choose 0–3 ONLY from this taxonomy, and use [] if none clearly apply —
  do not guess or list unrelated tags: {tags}.
- rebuttal_card_native: a polite forwardable counter-message in {lang}, ≤400 characters,
  including at least one source URL. Correct the claim; never insult the sender.

Return ONLY JSON:
{{"one_liner_native":"...","explanation_native":"...","manipulation_tags":[],"rebuttal_card_native":"..."}}"""


def rating_to_class(rating: str | None) -> str:
    """Map a human fact-check's textual rating to the local verdict classes."""
    r = (rating or "").lower()
    # check misleading/mixed first — "partly false", "half true" are MISLEADING, not FALSE/TRUE
    if any(w in r for w in ("misleading", "partly", "half", "mixture", "exagger", "out of context")):
        return "MISLEADING"
    if any(w in r for w in ("false", "incorrect", "pants on fire", "fake", "hoax", "no evidence")):
        return "FALSE"
    if any(w in r for w in ("true", "correct", "accurate")):
        return "TRUE"
    return "UNVERIFIABLE"


def _slug(text: str, claim_id) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "claim"
    return f"{base}-{str(claim_id)[:8]}"


def _models_used(_path: str) -> dict[str, str]:
    return {
        "normalizer": role("normalizer")["model"],
        "verifier": role("verifier")["model"],
        "synthesizer": role("synthesizer")["model"],
    }


def _evidence_tag(ev: dict, index: int, used: set[str]) -> str:
    raw = str(ev.get("id") or ev.get("evidence_id") or "").strip()
    tag = raw if re.fullmatch(r"e\d+", raw) else f"e{index}"
    if tag not in used:
        return tag

    suffix = index
    while f"e{suffix}" in used:
        suffix += 1
    return f"e{suffix}"


def render_evidence(evidence: list[dict]) -> tuple[str, dict[str, str]]:
    """Render v2 evidence rows for the synthesizer prompt."""
    if not evidence:
        return "(no evidence found)", {}

    lines: list[str] = []
    idmap: dict[str, str] = {}
    used: set[str] = set()
    for i, ev in enumerate(evidence, 1):
        tag = _evidence_tag(ev, i, used)
        used.add(tag)
        idmap[tag] = str(ev.get("id") or ev.get("evidence_id") or "")

        text = (ev.get("content") or ev.get("snippet") or ev.get("summary") or "").strip()
        if len(text) > 1200:
            text = text[:1197].rstrip() + "..."

        meta = [
            ev.get("domain") or ev.get("source") or "",
            f"credibility={ev.get('credibility')}" if ev.get("credibility") is not None else "",
            f"date={ev.get('published_at')}" if ev.get("published_at") else "",
        ]
        meta_text = ", ".join(part for part in meta if part) or "source unknown"
        title = ev.get("title") or ""
        url = ev.get("url") or ""
        stance = f" stance={ev.get('stance')}." if ev.get("stance") else ""
        lines.append(f"[e:{tag}] {meta_text}.{stance} {title} {url}\n{text}".strip())

    return "\n\n".join(lines), idmap


def _enforce_rebuttal(text: str, fallback_url: str | None) -> str:
    """≤400 chars and at least one URL (append the top source if the model omitted one).
    Strips [e:id] citation tags — the rebuttal is a forwardable message, not a cited card."""
    t = re.sub(r"\s*\[e:[^\]]+\]", "", text or "").strip()
    if "http" not in t and fallback_url:
        room = max(0, REBUTTAL_MAX - len(fallback_url) - 1)
        t = (t[:room].rstrip() + " " + fallback_url).strip()
    return t[:REBUTTAL_MAX]


def build_card(claim_id, claim_en, claim_native, verdict, confidence, path,
               evidence: list[dict], out: SynthOutput) -> VerdictCard:
    """Assemble a validated VerdictCard from the model output + deterministic fields.
    Pure (no I/O) so the citation-lock / tag-whitelist / rebuttal gates are unit-testable."""
    explanation, _viol = citations.validate(out.explanation_native)          # citation-lock hard gate
    tags = [t for t in out.manipulation_tags if t in MANIPULATION_TAGS][:3]  # taxonomy whitelist, cap 3
    top = sorted(evidence, key=lambda e: e.get("credibility") or 0, reverse=True)
    ev_refs = [EvidenceRef(url=e["url"], domain=e.get("domain", ""), stance=e.get("stance"),
                           date=e.get("published_at")) for e in top[:5] if e.get("url")]
    path = path or "verify"
    rebuttal = _enforce_rebuttal(out.rebuttal_card_native, ev_refs[0].url if ev_refs else None)
    return VerdictCard(
        slug=_slug(claim_en, claim_id), claim_native=claim_native, claim_en=claim_en,
        verdict=verdict, confidence=confidence, one_liner_native=out.one_liner_native,
        explanation_native=explanation, manipulation_tags=tags, evidence=ev_refs,
        rebuttal_card_native=rebuttal, path=path, models_used=_models_used(path),
    )


async def synthesize(con, job_id, claim_id, *, claim_en, claim_native, lang,
                     verdict, confidence, path="verify", evidence: list[dict], original: str = "") -> VerdictCard:
    await events.emit(job_id, "stage", {"stage": "SYNTHESIZE", "status": "started", "claim_id": str(claim_id)})
    path = path or "verify"
    evidence_text, _idmap = render_evidence(evidence)
    lang = lang or "en"
    user = (f'Original message: "{original or claim_en}"\nClaim: "{claim_en}"\n'
            f'Decided verdict: {verdict} (confidence {confidence}/100)\n\n'
            f'Evidence log:\n{evidence_text}')
    try:
        resp = await nim.call("synthesizer", [
            {"role": "system", "content": SYSTEM.format(lang=lang, tags=", ".join(sorted(MANIPULATION_TAGS)))},
            {"role": "user", "content": user},
        ], response_schema=SynthOutput)
        out: SynthOutput = resp.parsed  # type: ignore[assignment]
    except Exception:
        out = SynthOutput(one_liner_native=f"{verdict}.", explanation_native="", rebuttal_card_native="")

    card = build_card(claim_id, claim_en, claim_native, verdict, confidence, path, evidence, out)
    await con.execute(
        """insert into verdicts (claim_id, slug, verdict, confidence, card, path)
           values ($1, $2, $3, $4, $5::jsonb, $6)
           on conflict (slug) do update set verdict = excluded.verdict, confidence = excluded.confidence,
                                            card = excluded.card, path = excluded.path""",
        claim_id, card.slug, verdict, confidence, json.dumps(card.model_dump()), path,
    )
    await events.emit(job_id, "verdict", {"claim_id": str(claim_id), **card.model_dump()})
    await events.emit(job_id, "stage", {"stage": "SYNTHESIZE", "status": "done", "claim_id": str(claim_id)})
    return card
