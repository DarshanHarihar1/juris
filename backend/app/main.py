"""FastAPI web service (LLD §3.1). Phase 1 public REST:
POST /api/verify (text only), GET /api/jobs/{id}/events, GET /api/verdicts/{slug}.
POST /api/media and non-text types return 501 (v1 text-only)."""
import json
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import db
from .services import jobs


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await db.close()


app = FastAPI(title="Juris", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


class VerifyBody(BaseModel):
    type: Literal["text", "image", "audio", "url"]
    content: str
    lang_hint: str | None = None


@app.post("/api/verify")
async def verify(body: VerifyBody):
    """Create a submission + enqueue a job. v1 handles text only."""
    if body.type != "text":
        raise HTTPException(501, f"type '{body.type}' not supported yet (v1 text-only)")
    async with (await db.pool()).acquire() as con:
        submission_id = await con.fetchval(
            """insert into submissions (channel, user_hash, media_type, raw_text)
               values ('web', 'web-anon', 'text', $1) returning id""",
            body.content,
        )
    job_id = await jobs.enqueue(submission_id=submission_id)
    return {"job_id": str(job_id), "trial_url": f"/trial/{job_id}", "status": "queued"}


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    """Ordered event log for a job. ponytail: plain JSON poll; the live courtroom
    UI subscribes to events_log directly via Supabase Realtime (LLD §3.2)."""
    async with (await db.pool()).acquire() as con:
        rows = await con.fetch(
            "select id, event, data, created_at from events_log where job_id = $1::uuid order by id",
            job_id,
        )
    return {"events": [
        {"id": r["id"], "event": r["event"], "data": json.loads(r["data"]), "created_at": r["created_at"]}
        for r in rows
    ]}


@app.get("/api/verdicts/{slug}")
async def get_verdict(slug: str):
    async with (await db.pool()).acquire() as con:
        row = await con.fetchrow("select card from verdicts where slug = $1", slug)
    if row is None:
        raise HTTPException(404, "verdict not found")
    return json.loads(row["card"])


@app.post("/api/media")
async def media():
    raise HTTPException(501, "media upload not supported yet (v1 text-only)")
