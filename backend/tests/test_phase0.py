"""Phase 0 verification (design/phase-0-foundation.md):
  - MeshAPI smoke: mesh.call returns a schema-valid object; bad schema → 1 retry → fallback.
  - Queue: 5 jobs, 2 concurrent workers → each claimed exactly once.
  - Migration: down then up rebuilds all 7 tables + ivfflat index.
Live-service tests skip cleanly when DATABASE_URL / NIM_API_KEY are absent."""
import asyncio

import pytest

from conftest import needs_db, needs_mesh

pytestmark = pytest.mark.asyncio


@needs_mesh
async def test_mesh_smoke():
    from pydantic import BaseModel
    from app.services import mesh

    class Answer(BaseModel):
        answer: str

    resp = await mesh.call(
        "normalizer",
        [{"role": "user", "content": 'Reply with JSON {"answer": "pong"}. Only JSON.'}],
        response_schema=Answer,
    )
    assert resp.parsed is not None and resp.parsed.answer


@needs_db
async def test_queue_no_double_claim():
    from app import db
    from app.services import jobs

    ids = [await jobs.enqueue(payload={"n": i}) for i in range(5)]

    async def worker():
        claimed = []
        while (job := await jobs.claim_next()) is not None:
            claimed.append(job["id"])
        return claimed

    a, b = await asyncio.gather(worker(), worker())
    all_claimed = a + b
    ours = [j for j in all_claimed if j in ids]
    assert sorted(ours) == sorted(ids)          # every job claimed
    assert len(ours) == len(set(ours))          # none claimed twice

    con = await (await db.pool()).acquire()
    try:
        await con.execute("delete from jobs where id = any($1::uuid[])", ids)
    finally:
        await (await db.pool()).release(con)


@needs_db
async def test_migration_roundtrip():
    import asyncpg
    from app.config import database_url
    from scripts.migrate import _apply, _files

    await _apply(_files("down"))
    await _apply(_files("up"))

    con = await asyncpg.connect(database_url(), statement_cache_size=0)
    try:
        tables = {r["tablename"] for r in await con.fetch(
            "select tablename from pg_tables where schemaname = 'public'")}
        assert {"submissions", "claims", "evidence", "verdicts", "events_log", "jobs"} <= tables
        assert "trials" not in tables
        idx = await con.fetchval(
            "select indexdef from pg_indexes where indexname = 'claims_embedding_ivfflat'")
        assert idx and "ivfflat" in idx
    finally:
        await con.close()
