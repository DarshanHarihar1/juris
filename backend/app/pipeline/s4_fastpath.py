"""S4 Fast Path (LLD §5-S4). Three jurors (different families) read the QA
evidence log — answered sub-questions, not stance-tagged snippets — and vote.

Each juror declares a PRIOR (their own knowledge) before reading evidence, then
arbitrates: evidence governs when it directly addresses the claim; a confident
unanimous prior governs when evidence is thin; both weak → UNVERIFIABLE (1b gate).

Confidence-weighted plurality share ≥ agreement_theta → consensus (→S6),
otherwise orchestrator escalates to S5 trial."""
import asyncio
from collections import defaultdict

from ..config import role, thresholds
from ..models import JurorVote
from ..services import events, nim

JUROR_SYSTEM = """You are a fact-checking JUROR. Work in two steps.

STEP 1 — PRIOR (before reading evidence):
State what you already know about this claim.
  prior_verdict: your best guess from your own knowledge
  prior_confidence: how confident you are (0–1; 0 = you don't know)

STEP 2 — EVIDENCE (read the QA evidence log):
Each entry is a sub-question with a grounded answer from fetched web pages.
  evidence_addresses_claim: does the evidence DIRECTLY answer the claim? (true/false)

ARBITRATION RULE:
- If evidence directly addresses the claim → evidence governs; set verdict from evidence.
- If evidence is thin/unanswerable AND prior_confidence > 0.8 → prior governs; set verdict = prior_verdict.
- If both evidence and prior are weak → verdict = UNVERIFIABLE.

verdict ∈ TRUE | FALSE | MISLEADING | UNVERIFIABLE | CONFLICTING
- UNVERIFIABLE: evidence too thin AND prior not confident
- CONFLICTING: credible evidence sources genuinely disagree
confidence: your final confidence in the verdict (0–1)

Return ONLY JSON:
{"prior_verdict": "...", "prior_confidence": 0.0, "evidence_addresses_claim": true,
 "verdict": "...", "confidence": 0.0, "key_evidence_ids": ["e1"], "reasoning_short": "..."}"""


def render_evidence(evidence: list[dict]) -> tuple[str, dict[str, str]]:
    """Render evidence log for jurors/trial.

    QA rows (have 'question'): grouped by question into answered-question blocks.
    Legacy rows (have 'stance'): rendered as before, for back-compat with cached evidence.
    Returns (text, tag→row-id map).
    """
    if not evidence:
        return "(no evidence found)", {}

    is_qa = any(ev.get("question") for ev in evidence)

    if is_qa:
        by_q: dict[str, list[dict]] = defaultdict(list)
        for ev in evidence:
            by_q[ev.get("question") or ""].append(ev)

        lines, idmap = [], {}
        for i, (q, evs) in enumerate(by_q.items(), 1):
            tag = f"e{i}"
            idmap[tag] = str(evs[0].get("id") or "")
            first = evs[0]
            answerable = first.get("answerable", True)
            answer = first.get("answer") or ""
            sources = ", ".join(
                f"{ev.get('domain')} (cred={ev.get('credibility')})"
                for ev in evs[:3] if ev.get("domain")
            ) or "(no sources)"
            ans_line = answer if (answerable and answer) else "(no direct answer found)"
            lines.append(
                f"[e:{tag}] Q: {q}\n"
                f"        A: {ans_line}\n"
                f"        Sources: {sources}"
            )
        return "\n\n".join(lines), idmap

    # Legacy stance-based rendering (back-compat)
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
    """Confidence-weighted plurality. Returns (verdict, share, conf_0_100).

    e.g. [FALSE@0.9, FALSE@0.8, TRUE@0.4] → ("FALSE", 1.7/2.1 ≈ 0.81, 85)."""
    weight: dict[str, float] = {}
    for v in votes:
        w = max(0.0, min(1.0, v.confidence))
        weight[v.verdict] = weight.get(v.verdict, 0.0) + w
    total = sum(weight.values())
    if not total:
        return "UNVERIFIABLE", 0.0, 0
    winner = max(weight, key=weight.__getitem__)
    share = weight[winner] / total
    winners = [v for v in votes if v.verdict == winner]
    conf = round(sum(max(0.0, min(1.0, v.confidence)) for v in winners) * 100 / len(winners)) if winners else 0
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
        return None  # dead juror doesn't sink the vote; remaining quorum decides


async def deliberate(job_id, claim_id, claim: str, evidence: list[dict]) -> dict | None:
    """Run 3 jurors in parallel. Return consensus result dict, or None to escalate to S5."""
    await events.emit(job_id, "stage", {"stage": "S4_FASTPATH", "status": "started",
                                        "claim_id": str(claim_id)})
    evidence_text, _idmap = render_evidence(evidence)
    jurors = role("fastpath_jury")
    votes = [v for v in await asyncio.gather(*[_vote(m, claim, evidence_text) for m in jurors])
             if v is not None]

    if not votes:
        await events.emit(job_id, "stage", {"stage": "S4_FASTPATH", "status": "done",
                                            "claim_id": str(claim_id), "agreement": 0.0, "verdict": None})
        return None

    # 1b gate: if ALL jurors say evidence doesn't address the claim AND priors are weak →
    # return UNVERIFIABLE rather than emitting a confident verdict built on noise.
    floor = thresholds().get("prior_confidence_floor", 0.6)
    all_unanswerable = all(not v.evidence_addresses_claim for v in votes)
    avg_prior_conf = sum(v.prior_confidence for v in votes) / len(votes)
    if all_unanswerable and avg_prior_conf < floor:
        await events.emit(job_id, "stage", {"stage": "S4_FASTPATH", "status": "done",
                                            "claim_id": str(claim_id), "agreement": 1.0,
                                            "verdict": "UNVERIFIABLE"})
        return {
            "path": "consensus", "verdict": "UNVERIFIABLE", "confidence": 25,
            "agreement": 1.0, "key_evidence_ids": [],
        }

    verdict, share, conf = agreement(votes)
    theta = thresholds()["agreement_theta"]
    await events.emit(job_id, "stage", {"stage": "S4_FASTPATH", "status": "done",
                                        "claim_id": str(claim_id),
                                        "agreement": round(share, 3), "verdict": verdict})
    if share < theta:
        return None  # escalate to S5 trial
    return {
        "path": "consensus", "verdict": verdict, "confidence": conf,
        "agreement": round(share, 3),
        "key_evidence_ids": sorted({e for v in votes if v.verdict == verdict
                                    for e in v.key_evidence_ids}),
    }


