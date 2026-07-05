import os
import sys
from pathlib import Path

import pytest

# Make `app` / `scripts` importable and load root .env for live-service tests.
sys.path.insert(0, str(Path(__file__).parent.parent))

for line in (Path(__file__).parent.parent.parent / ".env").read_text().splitlines() if (
    Path(__file__).parent.parent.parent / ".env"
).exists() else []:
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)

needs_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")
needs_nim = pytest.mark.skipif(not os.environ.get("NIM_API_KEY"), reason="NIM_API_KEY not set")


@pytest.fixture(autouse=True)
async def _close_pool_after_test():
    # pytest-asyncio gives each test a fresh event loop; an asyncpg pool is bound to
    # the loop it was created on. Close it in-loop after each test so the next test
    # builds a fresh one (avoids cross-loop reuse + pooler client-cap exhaustion).
    yield
    from app import db
    await db.close()
