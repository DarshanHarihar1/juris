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

    with pytest.raises(HTTPException) as e:                 # audio still unsupported → 501
        await verify(VerifyBody(type="audio", content="x"))
    assert e.value.status_code == 501

    with pytest.raises(ValidationError):                   # missing content → 422 at parse
        VerifyBody(type="text")

    with pytest.raises(ValidationError):                   # unknown type → 422 at parse
        VerifyBody(type="video", content="x")

    with pytest.raises(HTTPException) as e:                 # legacy media upload → 501
        await media()
    assert e.value.status_code == 501


# --- S0 intake: url / image resolution (no NIM / network needed) -----------------
async def test_s0_url_strips_html(monkeypatch):
    from app.pipeline import s0_intake

    html_doc = ("<html><head><style>.x{color:red}</style></head><body>"
                "<script>var y=1;</script><h1>NASA confirms water on Mars</h1>"
                "<p>Reported&nbsp;today.</p></body></html>")

    class _Resp:
        text = html_doc
        def raise_for_status(self): pass

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _Resp()

    monkeypatch.setattr(s0_intake.httpx, "AsyncClient", _Client)
    out = await s0_intake.intake("url", None, "https://example.com/mars")
    assert "NASA confirms water on Mars" in out
    assert "var y" not in out and "color:red" not in out      # script/style dropped
    assert "&nbsp;" not in out                                # entities unescaped


async def test_s0_image_ocr(monkeypatch):
    from app.pipeline import s0_intake

    async def _fake_chat(model, messages):
        assert messages[0]["content"][1]["image_url"]["url"].startswith("data:image/")
        return type("M", (), {"content": "Lemon water cures cancer"})()

    monkeypatch.setattr(s0_intake.nim, "chat", _fake_chat)
    out = await s0_intake.intake("image", None, "data:image/png;base64,AAAA")
    assert out == "Lemon water cures cancer"

    assert await s0_intake.intake("image", None, "not-an-image") == ""   # bad ref → empty


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


async def test_normalize_includes_temporal_grounding_metadata(monkeypatch):
    from app.models import NormalizedClaim, NormalizerOutput
    from app.pipeline import s1_normalize

    seen = {}

    async def fake_call(role_name, messages, response_schema=None, model_id=None, tools=None):
        seen["system"] = messages[0]["content"]
        seen["user"] = messages[1]["content"]
        return type("Resp", (), {
            "parsed": NormalizerOutput(
                detected_lang="en",
                claims=[
                    NormalizedClaim(
                        text_norm="As of July 2026, D.K. Shivakumar is the current Chief Minister of Karnataka.",
                        text_norm_native="As of July 2026, D.K. Shivakumar is the current Chief Minister of Karnataka.",
                        claim_type="factual",
                        is_time_sensitive=True,
                    )
                ],
            )
        })()

    monkeypatch.setattr(s1_normalize.nim, "call", fake_call)

    out = await s1_normalize.normalize("DK Shivakumar is the CM of Karnataka")
    assert "Today's date:" in seen["system"]
    assert out.claims[0].is_time_sensitive is True
    assert "As of July 2026" in out.claims[0].text_norm


async def test_normalize_filters_low_checkworthiness_claims(monkeypatch):
    from app.models import NormalizedClaim, NormalizerOutput
    from app.pipeline import s1_normalize

    async def fake_call(role_name, messages, response_schema=None, model_id=None, tools=None):
        return type("Resp", (), {
            "parsed": NormalizerOutput(
                detected_lang="en",
                claims=[
                    NormalizedClaim(
                        text_norm="The Prime Minister of India is Narendra Modi.",
                        text_norm_native="The Prime Minister of India is Narendra Modi.",
                        claim_type="factual",
                        checkworthiness_score=0.95,
                    ),
                    NormalizedClaim(
                        text_norm="This speech was shocking.",
                        text_norm_native="This speech was shocking.",
                        claim_type="factual",
                        checkworthiness_score=0.2,
                    ),
                ],
            )
        })()

    monkeypatch.setattr(s1_normalize.nim, "call", fake_call)

    out = await s1_normalize.normalize("Some message")
    assert [c.text_norm for c in out.claims] == ["The Prime Minister of India is Narendra Modi."]


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
