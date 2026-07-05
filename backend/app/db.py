"""Async Postgres pool (asyncpg). One shared pool per process."""
import asyncpg

from .config import database_url

_pool: asyncpg.Pool | None = None


async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        # ponytail: statement_cache_size=0 — required for Supabase's transaction pooler (pgbouncer).
        _pool = await asyncpg.create_pool(database_url(), statement_cache_size=0)
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
