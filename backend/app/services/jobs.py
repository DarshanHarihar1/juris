"""Job queue on Postgres — enqueue + claim_next via FOR UPDATE SKIP LOCKED.
No Redis (LLD §1). claim_next is safe under concurrent workers."""
from uuid import UUID

from ..db import pool


async def enqueue(submission_id: UUID | None = None, payload: dict | None = None) -> UUID:
    async with (await pool()).acquire() as con:
        return await con.fetchval(
            "insert into jobs (submission_id, payload) values ($1, $2::jsonb) returning id",
            submission_id, _json(payload or {}),
        )


async def claim_next() -> dict | None:
    """Atomically claim one queued job. Returns the row dict or None if queue empty.
    SKIP LOCKED means two workers never claim the same job."""
    async with (await pool()).acquire() as con:
        row = await con.fetchrow(
            """
            update jobs set status = 'running', attempts = attempts + 1, claimed_at = now(), updated_at = now()
            where id = (
                select id from jobs where status = 'queued'
                order by created_at
                for update skip locked
                limit 1
            )
            returning *
            """
        )
        return dict(row) if row else None


async def mark_done(job_id: UUID) -> None:
    await _set_status(job_id, "done")


async def mark_error(job_id: UUID, error: str) -> None:
    async with (await pool()).acquire() as con:
        await con.execute(
            "update jobs set status = 'error', last_error = $2, updated_at = now() where id = $1",
            job_id, error,
        )


async def _set_status(job_id: UUID, status: str) -> None:
    async with (await pool()).acquire() as con:
        await con.execute(
            "update jobs set status = $2, updated_at = now() where id = $1", job_id, status
        )


def _json(d: dict) -> str:
    import json
    return json.dumps(d)
