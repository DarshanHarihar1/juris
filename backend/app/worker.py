"""Background worker — polls the jobs queue and runs the pipeline on claimed jobs.
Phase 1: process() runs the orchestrator (S0→S1) then marks the job done.
Later stages (S2→S6) extend the orchestrator, not this loop."""
import asyncio
import logging

from . import db
from .pipeline import orchestrator
from .services import jobs

log = logging.getLogger("juris.worker")
POLL_INTERVAL = 2.0  # seconds between empty-queue polls


async def process(job: dict) -> None:
    await orchestrator.run(job)
    await jobs.mark_done(job["id"])


async def run(max_idle_polls: int | None = None) -> None:
    """Poll loop. max_idle_polls bounds idle polling for tests; None = run forever."""
    idle = 0
    while True:
        job = await jobs.claim_next()
        if job is None:
            idle += 1
            if max_idle_polls is not None and idle >= max_idle_polls:
                return
            await asyncio.sleep(POLL_INTERVAL)
            continue
        idle = 0
        try:
            await process(job)
        except Exception as e:  # never let one bad job kill the worker
            log.exception("job %s failed", job["id"])
            await jobs.mark_error(job["id"], str(e))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run())
    finally:
        asyncio.run(db.close())
