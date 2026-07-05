"""Background worker — polls the jobs queue and processes claimed jobs.
Phase 0: the "process" step just emits events + marks done, proving the
enqueue → claim → events round-trip. Pipeline stages S0→S6 plug in here later."""
import asyncio
import logging

from . import db
from .services import events, jobs

log = logging.getLogger("juris.worker")
POLL_INTERVAL = 2.0  # seconds between empty-queue polls


async def process(job: dict) -> None:
    job_id = job["id"]
    await events.emit(job_id, "stage", {"stage": "S0_INTAKE", "status": "started"})
    # ponytail: no pipeline yet (Phase 1+). Just close the loop.
    await events.emit(job_id, "stage", {"stage": "S0_INTAKE", "status": "done"})
    await jobs.mark_done(job_id)


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
