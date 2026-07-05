"""Stage machine (LLD §3.2 event protocol). Wires S0→S2:
intake → normalize → persist+embed claims → precedent short-circuit (cache/human
fact-check). Zero-claim guard ends the job. S3→S6 append in later phases."""
from ..db import pool
from ..services import events, nim
from . import s0_intake, s1_normalize, s2_precedent


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

        embeddings = await nim.embed([nc.text_norm for nc in norm.claims])
        for nc, emb in zip(norm.claims, embeddings):
            claim_id = await con.fetchval(
                """insert into claims (submission_id, text_original, text_norm, text_norm_native, claim_type, embedding)
                   values ($1, $2, $3, $4, $5, $6::vector) returning id""",
                submission_id, text, nc.text_norm, nc.text_norm_native, nc.claim_type, s2_precedent.vec(emb),
            )
            await events.emit(job_id, "claim", {
                "claim_id": str(claim_id), "text_norm": nc.text_norm, "claim_type": nc.claim_type,
            })

            # S2 — precedent short-circuit (cache / human fact-check)
            await events.emit(job_id, "stage", {"stage": "S2_PRECEDENT", "status": "started"})
            sc = await s2_precedent.check(con, claim_id, emb, nc.text_norm)
            if sc:
                await events.emit(job_id, "verdict", {"claim_id": str(claim_id), **sc})
            else:
                await events.emit(job_id, "stage", {"stage": "S2_PRECEDENT", "status": "miss",
                                                    "claim_id": str(claim_id)})
    # S3 investigation → S6 synthesis land in later phases (S2 misses escalate there).
