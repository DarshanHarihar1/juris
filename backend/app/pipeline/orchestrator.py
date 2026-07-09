"""v2 stage machine: intake -> normalize -> verify -> synthesize.

One verifier agent runs per normalized claim. Claims within a submission execute
concurrently, while each claim keeps a single decision-making context.
"""
import asyncio
import logging

from ..db import pool
from ..services import events, whatsapp
from . import s0_intake, s1_normalize, s6_synthesize, verify

log = logging.getLogger("juris.orchestrator")


def _wa(sub) -> bool:
    """True when this job arrived over WhatsApp and still has a reply address to push to."""
    return sub["channel"] == "whatsapp" and bool(sub["reply_to"])


async def _run_claim(job_id, submission_id, original_text: str, normalized_claim, detected_lang: str) -> None:
    async with (await pool()).acquire() as con:
        claim_id = await con.fetchval(
            """insert into claims (submission_id, text_original, text_norm, text_norm_native, claim_type)
               values ($1, $2, $3, $4, $5) returning id""",
            submission_id,
            original_text,
            normalized_claim.text_norm,
            normalized_claim.text_norm_native,
            normalized_claim.claim_type,
        )

    await events.emit(job_id, "claim", {
        "claim_id": str(claim_id),
        "text_norm": normalized_claim.text_norm,
        "text_norm_native": normalized_claim.text_norm_native,
        "claim_type": normalized_claim.claim_type,
        "volatility": normalized_claim.volatility,
        "as_of_date": normalized_claim.as_of_date,
    })
    log.info("job=%s claim=%s norm=%r type=%s", job_id, claim_id, normalized_claim.text_norm, normalized_claim.claim_type)

    verdict, evidence = await verify.verify_with_evidence(
        job_id,
        normalized_claim,
        claim_id=claim_id,
        lang=detected_lang,
    )
    log.info("job=%s claim=%s VERDICT %s conf=%s path=verify",
             job_id, claim_id, verdict.verdict, verdict.confidence)

    async with (await pool()).acquire() as con:
        await s6_synthesize.synthesize(
            con,
            job_id,
            claim_id,
            claim_en=normalized_claim.text_norm,
            claim_native=normalized_claim.text_norm_native,
            lang=detected_lang,
            verdict=verdict.verdict,
            confidence=verdict.confidence,
            path="verify",
            evidence=evidence,
            original=original_text,
        )


async def run(job: dict) -> None:
    job_id = job["id"]
    submission_id = job["submission_id"]
    if submission_id is None:
        raise ValueError("job has no submission_id (use POST /api/verify)")
    log.info("job=%s start submission=%s", job_id, submission_id)

    async with (await pool()).acquire() as con:
        sub = await con.fetchrow(
            "select media_type, raw_text, media_uri, detected_lang, channel, reply_to from submissions where id = $1",
            submission_id)
    if sub is None:
        raise ValueError(f"submission {submission_id} not found")

    # S0 — intake: resolve text / url / image down to plain text for S1.
    await events.emit(job_id, "stage", {"stage": "INTAKE", "status": "started", "media_type": sub["media_type"]})
    text = await s0_intake.intake(sub["media_type"], sub["raw_text"], sub["media_uri"])
    await events.emit(job_id, "stage", {"stage": "INTAKE", "status": "done"})

    # url fetch / OCR yielded nothing → terminal (mirrors the zero-claim guard below).
    if not text:
        msg = "Couldn't extract any text to verify from that input."
        await events.emit(job_id, "terminal", {"reason": "nothing_to_verify", "message": msg})
        if _wa(sub):
            async with (await pool()).acquire() as con:
                await whatsapp.deliver_text(con, submission_id, sub["reply_to"], msg)
        return

    # S1 — normalize & decompose
    await events.emit(job_id, "stage", {"stage": "NORMALIZE", "status": "started"})
    norm = await s1_normalize.normalize(text, sub["detected_lang"])
    await events.emit(job_id, "stage", {"stage": "NORMALIZE", "status": "done", "lang": norm.detected_lang})

    async with (await pool()).acquire() as con:
        await con.execute("update submissions set detected_lang = $2 where id = $1", submission_id, norm.detected_lang)

        # Guard: nothing checkable → terminal reply, job ends (cost floor, LLD §5-S1).
        if not norm.claims:
            msg = "No checkable factual claim found — nothing to verify."
            await events.emit(job_id, "terminal", {"reason": "nothing_to_verify", "message": msg})
            if _wa(sub):
                await whatsapp.deliver_text(con, submission_id, sub["reply_to"], msg)
            return

        await asyncio.gather(*[
            _run_claim(job_id, submission_id, text, nc, norm.detected_lang)
            for nc in norm.claims
        ])

        # All claims decided — push the verdict card(s) back over WhatsApp and clear reply_to.
        if _wa(sub):
            await whatsapp.deliver_verdicts(con, submission_id, sub["reply_to"])
