"""FastAPI web service — Phase 0: /health + minimal enqueue endpoint.
Pipeline routes (S0–S6) land in later phases."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
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


class EnqueueBody(BaseModel):
    payload: dict = {}


@app.post("/api/jobs")
async def create_job(body: EnqueueBody):
    """Phase 0 stub: enqueue a bare job so the worker round-trip is demonstrable."""
    job_id = await jobs.enqueue(payload=body.payload)
    return {"job_id": str(job_id), "status": "queued"}
