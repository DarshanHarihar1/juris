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
