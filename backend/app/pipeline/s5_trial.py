"""S5 Trial (LLD §5-S5). On escalation, a Prosecutor (argues FALSE/MISLEADING) and a
Defense (argues TRUE) exchange citation-locked arguments over up to max_rebuttal_rounds,
each allowed ONE extra targeted search. A Judge (a family used nowhere else) rules on an
ANONYMIZED transcript ("Side 1"/"Side 2") + the evidence log → a 5-class Ruling.
A 90s wall-clock budget forces an expedited ruling on the transcript-so-far (LLD §9)."""
import json
import time

from ..config import thresholds
from ..models import Argument, Ruling
from ..services import citations, events, nim, search
from .s4_fastpath import render_evidence

TRIAL_BUDGET = 90.0  # seconds; over budget → judge rules on transcript-so-far (expedited)

PROSECUTOR_SYSTEM = """You are the PROSECUTOR. Argue the claim is FALSE or MISLEADING.
Every factual sentence MUST cite evidence with an [e:id] tag, e.g. [e:e2]. Be concise.
You MAY request ONE extra targeted search by setting search_query (else null).
Return ONLY JSON: {"text": "...", "search_query": null}"""

DEFENSE_SYSTEM = """You are the DEFENSE. Argue the claim is TRUE or defensible.
Every factual sentence MUST cite evidence with an [e:id] tag, e.g. [e:e2]. Be concise.
You MAY request ONE extra targeted search by setting search_query (else null).
Return ONLY JSON: {"text": "...", "search_query": null}"""

JUDGE_SYSTEM = """You are the JUDGE. Two anonymous sides argued about a claim. Read the
transcript and evidence log and rule impartially — do NOT assume which side is correct.
verdict ∈ TRUE | FALSE | MISLEADING | UNVERIFIABLE | CONFLICTING.
Return ONLY JSON: {"verdict":"...","confidence":0.0,"decisive_evidence_ids":["e1"],"reasoning":"..."}"""


def _convo(transcript: list[dict]) -> str:
    return "\n".join(f'{t["side"]} (round {t["round"]}): {t["text"]}' for t in transcript) or "(no arguments)"


def judge_payload(claim: str, evidence_text: str, transcript: list[dict]) -> str:
    """Anonymized payload handed to the Judge — sides are 'Side 1'/'Side 2', no model
    identifiers. Factored out so anonymization can be asserted in a test."""
    return f'Claim: "{claim}"\n\nEvidence log:\n{evidence_text}\n\nTranscript:\n{_convo(transcript)}'


async def _argue(role_name: str, system: str, claim: str, evidence_text: str,
                 transcript: list[dict]) -> Argument | None:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f'Claim: "{claim}"\n\nEvidence log:\n{evidence_text}\n\n'
                                     f'Debate so far:\n{_convo(transcript)}'},
    ]
    try:
        resp = await nim.call(role_name, messages, response_schema=Argument)
        return resp.parsed  # type: ignore[return-value]
    except Exception:
        return None


async def run(con, job_id, claim_id, claim: str, evidence: list[dict]) -> dict:
    await events.emit(job_id, "stage", {"stage": "S5_TRIAL", "status": "started", "claim_id": str(claim_id)})
    evidence_text, _idmap = render_evidence(evidence)
    rounds = thresholds()["max_rebuttal_rounds"]
    transcript: list[dict] = []
    searched = {"prosecutor": False, "defense": False}
    t0 = time.perf_counter()
    expedited = False

    for rnd in range(1, rounds + 1):
        for name, side, system in [("prosecutor", "Side 1", PROSECUTOR_SYSTEM),
                                   ("defense", "Side 2", DEFENSE_SYSTEM)]:
            if time.perf_counter() - t0 > TRIAL_BUDGET:
                expedited = True
                break
            arg = await _argue(name, system, claim, evidence_text, transcript)
            if arg is None:
                continue
            clean, violations = citations.validate(arg.text)      # citation-lock
            entry = {"side": side, "round": rnd, "text": clean}
            transcript.append(entry)
            await events.emit(job_id, "argument", {"claim_id": str(claim_id), "violations": len(violations), **entry})
            # optional single extra targeted search per side
            if arg.search_query and not searched[name]:
                searched[name] = True
                hits = await search.web_search(arg.search_query)
                if hits:
                    evidence_text += "\n" + "\n".join(
                        f"[e:x{i}] {h.get('domain')}: {h.get('title') or ''} — {h.get('snippet') or ''}"
                        for i, h in enumerate(hits[:3], 1))
        if expedited:
            break

    # Judge rules on the anonymized payload (no model names — only Side 1 / Side 2).
    ruling: Ruling | None = None
    try:
        resp = await nim.call("judge", [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": judge_payload(claim, evidence_text, transcript)},
        ], response_schema=Ruling)
        ruling = resp.parsed  # type: ignore[assignment]
    except Exception:
        ruling = None
    if ruling is None:
        ruling = Ruling(verdict="UNVERIFIABLE", confidence=0.3, reasoning="judge unavailable")

    await con.execute(
        "insert into trials (claim_id, transcript, ruling) values ($1, $2::jsonb, $3::jsonb)",
        claim_id, json.dumps(transcript), json.dumps(ruling.model_dump()))
    await events.emit(job_id, "ruling", {"claim_id": str(claim_id), "expedited": expedited, **ruling.model_dump()})
    await events.emit(job_id, "stage", {"stage": "S5_TRIAL", "status": "done", "claim_id": str(claim_id)})
    return {
        "path": "trial", "verdict": ruling.verdict, "confidence": round(100 * ruling.confidence),
        "decisive_evidence_ids": ruling.decisive_evidence_ids, "expedited": expedited,
    }
