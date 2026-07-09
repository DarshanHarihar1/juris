"""Background worker — polls the jobs queue and runs the pipeline on claimed jobs."""
import asyncio
import logging

from langsmith import Client as LangSmithClient

from . import db
from .pipeline import orchestrator
from .services import jobs

log = logging.getLogger("juris.worker")
POLL_INTERVAL = 2.0  # seconds between empty-queue polls


def _flush_traces() -> None:
    """Best-effort flush so short-lived workers don't drop spans. No-op if tracing off."""
    try:
        LangSmithClient().flush()
    except Exception:
        pass


async def process(job: dict) -> None:
    try:
        await orchestrator.run(
            job,
            langsmith_extra={"metadata": {
                "job_id": str(job["id"]),
                "submission_id": str(job.get("submission_id")),
            }},
        )
        await jobs.mark_done(job["id"])
    finally:
        _flush_traces()


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
            _flush_traces()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run())
    finally:
        _flush_traces()
        asyncio.run(db.close())
