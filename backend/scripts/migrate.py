"""Tiny SQL migration runner. `python -m scripts.migrate up|down`.
ponytail: plain .sql files, not alembic — no branching migrations needed yet.
Applies every backend/migrations/*.sql (up) or *.down.sql (down)."""
import asyncio
import sys
from pathlib import Path

import asyncpg

from app.config import database_url

MIGRATIONS = Path(__file__).parent.parent / "migrations"


async def _apply(files: list[Path]) -> None:
    con = await asyncpg.connect(database_url(), statement_cache_size=0)
    try:
        for f in files:
            print(f"applying {f.name}")
            await con.execute(f.read_text())
    finally:
        await con.close()


def _files(direction: str) -> list[Path]:
    if direction == "up":
        return sorted(p for p in MIGRATIONS.glob("*.sql") if not p.name.endswith(".down.sql"))
    if direction == "down":
        return sorted(MIGRATIONS.glob("*.down.sql"), reverse=True)
    raise SystemExit("usage: python -m scripts.migrate up|down")


if __name__ == "__main__":
    direction = sys.argv[1] if len(sys.argv) > 1 else "up"
    asyncio.run(_apply(_files(direction)))
    print("done")
