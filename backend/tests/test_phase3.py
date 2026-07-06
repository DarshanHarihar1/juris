"""Phase 3 verification (design/phase-3-investigation.md). Offline tests fake
nim.chat + tools so the ReAct loop is exercised without NIM/DB:
- tool-call cap is never exceeded (no runaway loops);
- stance is repaired to a valid enum, credibility ∈ [0,1];
- duplicate URLs across investigators collapse to one row;
- investigators run in parallel (wall-clock ≈ max, not sum);
- one investigator erroring out still persists the other's evidence.
The end-to-end test is gated on NIM + DB."""
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
    """Model that always wants another tool call must still stop at the cap of 3."""
    from app.pipeline import s3_investigate as s3
    from app.services import tools

    executed = {"n": 0}

    async def fake_call_tool(name, **kw):
        executed["n"] += 1
        return [{"url": "https://altnews.in/x", "title": "t", "snippet": "s"}]

    async def fake_chat(model, messages, tools=None):
        if tools:                                              # still allowed → keep requesting a tool
            return _msg(tool_calls=[("web_search", {"query": "q"})])
        return _msg(content=json.dumps({"evidence": [
            {"url": "https://altnews.in/x", "title": "t", "snippet": "s", "stance": "refutes"}]}))

    monkeypatch.setattr(tools, "call_tool", fake_call_tool)
    monkeypatch.setattr(s3.nim, "chat", fake_chat)

    items, tool_log = await s3._investigate_one("claim", "claim", "m", ["web_search", "numeric_check"])
    assert executed["n"] == 3 and len(tool_log) == 3          # capped, and numeric_check filtered out
    fin = s3._finalize(items, "m")
    assert fin and fin[0]["stance"] == "refutes" and 0.0 <= fin[0]["credibility"] <= 1.0


async def test_stance_repair_and_dedup():
    from app.pipeline import s3_investigate as s3

    repaired = s3._finalize([{"url": "https://x.com/1", "stance": "definitely-true"}], "A")
    assert repaired[0]["stance"] == "mentions"                # invalid stance repaired to neutral

    deduped = s3._dedup([
        {"url": "https://x.com/1", "credibility": 0.3, "stance": "mentions"},
        {"url": "https://x.com/1/", "credibility": 0.9, "stance": "refutes"},   # same URL (trailing /)
    ])
    assert len(deduped) == 1 and deduped[0]["credibility"] == 0.9   # one row, higher-credibility kept


async def test_parallel_execution(monkeypatch):
    from app.pipeline import s3_investigate as s3

    async def slow(claim_en, claim_native, model, tool_names):
        await asyncio.sleep(0.3)
        return ([{"url": f"https://altnews.in/{model}", "stance": "refutes"}], [])

    monkeypatch.setattr(s3, "_investigate_one", slow)
    monkeypatch.setattr(s3, "role", lambda n: [{"model": "A", "tools": []}, {"model": "B", "tools": []}])

    emitted = []
    async def fake_emit(job_id, event, data=None):
        emitted.append(event)
    monkeypatch.setattr(s3.events, "emit", fake_emit)

    class FakeCon:
        async def fetchval(self, *a):
            return "row"

    t = time.perf_counter()
    ev = await s3.investigate(FakeCon(), "job", "claim", "c", "c")
    dt = time.perf_counter() - t
    assert dt < 0.5                                           # ran concurrently (sum would be ~0.6s)
    assert len(ev) == 2 and emitted.count("evidence") == 2


async def test_graceful_degradation(monkeypatch):
    from app.pipeline import s3_investigate as s3

    async def one_fails(claim_en, claim_native, model, tool_names):
        if model == "B":
            raise RuntimeError("investigator B provider error")
        return ([{"url": "https://altnews.in/x", "stance": "refutes"}], [])

    monkeypatch.setattr(s3, "_investigate_one", one_fails)
    monkeypatch.setattr(s3, "role", lambda n: [{"model": "A", "tools": []}, {"model": "B", "tools": []}])
    monkeypatch.setattr(s3.events, "emit", lambda *a, **k: _noop())

    class FakeCon:
        async def fetchval(self, *a):
            return "row"

    ev = await s3.investigate(FakeCon(), "job", "claim", "c", "c")
    assert len(ev) == 1 and ev[0]["found_by"] == "A"         # B failed, A's evidence still persisted


async def _noop():
    return None


@needs_db
@needs_nim
async def test_investigation_e2e():
    """A real miss claim yields ≥1 deduped, stance-tagged, credibility-scored evidence row."""
    from app import db
    from app.pipeline import s3_investigate as s3

    con = await (await db.pool()).acquire()
    try:
        sub = await con.fetchval(
            "insert into submissions (channel, user_hash, media_type, raw_text) "
            "values ('web','test','text',$1) returning id", "e2e phase3")
        claim_id = await con.fetchval(
            "insert into claims (submission_id, text_original, text_norm, text_norm_native, claim_type) "
            "values ($1,$2,$2,$2,'factual') returning id",
            sub, "The Great Wall of China is visible from space with the naked eye.")

        ev = await s3.investigate(con, "00000000-0000-0000-0000-000000000000", claim_id,
                                  "The Great Wall of China is visible from space with the naked eye.",
                                  "The Great Wall of China is visible from space with the naked eye.")
        rows = await con.fetch("select stance, credibility, url from evidence where claim_id = $1", claim_id)
        assert len(rows) == len(ev)
        urls = [r["url"] for r in rows]
        assert len(urls) == len(set(u.rstrip("/") for u in urls))          # deduped
        for r in rows:
            assert r["stance"] in s3.STANCES and 0.0 <= (r["credibility"] or 0) <= 1.0
    finally:
        await con.execute("delete from submissions where id = $1", sub)     # cascades claims + evidence
        await (await db.pool()).release(con)
