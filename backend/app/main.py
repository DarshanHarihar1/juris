"""FastAPI web service (LLD §3.1). Public REST:
POST /api/verify (text / url / image), GET /api/jobs/{id}/events, GET /api/verdicts/{slug}.
url/image content is resolved to text in S0 by the worker; audio still returns 501."""
import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import db, worker
from .services import jobs

# Route juris.* logs to stdout so `render logs` captures the pipeline trace (S2 decisions,
# verdicts, errors). One handler, INFO level; the in-process worker shares this process.
_jlog = logging.getLogger("juris")
if not _jlog.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    _jlog.addHandler(_h)
    _jlog.setLevel(logging.INFO)
    _jlog.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ponytail: worker runs in-process (free plan has no worker dyno). Single
    # instance both serves and drains the queue — fine for demo scale. Split
    # into a `type: worker` on Starter+ if the two need to scale independently.
    worker_task = asyncio.create_task(worker.run())
    yield
    worker_task.cancel()
    await db.close()


app = FastAPI(title="Juris", lifespan=lifespan)
# ponytail: open CORS — public read API, no cookies/credentials. Restrict origins if abused.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health():
    return {"status": "ok"}


class VerifyBody(BaseModel):
    type: Literal["text", "image", "audio", "url"]
    content: str
    lang_hint: str | None = None


@app.post("/api/verify")
async def verify(body: VerifyBody):
    """Create a submission + enqueue a job. text/url/image supported (audio deferred).
    url/image are resolved to text in S0 by the worker, keeping this endpoint fast."""
    if body.type == "audio":
        raise HTTPException(501, "type 'audio' not supported yet")
    # text → raw_text; url/image → media_uri (the URL or a data: image). S0 resolves both.
    raw_text = body.content if body.type == "text" else None
    media_uri = body.content if body.type in ("url", "image") else None
    async with (await db.pool()).acquire() as con:
        submission_id = await con.fetchval(
            """insert into submissions (channel, user_hash, media_type, raw_text, media_uri)
               values ('web', 'web-anon', $1, $2, $3) returning id""",
            body.type, raw_text, media_uri,
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
