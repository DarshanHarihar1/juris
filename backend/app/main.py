"""FastAPI web service (LLD §3.1). Public REST:
POST /api/verify (text / url / image), GET /api/jobs/{id}/events, GET /api/verdicts/{slug}.
url/image content is resolved to text in S0 by the worker; audio still returns 501."""
import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from typing import Literal

from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import config, db, worker
from .services import jobs, whatsapp

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
    return {"job_id": str(job_id), "investigation_url": f"/investigation/{job_id}", "status": "queued"}


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    """Ordered event log for a job. ponytail: plain JSON poll; the live investigation
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


@app.get("/api/jobs/{job_id}/stream")
async def job_stream(job_id: str, request: Request):
    """SSE stream of events_log rows for the live investigation UI. Sets the SSE `id:`
    field so EventSource reconnects resume gaplessly via Last-Event-ID. Closes after
    a verdict/terminal event. ponytail: 500ms DB poll loop; LISTEN/NOTIFY if load matters."""
    last_event_id = request.headers.get("last-event-id", "")
    start_id = int(last_event_id) if last_event_id.isdigit() else 0

    async def gen():
        last_id, done = start_id, False
        while not done:
            if await request.is_disconnected():
                return
            async with (await db.pool()).acquire() as con:
                rows = await con.fetch(
                    "select id, event, data, created_at from events_log"
                    " where job_id = $1::uuid and id > $2 order by id",
                    job_id, last_id,
                )
            for r in rows:
                last_id = r["id"]
                done = done or r["event"] in ("verdict", "terminal")
                payload = json.dumps({
                    "id": r["id"], "event": r["event"],
                    "data": json.loads(r["data"]), "created_at": str(r["created_at"]),
                })
                yield f"id: {r['id']}\ndata: {payload}\n\n"
            if not done:
                await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/verdicts/{slug}")
async def get_verdict(slug: str):
    async with (await db.pool()).acquire() as con:
        row = await con.fetchrow("select card from verdicts where slug = $1", slug)
    if row is None:
        raise HTTPException(404, "verdict not found")
    return json.loads(row["card"])


@app.post("/webhooks/whatsapp")
async def whatsapp_webhook(request: Request):
    """Twilio WhatsApp sandbox inbound (LLD §8). Enqueues a job and acks synchronously via
    TwiML — no outbound API call for the ack. The verdict is pushed later by the worker.
    ponytail: stdlib parse_qs handles the form-encoded body (no python-multipart dep). Raw
    wa_id/From are used here but never logged or persisted un-hashed (§35)."""
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    msg = whatsapp.adapter.parse_inbound(form)

    # "R" → forwardable rebuttal for this user's most recent verdict, replied inline via TwiML.
    if msg.text.upper() == "R":
        async with (await db.pool()).acquire() as con:
            row = await con.fetchrow(
                """select v.card from verdicts v
                     join claims c on c.id = v.claim_id
                     join submissions s on s.id = c.submission_id
                    where s.user_hash = $1 order by v.created_at desc limit 1""",
                whatsapp.hash_waid(msg.wa_id))
        rebuttal = (json.loads(row["card"]).get("rebuttal_card_native") if row
                    else None) or "No recent verdict yet — send a claim to fact-check first."
        return Response(whatsapp.ack_twiml(rebuttal), media_type="application/xml")

    async with (await db.pool()).acquire() as con:
        async with con.transaction():
            # Idempotency: a retried webhook returns the same job, no re-enqueue (§8).
            job_id = await con.fetchval("select job_id from wa_inbound where message_sid = $1", msg.msg_sid)
            if job_id is None:
                submission_id = await con.fetchval(
                    """insert into submissions (channel, user_hash, media_type, raw_text, media_uri, reply_to)
                       values ('whatsapp', $1, $2, $3, $4, $5) returning id""",
                    whatsapp.hash_waid(msg.wa_id), msg.media_type,
                    msg.text or None, msg.media_uri, msg.reply_to)
                job_id = await con.fetchval(
                    "insert into jobs (submission_id) values ($1) returning id", submission_id)
                await con.execute(
                    "insert into wa_inbound (message_sid, job_id) values ($1, $2)", msg.msg_sid, job_id)

    investigation_url = f"{config.public_base_url()}/investigation/{job_id}"
    return Response(
        whatsapp.ack_twiml(f"🔍 Juris is investigating — follow the live investigation: {investigation_url}"),
        media_type="application/xml")


@app.post("/api/media")
async def media():
    raise HTTPException(501, "direct media upload not supported yet")
