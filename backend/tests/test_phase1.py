"""Phase 2 normalization tests (design/phase-2-normalization.md).

Covers in-process lang detect, one-LLM extract/decompose, one- vs multi-sub-claim,
and empty/noisy inputs. Model-dependent tests skip without NIM_API_KEY.
"""
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from conftest import needs_db, needs_nim

pytestmark = pytest.mark.asyncio


# --- API contract (no NIM / DB needed) ------------------------------------------
async def test_api_contract():
    from app.main import VerifyBody, media, verify

    with pytest.raises(HTTPException) as e:
        await verify(VerifyBody(type="audio", content="x"))
    assert e.value.status_code == 501

    with pytest.raises(ValidationError):
        VerifyBody(type="text")

    with pytest.raises(ValidationError):
        VerifyBody(type="video", content="x")

    with pytest.raises(HTTPException) as e:
        await media()
    assert e.value.status_code == 501


# --- S0 intake -----------------------------------------------------------------
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
    assert "var y" not in out and "color:red" not in out
    assert "&nbsp;" not in out


async def test_s0_image_ocr(monkeypatch):
    from app.pipeline import s0_intake

    async def _fake_chat(model, messages):
        assert messages[0]["content"][1]["image_url"]["url"].startswith("data:image/")
        return type("M", (), {"content": "Lemon water cures cancer"})()

    monkeypatch.setattr(s0_intake.nim, "chat", _fake_chat)
    out = await s0_intake.intake("image", None, "data:image/png;base64,AAAA")
    assert out == "Lemon water cures cancer"
    assert await s0_intake.intake("image", None, "not-an-image") == ""


# --- language detect (no LLM) --------------------------------------------------
def test_detect_language_no_nim():
    from app.pipeline import s1_normalize

    assert s1_normalize.detect_language("DKS is the CM of Karnataka.") == "en"
    assert s1_normalize.detect_language("hello", hint="hi-Latn") == "hi"
    assert s1_normalize.detect_language("") == "en"


async def test_normalize_one_llm_call_and_schema(monkeypatch):
    from app.models import ExtractOutput, NormalizerOutput
    from app.pipeline import s1_normalize

    calls = []

    async def fake_call(role_name, messages, response_schema=None, **k):
        calls.append({"role": role_name, "schema": response_schema, "system": messages[0]["content"]})
        return type("Resp", (), {
            "parsed": ExtractOutput(sub_claims=["D.K. Shivakumar is the Chief Minister of Karnataka."])
        })()

    monkeypatch.setattr(s1_normalize.nim, "call", fake_call)
    out = await s1_normalize.normalize("DKS is the CM of Karnataka.")

    assert len(calls) == 1
    assert calls[0]["schema"] is ExtractOutput
    assert isinstance(out, NormalizerOutput)
    assert out.language == "en"
    assert out.sub_claims == ["D.K. Shivakumar is the Chief Minister of Karnataka."]
    assert "sub_claims" in calls[0]["system"] or "atomic" in calls[0]["system"].lower()


async def test_normalize_one_sub_claim(monkeypatch):
    from app.models import ExtractOutput
    from app.pipeline import s1_normalize

    async def fake_call(*a, **k):
        return type("Resp", (), {
            "parsed": ExtractOutput(sub_claims=["DKS is the CM of Karnataka."])
        })()

    monkeypatch.setattr(s1_normalize.nim, "call", fake_call)
    out = await s1_normalize.normalize("DKS is the CM of Karnataka.")
    assert len(out.sub_claims) == 1
    assert "DKS" in out.sub_claims[0] or "Karnataka" in out.sub_claims[0]


async def test_normalize_multi_sub_claims(monkeypatch):
    from app.models import ExtractOutput
    from app.pipeline import s1_normalize

    async def fake_call(*a, **k):
        return type("Resp", (), {
            "parsed": ExtractOutput(sub_claims=[
                "The Earth is flat.",
                "Vaccines cause autism.",
            ])
        })()

    monkeypatch.setattr(s1_normalize.nim, "call", fake_call)
    out = await s1_normalize.normalize("The Earth is flat and vaccines cause autism.")
    assert len(out.sub_claims) == 2
    assert all(isinstance(c, str) and c.strip() for c in out.sub_claims)


async def test_normalize_empty_and_cap(monkeypatch):
    from app.models import ExtractOutput
    from app.pipeline import s1_normalize

    async def empty(*a, **k):
        return type("Resp", (), {"parsed": ExtractOutput(sub_claims=[])})()

    monkeypatch.setattr(s1_normalize.nim, "call", empty)
    assert (await s1_normalize.normalize("forward to 10 people!!")).sub_claims == []

    async def many(*a, **k):
        return type("Resp", (), {
            "parsed": ExtractOutput(sub_claims=[f"Claim {i}." for i in range(10)])
        })()

    monkeypatch.setattr(s1_normalize.nim, "call", many)
    out = await s1_normalize.normalize("many facts")
    assert len(out.sub_claims) == s1_normalize.MAX_SUB_CLAIMS


async def test_normalize_strips_blank_entries(monkeypatch):
    from app.models import ExtractOutput
    from app.pipeline import s1_normalize

    async def fake_call(*a, **k):
        return type("Resp", (), {
            "parsed": ExtractOutput(sub_claims=["  Real claim.  ", "", "  "])
        })()

    monkeypatch.setattr(s1_normalize.nim, "call", fake_call)
    out = await s1_normalize.normalize("noise")
    assert out.sub_claims == ["Real claim."]


# --- golden mini-set (live NIM) ------------------------------------------------
@needs_nim
async def test_normalize_noisy_single_claim():
    from app.pipeline import s1_normalize

    out = await s1_normalize.normalize(
        "😱 BREAKING doctors say lemon water at 4am cures cancer forward to 10!!"
    )
    assert out.language.startswith("en")
    assert 1 <= len(out.sub_claims) <= 3
    joined = " ".join(c.lower() for c in out.sub_claims)
    assert "lemon" in joined and "cancer" in joined
    assert "forward" not in joined and "😱" not in joined


@needs_nim
async def test_compound_split():
    from app.pipeline import s1_normalize

    out = await s1_normalize.normalize("The Earth is flat and vaccines cause autism.")
    assert len(out.sub_claims) >= 2


@needs_nim
async def test_opinion_filter():
    from app.pipeline import s1_normalize

    out = await s1_normalize.normalize("Modi is the best PM ever, don't you think?")
    assert out.sub_claims == []


@needs_nim
async def test_normalize_canonical_one_claim():
    from app.pipeline import s1_normalize

    out = await s1_normalize.normalize("DKS is the CM of Karnataka.")
    assert out.language.startswith("en")
    assert len(out.sub_claims) == 1


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
    while job and job["id"] != job_id:
        job = await jobs.claim_next()
    assert job is not None
    await worker.process(job)

    con = await (await db.pool()).acquire()
    try:
        claims = await con.fetch("select text_norm from claims where submission_id = $1", submission_id)
        events = await con.fetch("select event from events_log where job_id = $1 order by id", job_id)
        status = await con.fetchval("select status from jobs where id = $1", job_id)
        assert len(claims) >= 1
        evset = {e["event"] for e in events}
        assert "stage" in evset and "claim" in evset
        assert status == "done"
        await con.execute("delete from claims where submission_id = $1", submission_id)
        await con.execute("delete from events_log where job_id = $1", job_id)
        await con.execute("delete from jobs where id = $1", job_id)
        await con.execute("delete from submissions where id = $1", submission_id)
    finally:
        await (await db.pool()).release(con)
