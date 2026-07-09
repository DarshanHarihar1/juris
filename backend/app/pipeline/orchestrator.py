"""v2 stage machine: intake -> normalize -> verify (N) -> verdict (AND + format/summary).

Normalize yields `{ language, sub_claims }`. One verifier per sub-claim, then one
Stage-3 card for the submission.
"""
import asyncio
import logging

from langsmith import traceable

from ..db import pool
from ..services import events, search as search_svc, whatsapp
from . import s0_intake, s1_normalize, s6_synthesize, verify
from .s6_synthesize import VerifiedPart

log = logging.getLogger("juris.orchestrator")


def _wa(sub) -> bool:
    """True when this job arrived over WhatsApp and still has a reply address to push to."""
    return sub["channel"] == "whatsapp" and bool(sub["reply_to"])


def _ls_meta(job_id, **extra) -> dict:
    return {"metadata": {"job_id": str(job_id), **{k: str(v) if v is not None else None for k, v in extra.items()}}}


async def _verify_one(job_id, submission_id, original_text: str, sub_claim: str, language: str) -> VerifiedPart:
    async with (await pool()).acquire() as con:
        claim_id = await con.fetchval(
            """insert into claims (submission_id, text_original, text_norm, text_norm_native, claim_type)
               values ($1, $2, $3, $4, $5) returning id""",
            submission_id,
            original_text,
            sub_claim,
            sub_claim,
            "factual",
        )

    await events.emit(job_id, "claim", {
        "claim_id": str(claim_id),
        "text_norm": sub_claim,
        "language": language,
    })
    log.info("job=%s claim=%s norm=%r", job_id, claim_id, sub_claim)

    scv = await verify.verify_with_evidence(
        job_id,
        sub_claim,
        claim_id=claim_id,
        lang=language,
    )
    log.info("job=%s claim=%s VERDICT %s evidence=%d",
             job_id, claim_id, scv.verdict, len(scv.evidence))
    return VerifiedPart(claim_id=claim_id, sub_claim=sub_claim, scv=scv)


@traceable(name="job", run_type="chain")
async def run(job: dict) -> None:
    job_id = job["id"]
    submission_id = job["submission_id"]
    if submission_id is None:
        raise ValueError("job has no submission_id (use POST /api/verify)")
    log.info("job=%s start submission=%s", job_id, submission_id)
    warm_task = asyncio.create_task(search_svc.warm_searxng())

    async with (await pool()).acquire() as con:
        sub = await con.fetchrow(
            "select media_type, raw_text, media_uri, detected_lang, channel, reply_to from submissions where id = $1",
            submission_id)
    if sub is None:
        raise ValueError(f"submission {submission_id} not found")

    await events.emit(job_id, "stage", {"stage": "INTAKE", "status": "started", "media_type": sub["media_type"]})
    text = await s0_intake.intake(sub["media_type"], sub["raw_text"], sub["media_uri"])
    await events.emit(job_id, "stage", {"stage": "INTAKE", "status": "done"})

    if not text:
        warm_task.cancel()
        msg = "Couldn't extract any text to verify from that input."
        await events.emit(job_id, "terminal", {"reason": "nothing_to_verify", "message": msg})
        if _wa(sub):
            async with (await pool()).acquire() as con:
                await whatsapp.deliver_text(con, submission_id, sub["reply_to"], msg)
        return

    await events.emit(job_id, "stage", {"stage": "NORMALIZE", "status": "started"})
    norm = await s1_normalize.normalize(
        text, sub["detected_lang"],
        langsmith_extra=_ls_meta(job_id, submission_id=submission_id),
    )
    await events.emit(job_id, "stage", {
        "stage": "NORMALIZE", "status": "done",
        "lang": norm.language, "sub_claim_count": len(norm.sub_claims),
    })

    async with (await pool()).acquire() as con:
        await con.execute("update submissions set detected_lang = $2 where id = $1", submission_id, norm.language)

        if not norm.sub_claims:
            warm_task.cancel()
            msg = "No checkable factual claim found — nothing to verify."
            await events.emit(job_id, "terminal", {"reason": "nothing_to_verify", "message": msg})
            if _wa(sub):
                await whatsapp.deliver_text(con, submission_id, sub["reply_to"], msg)
            return

        await warm_task

        parts = list(await asyncio.gather(*[
            _verify_one(job_id, submission_id, text, sc, norm.language)
            for sc in norm.sub_claims
        ]))

        await s6_synthesize.verdict_stage(
            con,
            job_id,
            claim_id=parts[0].claim_id,
            original=text,
            lang=norm.language,
            parts=parts,
            langsmith_extra=_ls_meta(job_id, submission_id=submission_id),
        )

        if _wa(sub):
            await whatsapp.deliver_verdicts(con, submission_id, sub["reply_to"])
