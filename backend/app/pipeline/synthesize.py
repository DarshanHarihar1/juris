"""Stage 3 — Verdict: AND-combine + format (1 claim) or summarize (N≥2).

Single sub-claim: no LLM — format verify agent's {verdict, explanation, evidence}.
Multi: rule-based AND label + one gpt-oss-120b summary call (MeshAPI).
"""
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from langsmith import traceable
from pydantic import BaseModel, field_validator

from ..config import role
from ..models import EvidenceRef, SubClaimVerdict, SynthOutput, VerdictCard, to_db_verdict
from ..services import events, mesh

REBUTTAL_MAX = 400

SUMMARY_SYSTEM = """You write a short WhatsApp-forwardable fact-check summary.

The combined verdict label is already DECIDED by rules — do not change or soften it.
Combined verdict: {verdict}

Write in the user's language ({lang}):
- explanation: 2–5 sentences that cover ALL sub-claim findings (do not drop a false
  or unverifiable part). Stay consistent with the combined verdict.
- Keep it plain text (no JSON, no markdown fences).

Return ONLY JSON: {{"explanation":"..."}}"""

LOCALIZE_SYSTEM = """Translate this fact-check explanation into the user's language ({lang}).
Keep the meaning exact and preserve any URLs verbatim. Plain text only.

Return ONLY JSON: {{"explanation":"..."}}"""


class SummaryOutput(BaseModel):
    explanation: str

    @field_validator("explanation")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        text = (v or "").strip()
        if not text:
            raise ValueError("explanation must be non-empty")
        return text


@dataclass
class VerifiedPart:
    claim_id: object
    sub_claim: str
    scv: SubClaimVerdict


def and_combine(verdicts: list[str]) -> str:
    """Rule-based AND (false-dominates). Pure — no LLM."""
    labels = [(v or "").strip().lower() for v in verdicts]
    if not labels:
        return "unverifiable"
    if any(v == "false" for v in labels):
        return "false"
    if all(v == "true" for v in labels):
        return "true"
    return "unverifiable"


def confidence_for(verdict: str) -> int:
    return {"true": 80, "false": 80, "unverifiable": 40}.get(
        (verdict or "").strip().lower(), 40
    )


def _slug(text: str, claim_id) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "claim"
    return f"{base}-{str(claim_id)[:8]}"


def _models_used() -> dict[str, str]:
    return {
        "normalizer": role("normalizer")["model"],
        "verifier": role("verifier")["model"],
        "synthesizer": role("synthesizer")["model"],
    }


def _domain(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower().removeprefix("www.")
    except Exception:
        return ""


def _evidence_rows(parts: list[VerifiedPart]) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for p in parts:
        for u in p.scv.evidence:
            if u and u not in seen:
                seen.add(u)
                rows.append({"url": u, "domain": _domain(u), "snippet": ""})
    return rows


def _enforce_rebuttal(text: str, fallback_url: str | None) -> str:
    t = re.sub(r"\s*\[e:[^\]]+\]", "", text or "").strip()
    if "http" not in t and fallback_url:
        room = max(0, REBUTTAL_MAX - len(fallback_url) - 1)
        t = (t[:room].rstrip() + " " + fallback_url).strip()
    return t[:REBUTTAL_MAX]


def _concat_fallback(parts: list[VerifiedPart], combined: str) -> str:
    bits = [f"[{p.scv.verdict}] {p.sub_claim}: {p.scv.explanation}" for p in parts]
    return f"Combined verdict: {combined}. " + " | ".join(bits)


def format_single(scv: SubClaimVerdict) -> SynthOutput:
    """N=1: no LLM — thin template over verify output. English stays LLM-free;
    non-English is localized separately in verdict_stage (see _localize)."""
    urls = list(scv.evidence)
    return SynthOutput(
        one_liner_native=f"{to_db_verdict(scv.verdict)}.",
        explanation_native=scv.explanation.strip(),
        rebuttal_card_native=_enforce_rebuttal(scv.explanation, urls[0] if urls else None),
    )


async def _localize(out: SynthOutput, lang: str, fallback_url: str | None) -> SynthOutput:
    """Translate the single-claim explanation into `lang` so a Hindi (etc.) forward
    gets a reply in the same language. One small call; on error keep the English."""
    try:
        resp = await mesh.call(
            "synthesizer",
            [
                {"role": "system", "content": LOCALIZE_SYSTEM.format(lang=lang)},
                {"role": "user", "content": out.explanation_native},
            ],
            response_schema=SummaryOutput,
        )
        text = resp.parsed.explanation.strip() if resp.parsed else ""
    except Exception:
        text = ""
    if not text:
        return out
    return SynthOutput(
        one_liner_native=out.one_liner_native,
        explanation_native=text,
        rebuttal_card_native=_enforce_rebuttal(text, fallback_url),
    )


async def summarize_multi(
    parts: list[VerifiedPart], combined: str, lang: str,
) -> SynthOutput:
    """N≥2: exactly one LLM call for explanation; label already decided."""
    lang = lang or "en"
    lines = []
    for i, p in enumerate(parts, 1):
        urls = ", ".join(p.scv.evidence[:3]) or "(none)"
        lines.append(
            f"{i}. sub_claim={p.sub_claim!r}\n"
            f"   verdict={p.scv.verdict}\n"
            f"   explanation={p.scv.explanation}\n"
            f"   evidence={urls}"
        )
    user = (
        f"Combined verdict (FIXED): {combined}\n\n"
        f"Sub-claim findings:\n" + "\n\n".join(lines)
    )
    try:
        resp = await mesh.call(
            "synthesizer",
            [
                {"role": "system", "content": SUMMARY_SYSTEM.format(
                    verdict=combined, lang=lang,
                )},
                {"role": "user", "content": user},
            ],
            response_schema=SummaryOutput,
        )
        explanation = resp.parsed.explanation if resp.parsed else _concat_fallback(parts, combined)
    except Exception:
        explanation = _concat_fallback(parts, combined)

    urls = [u for p in parts for u in p.scv.evidence]
    return SynthOutput(
        one_liner_native=f"{to_db_verdict(combined)}.",
        explanation_native=explanation,
        rebuttal_card_native=_enforce_rebuttal(explanation, urls[0] if urls else None),
    )


def build_card(claim_id, claim_en, claim_native, verdict, confidence, path,
               evidence: list[dict], out: SynthOutput) -> VerdictCard:
    """Assemble a VerdictCard. Pure (no I/O) for unit tests."""
    ev_refs = [
        EvidenceRef(url=e["url"], domain=e.get("domain") or _domain(e["url"]),
                    stance=e.get("stance"),
                    date=str(e["published_at"]) if e.get("published_at") else None)
        for e in evidence[:5] if e.get("url")
    ]
    path = path or "verify"
    rebuttal = _enforce_rebuttal(out.rebuttal_card_native, ev_refs[0].url if ev_refs else None)
    return VerdictCard(
        slug=_slug(claim_en, claim_id), claim_native=claim_native, claim_en=claim_en,
        verdict=verdict, confidence=confidence, one_liner_native=out.one_liner_native,
        explanation_native=out.explanation_native or "", evidence=ev_refs,
        rebuttal_card_native=rebuttal, path=path, models_used=_models_used(),
        manipulation_tags=[],
    )


# Kept for older tests / eval adapters that still call render_evidence.
def render_evidence(evidence: list[dict]) -> tuple[str, dict[str, str]]:
    if not evidence:
        return "(no evidence found)", {}
    lines, idmap, used = [], {}, set()
    for i, ev in enumerate(evidence, 1):
        tag = f"e{i}"
        used.add(tag)
        idmap[tag] = str(ev.get("id") or "")
        text = (ev.get("content") or ev.get("snippet") or "").strip()
        if len(text) > 1200:
            text = text[:1197].rstrip() + "..."
        lines.append(f"[e:{tag}] {ev.get('domain') or ''} {ev.get('url') or ''}\n{text}".strip())
    return "\n\n".join(lines), idmap


@traceable(name="verdict", run_type="chain")
async def verdict_stage(
    con, job_id, *,
    claim_id,
    original: str,
    lang: str,
    parts: list[VerifiedPart],
) -> VerdictCard:
    """Stage 3 entry: AND-combine + format or summarize, then persist one card."""
    await events.emit(job_id, "stage", {
        "stage": "VERDICT", "status": "started", "sub_claim_count": len(parts),
    })
    combined = and_combine([p.scv.verdict for p in parts])
    db_verdict = to_db_verdict(combined)
    confidence = confidence_for(combined)
    evidence = _evidence_rows(parts)

    if len(parts) == 1:
        out = format_single(parts[0].scv)
        if (lang or "en") != "en":
            urls = list(parts[0].scv.evidence)
            out = await _localize(out, lang, urls[0] if urls else None)
        claim_en = parts[0].sub_claim
        claim_native = parts[0].sub_claim
    else:
        out = await summarize_multi(parts, combined, lang)
        claim_en = original or " | ".join(p.sub_claim for p in parts)
        claim_native = claim_en

    card = build_card(
        claim_id, claim_en, claim_native, db_verdict, confidence, "verify", evidence, out,
    )
    await con.execute(
        """insert into verdicts (claim_id, slug, verdict, confidence, card, path)
           values ($1, $2, $3, $4, $5::jsonb, $6)
           on conflict (slug) do update set verdict = excluded.verdict, confidence = excluded.confidence,
                                            card = excluded.card, path = excluded.path""",
        claim_id, card.slug, db_verdict, confidence, json.dumps(card.model_dump()), "verify",
    )
    await events.emit(job_id, "verdict", {"claim_id": str(claim_id), **card.model_dump()})
    await events.emit(job_id, "stage", {
        "stage": "VERDICT", "status": "done", "verdict": combined, "sub_claim_count": len(parts),
    })
    return card


# Back-compat alias used by older tests / eval.
async def synthesize(con, job_id, claim_id, *, claim_en, claim_native, lang,
                     verdict, confidence, path="verify", evidence: list[dict],
                     original: str = "", explanation_seed: str = "", **_kw) -> VerdictCard:
    """Legacy single-claim persist path (explanation_seed → no LLM)."""
    raw = str(verdict or "unverifiable")
    mapped = {"TRUE": "true", "FALSE": "false", "UNVERIFIABLE": "unverifiable"}.get(raw, raw.lower())
    scv = SubClaimVerdict(
        verdict=mapped,  # type: ignore[arg-type]
        explanation=(explanation_seed or f"{raw}.").strip(),
        evidence=[e["url"] for e in evidence if e.get("url")],
    )
    part = VerifiedPart(claim_id=claim_id, sub_claim=claim_en or claim_native, scv=scv)
    return await verdict_stage(
        con, job_id, claim_id=claim_id, original=original or claim_en,
        lang=lang or "en", parts=[part],
    )
