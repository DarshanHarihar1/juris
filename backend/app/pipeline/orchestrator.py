"""Stage machine (LLD §3.2 event protocol). Full pipeline S0→S6:
intake → normalize → persist+embed claims → precedent short-circuit (cache/human
fact-check) → on a miss, parallel investigation → fast-path jury or adversarial trial →
synthesis into the user-facing VerdictCard. Zero-claim guard ends the job."""
import logging

from ..db import pool
from ..services import credibility, events, nim
from . import (s0_intake, s1_normalize, s2_precedent, s3_investigate, s4_fastpath,
               s5_trial, s6_synthesize)

log = logging.getLogger("juris.orchestrator")


async def run(job: dict) -> None:
    job_id = job["id"]
    submission_id = job["submission_id"]
    if submission_id is None:
        raise ValueError("job has no submission_id (use POST /api/verify)")
    log.info("job=%s start submission=%s", job_id, submission_id)

    async with (await pool()).acquire() as con:
        sub = await con.fetchrow(
            "select media_type, raw_text, media_uri, detected_lang from submissions where id = $1",
            submission_id)
    if sub is None:
        raise ValueError(f"submission {submission_id} not found")

    # S0 — intake: resolve text / url / image down to plain text for S1.
    await events.emit(job_id, "stage", {"stage": "S0_INTAKE", "status": "started",
                                        "media_type": sub["media_type"]})
    text = await s0_intake.intake(sub["media_type"], sub["raw_text"], sub["media_uri"])
    await events.emit(job_id, "stage", {"stage": "S0_INTAKE", "status": "done"})

    # url fetch / OCR yielded nothing → terminal (mirrors the zero-claim guard below).
    if not text:
        await events.emit(job_id, "terminal", {
            "reason": "nothing_to_verify",
            "message": "Couldn't extract any text to verify from that input.",
        })
        return

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
            log.info("job=%s claim=%s norm=%r type=%s", job_id, claim_id, nc.text_norm, nc.claim_type)

            # S2 — precedent short-circuit (cache / human fact-check)
            await events.emit(job_id, "stage", {"stage": "S2_PRECEDENT", "status": "started"})
            sc = await s2_precedent.check(con, claim_id, emb, nc.text_norm)
            if sc and sc["path"] == "cache":
                # already-synthesized card from a prior run. ponytail: re-emit as-is;
                # re-translating into the user's language is a nice-to-have, not v1.
                await events.emit(job_id, "stage", {"stage": "S2_PRECEDENT", "status": "done",
                                                    "claim_id": str(claim_id), "hit": "cache"})
                await events.emit(job_id, "verdict", {"claim_id": str(claim_id), **sc["card"], "path": "cache"})
                log.info("job=%s claim=%s VERDICT %s path=cache (sim=%.3f)",
                         job_id, claim_id, sc.get("verdict"), sc.get("similarity", 0.0))
                continue
            if sc and sc["path"] == "precedent":
                fc = sc["fact_check"]
                await events.emit(job_id, "stage", {"stage": "S2_PRECEDENT", "status": "done",
                                                    "claim_id": str(claim_id), "hit": "precedent"})
                log.info("job=%s claim=%s -> precedent %s (sim=%.3f) %s", job_id, claim_id,
                         s6_synthesize.rating_to_class(fc.get("rating")), sc.get("similarity", 0.0), fc.get("url"))
                ev = [{"url": fc["url"], "domain": fc["domain"], "title": fc.get("title"),
                       "snippet": fc.get("claim") or fc.get("rating"), "stance": "refutes",
                       "credibility": credibility.score(fc["domain"]), "published_at": fc.get("published_at")}]
                await s6_synthesize.synthesize(
                    con, job_id, claim_id, claim_en=nc.text_norm, claim_native=nc.text_norm_native,
                    lang=norm.detected_lang, verdict=s6_synthesize.rating_to_class(fc.get("rating")),
                    confidence=80, path="precedent", evidence=ev, original=text)
                continue
            await events.emit(job_id, "stage", {"stage": "S2_PRECEDENT", "status": "miss",
                                                "claim_id": str(claim_id)})

            # S3 — parallel investigation gathers a cited evidence log for the miss.
            evidence = await s3_investigate.investigate(con, job_id, claim_id, nc.text_norm, nc.text_norm_native)
            log.info("job=%s claim=%s S2 miss -> S3 gathered %d evidence rows", job_id, claim_id, len(evidence))

            # S4 — fast-path jury; consensus resolves cheaply, otherwise escalate to trial.
            result = await s4_fastpath.deliberate(job_id, claim_id, nc.text_norm, evidence)
            if result is None:
                log.info("job=%s claim=%s jury split -> S5 trial", job_id, claim_id)
                await events.emit(job_id, "escalation", {"claim_id": str(claim_id)})
                result = await s5_trial.run(con, job_id, claim_id, nc.text_norm, evidence)   # S5 trial

            log.info("job=%s claim=%s VERDICT %s conf=%s path=%s",
                     job_id, claim_id, result["verdict"], result["confidence"], result["path"])
            # S6 — synthesize the user-facing VerdictCard, persist it, seed the cache.
            await s6_synthesize.synthesize(
                con, job_id, claim_id, claim_en=nc.text_norm, claim_native=nc.text_norm_native,
                lang=norm.detected_lang, verdict=result["verdict"], confidence=result["confidence"],
                path=result["path"], evidence=evidence, original=text)
