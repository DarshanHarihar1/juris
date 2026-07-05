"""Phase 1 verification (design/phase-1-intake-normalize.md):
- Normalization quality on a golden mini-set (noisy → clean atomic claim, lang detect).
- Compound split → 2 claims; opinion → 0 claims (terminal).
- API contract: non-text → 501, missing content → 422 (no external deps).
- E2E: POST /api/verify → worker → claims persisted + events emitted.
Model-dependent tests skip without NIM_API_KEY; DB tests skip without DATABASE_URL."""
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from conftest import needs_db, needs_nim

pytestmark = pytest.mark.asyncio


# --- API contract (no NIM / DB needed) ------------------------------------------
async def test_api_contract():
    from app.main import VerifyBody, media, verify

    with pytest.raises(HTTPException) as e:                 # non-text → 501
        await verify(VerifyBody(type="image", content="x"))
    assert e.value.status_code == 501

    with pytest.raises(ValidationError):                   # missing content → 422 at parse
        VerifyBody(type="text")

    with pytest.raises(HTTPException) as e:                 # media upload → 501
        await media()
    assert e.value.status_code == 501


# --- normalization quality ------------------------------------------------------
@needs_nim
async def test_normalize_noisy_single_claim():
    from app.pipeline import s1_normalize

    out = await s1_normalize.normalize(
        "😱 BREAKING doctors say lemon water at 4am cures cancer forward to 10!!"
    )
    assert out.detected_lang.startswith("en")
    assert 1 <= len(out.claims) <= 3
    joined = " ".join(c.text_norm.lower() for c in out.claims)
    assert "lemon" in joined and "cancer" in joined
    assert "forward" not in joined and "😱" not in joined          # CTA / emoji stripped


@needs_nim
async def test_compound_split():
    from app.pipeline import s1_normalize

    out = await s1_normalize.normalize("The Earth is flat and vaccines cause autism.")
    assert len(out.claims) == 2


@needs_nim
async def test_opinion_filter():
    from app.pipeline import s1_normalize

    out = await s1_normalize.normalize("Modi is the best PM ever, don't you think?")
    assert out.claims == []


# --- end-to-end through the worker ----------------------------------------------
@needs_db
@needs_nim
async def test_verify_to_claims_e2e():
    from app import db, worker
    from app.services import jobs

    con = await (await db.pool()).acquire()
    try:
        submission_id = await con.fetchval(
            """insert into submissions (channel, user_hash, media_type, raw_text)
               values ('web', 'test', 'text', $1) returning id""",
            "The Great Wall of China is visible from space with the naked eye.",
        )
    finally:
        await (await db.pool()).release(con)

    job_id = await jobs.enqueue(submission_id=submission_id)
    job = await jobs.claim_next()
    while job and job["id"] != job_id:                      # skip unrelated queued jobs
        job = await jobs.claim_next()
    assert job is not None
    await worker.process(job)

    con = await (await db.pool()).acquire()
    try:
        claims = await con.fetch("select text_norm, claim_type from claims where submission_id = $1", submission_id)
        events = await con.fetch("select event from events_log where job_id = $1 order by id", job_id)
        status = await con.fetchval("select status from jobs where id = $1", job_id)
        assert len(claims) >= 1
        evset = {e["event"] for e in events}
        assert "stage" in evset and "claim" in evset
        assert status == "done"
        # cleanup
        await con.execute("delete from claims where submission_id = $1", submission_id)
        await con.execute("delete from events_log where job_id = $1", job_id)
        await con.execute("delete from jobs where id = $1", job_id)
        await con.execute("delete from submissions where id = $1", submission_id)
    finally:
        await (await db.pool()).release(con)
