"""Stage machine (LLD §3.2 event protocol). Phase 1 wires S0→S1:
intake → normalize → persist claims → emit stage/claim events. Zero-claim guard
ends the job with a terminal "nothing to verify". S2→S6 append in later phases."""
from ..db import pool
from ..services import events
from . import s0_intake, s1_normalize


async def run(job: dict) -> None:
    job_id = job["id"]
    submission_id = job["submission_id"]
    if submission_id is None:
        raise ValueError("job has no submission_id (use POST /api/verify)")

    async with (await pool()).acquire() as con:
        sub = await con.fetchrow("select raw_text, detected_lang from submissions where id = $1", submission_id)
    if sub is None:
        raise ValueError(f"submission {submission_id} not found")

    # S0 — intake (text-only v1)
    await events.emit(job_id, "stage", {"stage": "S0_INTAKE", "status": "started"})
    text = s0_intake.intake(sub["raw_text"] or "")
    await events.emit(job_id, "stage", {"stage": "S0_INTAKE", "status": "done"})

    # S1 — normalize & decompose
    await events.emit(job_id, "stage", {"stage": "S1_NORMALIZE", "status": "started"})
    norm = await s1_normalize.normalize(text, sub["detected_lang"])
    await events.emit(job_id, "stage", {"stage": "S1_NORMALIZE", "status": "done", "lang": norm.detected_lang})

    async with (await pool()).acquire() as con:
        await con.execute("update submissions set detected_lang = $2 where id = $1", submission_id, norm.detected_lang)

        # Guard: nothing checkable → terminal reply, job ends (cost floor, LLD §5-S1).
        if not norm.claims:
            await events.emit(job_id, "terminal", {
                "reason": "nothing_to_verify",
                "message": "No checkable factual claim found — nothing to verify.",
            })
            return

        for nc in norm.claims:
            claim_id = await con.fetchval(
                """insert into claims (submission_id, text_original, text_norm, text_norm_native, claim_type)
                   values ($1, $2, $3, $4, $5) returning id""",
                submission_id, text, nc.text_norm, nc.text_norm_native, nc.claim_type,
            )
            await events.emit(job_id, "claim", {
                "claim_id": str(claim_id), "text_norm": nc.text_norm, "claim_type": nc.claim_type,
            })
    # S2 precedent → S6 synthesis land in later phases.
