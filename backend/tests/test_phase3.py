"""Phase 3 verification — QA-decomposition mode (Phase 3b rearchitecture).
Offline tests mock nim.chat + tools so the ReAct loop is exercised without NIM/DB:
- tool-call cap respected inside _answer_question;
- _dedup keys by (question, url): same URL can appear for different questions;
- all (question × investigator) tasks parallelise;
- one investigator failing on a question doesn't block the others.
"""
import asyncio
import json
import time
import types

import pytest

from conftest import needs_db, needs_nim

pytestmark = pytest.mark.asyncio


def _msg(content=None, tool_calls=None):
    """Duck-typed OpenAI assistant message: .content, .tool_calls, .model_dump()."""
    tcs = []
    for i, (name, args) in enumerate(tool_calls or []):
        fn = types.SimpleNamespace(name=name, arguments=json.dumps(args))
        tcs.append(types.SimpleNamespace(id=f"call_{i}", type="function", function=fn))
    m = types.SimpleNamespace(content=content, tool_calls=(tcs or None))
    m.model_dump = lambda exclude_none=True: {"role": "assistant", "content": content}
    return m


async def test_tool_cap_enforced(monkeypatch):
    """_answer_question must stop at the tool cap of 3 and still output an answer."""
    from app.pipeline import s3_investigate as s3
    from app.services import tools

    executed = {"n": 0}

    async def fake_call_tool(name, **kw):
        executed["n"] += 1
        return [{"url": "https://altnews.in/x", "title": "t", "snippet": "s", "domain": "altnews.in"}]

    async def fake_chat(model, messages, tools=None):
        if tools:   # still within cap → keep requesting tool calls
            return _msg(tool_calls=[("web_search", {"query": "q"})])
        return _msg(content=json.dumps({
            "question": "test?", "answer": "yes", "answerable": True,
            "sources": [{"url": "https://altnews.in/x", "title": "t", "snippet": "s"}],
        }))

    monkeypatch.setattr(tools, "call_tool", fake_call_tool)
    monkeypatch.setattr(s3.nim, "chat", fake_chat)

    qa = await s3._answer_question("test?", "claim", "claim", "m",
                                   ["web_search", "fetch_page"], False)
    assert executed["n"] == 3, f"expected 3 tool calls, got {executed['n']}"
    assert qa is not None
    assert qa.answerable is True
    assert len(qa.sources) == 1
    assert qa.sources[0].published_at is None


async def test_dedup():
    """_dedup keys on (question, url): same URL for same question collapses; different questions stay separate."""
    from app.pipeline import s3_investigate as s3

    # same URL, same question → keep higher-credibility copy
    rows = s3._dedup([
        {"url": "https://x.com/1",  "question": "Q?", "credibility": 0.3, "answer": "a"},
        {"url": "https://x.com/1/", "question": "Q?", "credibility": 0.9, "answer": "a"},
    ])
    assert len(rows) == 1
    assert rows[0]["credibility"] == 0.9

    # same URL, different questions → two separate rows
    rows2 = s3._dedup([
        {"url": "https://x.com/1", "question": "Q1?", "credibility": 0.8, "answer": "a1"},
        {"url": "https://x.com/1", "question": "Q2?", "credibility": 0.8, "answer": "a2"},
    ])
    assert len(rows2) == 2


async def test_parallel_execution(monkeypatch):
    """All (question × investigator) tasks run concurrently — wall-clock ≈ max, not sum."""
    from app.pipeline import s3_investigate as s3
    from app.models import ClaimQuestions, QuestionAnswer, QASource

    async def fake_decompose(*a):
        return ClaimQuestions(questions=["Q1?", "Q2?"], time_sensitive=False)

    async def slow_answer(question, claim_en, claim_native, model, tool_names, time_sensitive,
                          claim_seed="", search_queries=None):
        await asyncio.sleep(0.3)
        return QuestionAnswer(
            question=question, answer="ans", answerable=True,
            sources=[QASource(url=f"https://altnews.in/{model}", title="t", snippet="s")],
        )

    monkeypatch.setattr(s3, "_decompose", fake_decompose)
    monkeypatch.setattr(s3, "_answer_question", slow_answer)
    monkeypatch.setattr(s3, "role", lambda n: [{"model": "A", "tools": []}, {"model": "B", "tools": []}])

    emitted = []
    async def fake_emit(job_id, event, data=None):
        emitted.append(event)

    monkeypatch.setattr(s3.events, "emit", fake_emit)

    class FakeCon:
        async def fetchval(self, *a):
            return "row-id"

    t = time.perf_counter()
    ev = await s3.investigate(FakeCon(), "job", "claim", "c", "c")
    dt = time.perf_counter() - t

    # 4 tasks × 0.3s serial = 1.2s; parallel → ~0.3s
    assert dt < 0.6, f"tasks did not run in parallel (took {dt:.2f}s)"
    assert len(ev) >= 2
    assert emitted.count("evidence") >= 2


async def test_graceful_degradation(monkeypatch):
    """One investigator failing on a question doesn't prevent the other from persisting."""
    from app.pipeline import s3_investigate as s3
    from app.models import ClaimQuestions, QuestionAnswer, QASource

    async def fake_decompose(*a):
        return ClaimQuestions(questions=["Q1?"], time_sensitive=False)

    async def one_fails(question, claim_en, claim_native, model, tool_names, time_sensitive,
                        claim_seed="", search_queries=None):
        if model == "B":
            raise RuntimeError("investigator B provider error")
        return QuestionAnswer(
            question=question, answer="yes", answerable=True,
            sources=[QASource(url="https://altnews.in/x", title="t", snippet="s")],
        )

    monkeypatch.setattr(s3, "_decompose", fake_decompose)
    monkeypatch.setattr(s3, "_answer_question", one_fails)
    monkeypatch.setattr(s3, "role", lambda n: [{"model": "A", "tools": []}, {"model": "B", "tools": []}])
    monkeypatch.setattr(s3.events, "emit", lambda *a, **k: _noop())

    class FakeCon:
        async def fetchval(self, *a):
            return "row"

    ev = await s3.investigate(FakeCon(), "job", "claim", "c", "c")
    assert len(ev) == 1
    assert ev[0]["found_by"] == "A"   # B failed, A's evidence still persisted


async def test_investigate_uses_time_sensitive_flag_from_s1(monkeypatch):
    """S3 should honor S1's time-sensitive metadata instead of re-deriving it."""
    from app.models import ClaimQuestions, QuestionAnswer
    from app.pipeline import s3_investigate as s3

    seen = []

    async def fake_decompose(*a, **k):
        return ClaimQuestions(questions=["Who is the current CM of Karnataka?"], time_sensitive=False)

    async def fake_answer(question, claim_en, claim_native, model, tool_names, time_sensitive,
                          claim_seed="", search_queries=None):
        seen.append(time_sensitive)
        return QuestionAnswer(question=question, answer="", answerable=False, sources=[])

    monkeypatch.setattr(s3, "_decompose", fake_decompose)
    monkeypatch.setattr(s3, "_answer_question", fake_answer)
    monkeypatch.setattr(s3, "role", lambda n: [{"model": "A", "tools": []}, {"model": "B", "tools": []}])
    monkeypatch.setattr(s3.events, "emit", lambda *a, **k: _noop())

    class FakeCon:
        async def fetchval(self, *a):
            return "row"

    await s3.investigate(FakeCon(), "job", "claim", "DK Shivakumar is the CM of Karnataka",
                         "DK Shivakumar is the CM of Karnataka", is_time_sensitive=True)
    assert seen == [True, True]


async def test_answer_question_preserves_source_published_at(monkeypatch):
    from app.pipeline import s3_investigate as s3
    from app.services import tools

    async def fake_call_tool(name, **kw):
        return [{
            "url": "https://altnews.in/x",
            "title": "t",
            "snippet": "s",
            "published_at": "2026-07-08",
            "domain": "altnews.in",
        }]

    async def fake_chat(model, messages, tools=None):
        if tools:
            return _msg(tool_calls=[("web_search", {"query": "q"})])
        return _msg(content=json.dumps({
            "question": "test?",
            "answer": "yes",
            "answerable": True,
            "sources": [{"url": "https://altnews.in/x", "title": "t", "snippet": "s"}],
        }))

    monkeypatch.setattr(tools, "call_tool", fake_call_tool)
    monkeypatch.setattr(s3.nim, "chat", fake_chat)

    qa = await s3._answer_question("test?", "claim", "claim", "m", ["web_search"], True)
    assert qa is not None
    assert qa.sources[0].published_at == "2026-07-08"


async def test_to_evidence_rows_carries_published_at():
    from app.models import QASource, QuestionAnswer
    from app.pipeline import s3_investigate as s3

    qa = QuestionAnswer(
        question="Who is CM?",
        answer="DK Shivakumar",
        answerable=True,
        sources=[QASource(
            url="https://altnews.in/x",
            title="t",
            snippet="s",
            published_at="2026-07-08",
        )],
    )

    rows = s3._to_evidence_rows(qa, "model-a")
    assert rows[0]["published_at"] == "2026-07-08"


async def test_to_evidence_rows_recomputes_domain_from_unwrapped_url():
    from app.models import QASource, QuestionAnswer
    from app.pipeline import s3_investigate as s3

    qa = QuestionAnswer(
        question="Who is CM?",
        answer="DK Shivakumar",
        answerable=True,
        sources=[QASource(
            url="https://www.google.com/url?url=https://www.hindustantimes.com/india-news/dk-shivakumar-is-the-new-chief-minister-of-karnataka-10162319418868.html",
            title="t",
            snippet="s",
            published_at="2026-07-09",
        )],
    )

    rows = s3._to_evidence_rows(qa, "model-a")
    assert rows[0]["url"] == "https://www.hindustantimes.com/india-news/dk-shivakumar-is-the-new-chief-minister-of-karnataka-10162319418868.html"
    assert rows[0]["domain"] == "hindustantimes.com"


async def test_generate_queries_caps_at_three(monkeypatch):
    from app.pipeline import s3_investigate as s3

    async def fake_call(role_name, messages, response_schema=None, model_id=None, tools=None):
        return type("Resp", (), {"parsed": type("Parsed", (), {
            "queries": ["q1 current", "q2 july 2026", "q3 Karnataka CM", "q4 extra"],
        })()})()

    monkeypatch.setattr(s3.nim, "call", fake_call)

    queries = await s3._generate_queries(
        "As of July 2026, D.K. Shivakumar is the current Chief Minister of Karnataka.",
        "Who is the current CM of Karnataka?",
        True,
        "2026-07-09",
    )
    assert queries == ["q1 current", "q2 july 2026", "q3 Karnataka CM"]


async def test_investigate_passes_generated_queries_to_investigators(monkeypatch):
    from app.models import ClaimQuestions, QuestionAnswer
    from app.pipeline import s3_investigate as s3

    seen = []

    async def fake_decompose(*a, **k):
        return ClaimQuestions(questions=["Who is the current CM of Karnataka?"], time_sensitive=True)

    async def fake_generate_queries(claim_en, question, time_sensitive, as_of_date):
        return ["karnataka cm july 2026", "dk shivakumar current cm"]

    async def fake_answer(question, claim_en, claim_native, model, tool_names, time_sensitive,
                          claim_seed="", search_queries=None):
        seen.append(search_queries)
        return QuestionAnswer(question=question, answer="", answerable=False, sources=[])

    monkeypatch.setattr(s3, "_decompose", fake_decompose)
    monkeypatch.setattr(s3, "_generate_queries", fake_generate_queries)
    monkeypatch.setattr(s3, "_answer_question", fake_answer)
    monkeypatch.setattr(s3, "role", lambda n: [{"model": "A", "tools": []}, {"model": "B", "tools": []}])
    monkeypatch.setattr(s3.events, "emit", lambda *a, **k: _noop())

    class FakeCon:
        async def fetchval(self, *a):
            return "row"

    await s3.investigate(
        FakeCon(), "job", "claim",
        "As of July 2026, D.K. Shivakumar is the current Chief Minister of Karnataka.",
        "As of July 2026, D.K. Shivakumar is the current Chief Minister of Karnataka.",
        is_time_sensitive=True,
    )
    assert seen == [
        ["karnataka cm july 2026", "dk shivakumar current cm"],
        ["karnataka cm july 2026", "dk shivakumar current cm"],
    ]


async def test_filter_relevant_evidence_drops_irrelevant_rows(monkeypatch):
    from app.pipeline import s3_investigate as s3

    rows = [
        {
            "url": "https://boomlive.in/cm",
            "question": "Who is the current CM of Karnataka?",
            "answer": "D.K. Shivakumar",
            "answerable": True,
            "title": "CM story",
            "snippet": "D.K. Shivakumar is the current CM of Karnataka.",
            "domain": "boomlive.in",
            "credibility": 0.85,
            "stance": None,
        },
        {
            "url": "https://boomlive.in/drunk-video",
            "question": "Who is the current CM of Karnataka?",
            "answer": "No",
            "answerable": True,
            "title": "Old video",
            "snippet": "Old video of DK Shivakumar walking unsteadily.",
            "domain": "boomlive.in",
            "credibility": 0.85,
            "stance": None,
        },
    ]

    labels = iter(["supports", "irrelevant"])

    async def fake_call(role_name, messages, response_schema=None, model_id=None, tools=None):
        label = next(labels)
        return type("Resp", (), {
            "parsed": type("Parsed", (), {"label": label})()
        })()

    monkeypatch.setattr(s3.nim, "call", fake_call)

    filtered = await s3._filter_relevant_evidence(
        "As of July 2026, D.K. Shivakumar is the current Chief Minister of Karnataka.",
        rows,
    )
    assert len(filtered) == 1
    assert filtered[0]["url"] == "https://boomlive.in/cm"
    assert filtered[0]["stance"] == "supports"


async def _noop():
    return None


@needs_db
@needs_nim
async def test_investigation_e2e():
    """A real claim yields ≥1 deduped QA evidence row with question + answer fields."""
    from app import db
    from app.pipeline import s3_investigate as s3

    con = await (await db.pool()).acquire()
    try:
        sub = await con.fetchval(
            "insert into submissions (channel, user_hash, media_type, raw_text) "
            "values ('web','test','text',$1) returning id", "e2e phase3 qa")
        claim_id = await con.fetchval(
            "insert into claims (submission_id, text_original, text_norm, text_norm_native, claim_type) "
            "values ($1,$2,$2,$2,'factual') returning id",
            sub, "The Great Wall of China is visible from space with the naked eye.")

        ev = await s3.investigate(con, "00000000-0000-0000-0000-000000000000", claim_id,
                                  "The Great Wall of China is visible from space with the naked eye.",
                                  "The Great Wall of China is visible from space with the naked eye.")
        rows = await con.fetch(
            "select url, question, answer, answerable, credibility from evidence where claim_id = $1",
            claim_id)
        assert len(rows) >= 1
        # dedup is per (question, url) — same URL may appear for different questions
        pairs = [(r["question"], r["url"].rstrip("/")) for r in rows if r["url"]]
        assert len(pairs) == len(set(pairs)), "duplicate (question, url) pairs found"
        for r in rows:
            assert r["question"]                                      # every row has a sub-question
            assert r["credibility"] is None or 0.0 <= r["credibility"] <= 1.0
    finally:
        await con.execute("delete from submissions where id = $1", sub)
        await (await db.pool()).release(con)
