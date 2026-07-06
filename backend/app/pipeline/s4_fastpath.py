"""S4 Fast Path (LLD §5-S4). Three jurors from different families each read the
normalized claim + full evidence log (no browsing) and return a structured verdict.
Agreement = confidence-weighted plurality share; ≥ agreement_theta → consensus (→S6),
otherwise the orchestrator escalates to the S5 trial."""
import asyncio

from ..config import role, thresholds
from ..models import JurorVote
from ..services import events, nim

JUROR_SYSTEM = """You are a fact-checking JUROR. Judge ONLY on the evidence log given —
you may not browse. Weigh each source's credibility and stance toward the claim.

verdict ∈ TRUE | FALSE | MISLEADING | UNVERIFIABLE | CONFLICTING
- UNVERIFIABLE when the evidence is too thin to decide.
- CONFLICTING when credible sources genuinely disagree.
confidence ∈ 0..1. Cite the evidence tags (e1, e2, …) your verdict leans on.

Return ONLY JSON: {"verdict": "...", "confidence": 0.0, "key_evidence_ids": ["e1"], "reasoning_short": "..."}"""


def render_evidence(evidence: list[dict]) -> tuple[str, dict[str, str]]:
    """Render the evidence log with stable [e:eN] tags. Returns (text, tag→row-id map)."""
    if not evidence:
        return "(no evidence found)", {}
    lines, idmap = [], {}
    for i, ev in enumerate(evidence, 1):
        tag = f"e{i}"
        idmap[tag] = str(ev.get("id") or "")
        lines.append(
            f"[e:{tag}] {ev.get('domain')} (stance={ev.get('stance')}, "
            f"credibility={ev.get('credibility')}): {ev.get('title') or ''} — {ev.get('snippet') or ''}"
        )
    return "\n".join(lines), idmap


def agreement(votes: list[JurorVote]) -> tuple[str, float, int]:
    """Confidence-weighted plurality. Returns (winning_verdict, share, confidence_0_100).
    share = winner_weight / total_weight; confidence = mean confidence of the winners.
    e.g. [FALSE@0.9, FALSE@0.8, TRUE@0.4] → ("FALSE", 1.7/2.1 ≈ 0.81, 85)."""
    weight: dict[str, float] = {}
    for v in votes:
        c = max(0.0, min(1.0, v.confidence))
        weight[v.verdict] = weight.get(v.verdict, 0.0) + c
    total = sum(weight.values())
    if total == 0:
        return "UNVERIFIABLE", 0.0, 0
    winner = max(weight, key=lambda k: weight[k])
    share = weight[winner] / total
    winners = [max(0.0, min(1.0, v.confidence)) for v in votes if v.verdict == winner]
    conf = round(100 * (sum(winners) / len(winners))) if winners else 0
    return winner, share, conf


async def _vote(model: str, claim: str, evidence_text: str) -> JurorVote | None:
    messages = [
        {"role": "system", "content": JUROR_SYSTEM},
        {"role": "user", "content": f'Claim: "{claim}"\n\nEvidence log:\n{evidence_text}'},
    ]
    try:
        resp = await nim.call(None, messages, response_schema=JurorVote, model_id=model)
        return resp.parsed  # type: ignore[return-value]
    except Exception:
        return None  # a dead juror doesn't sink the vote; the remaining quorum decides


async def deliberate(job_id, claim_id, claim: str, evidence: list[dict]) -> dict | None:
    """Run the 3 jurors in parallel. Return a consensus result dict, or None to escalate."""
    await events.emit(job_id, "stage", {"stage": "S4_FASTPATH", "status": "started", "claim_id": str(claim_id)})
    evidence_text, _idmap = render_evidence(evidence)
    jurors = role("fastpath_jury")
    votes = [v for v in await asyncio.gather(*[_vote(m, claim, evidence_text) for m in jurors]) if v is not None]

    if not votes:
        await events.emit(job_id, "stage", {"stage": "S4_FASTPATH", "status": "done",
                                            "claim_id": str(claim_id), "agreement": 0.0, "verdict": None})
        return None
    verdict, share, conf = agreement(votes)
    theta = thresholds()["agreement_theta"]
    await events.emit(job_id, "stage", {"stage": "S4_FASTPATH", "status": "done", "claim_id": str(claim_id),
                                        "agreement": round(share, 3), "verdict": verdict})
    if share < theta:
        return None  # escalate
    return {
        "path": "consensus", "verdict": verdict, "confidence": conf, "agreement": round(share, 3),
        "key_evidence_ids": sorted({e for v in votes if v.verdict == verdict for e in v.key_evidence_ids}),
    }
