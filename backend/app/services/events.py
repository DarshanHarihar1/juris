"""Writes rows to events_log for the live investigation feed."""
import json
from uuid import UUID

from ..db import pool


async def emit(job_id: UUID, event: str, data: dict | None = None) -> None:
    async with (await pool()).acquire() as con:
        await con.execute(
            "insert into events_log (job_id, event, data) values ($1, $2, $3::jsonb)",
            job_id, event, json.dumps(data or {}),
        )
