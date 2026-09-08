"""orchestrator.run() must not hold a pooled connection across the verify(N) fan-out
(juris-001: was starving the 5-connection pool for the full asyncio.gather span)."""
import pytest

from app.models import NormalizerOutput, SubClaimVerdict
from app.pipeline import orchestrator

pytestmark = pytest.mark.asyncio


class _FakeConnection:
    async def fetchrow(self, *a, **k):
        return {
            "media_type": "text", "raw_text": "claim text", "media_uri": None,
            "detected_lang": None, "channel": "web", "reply_to": None,
        }

    async def execute(self, *a, **k):
        return None

    async def fetchval(self, *a, **k):
        return 1


class _FakeAcquireCtx:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        self.pool.outstanding += 1
        return _FakeConnection()

    async def __aexit__(self, *a):
        self.pool.outstanding -= 1
        return False


class _FakePool:
    def __init__(self):
        self.outstanding = 0

    def acquire(self):
        return _FakeAcquireCtx(self)


async def test_no_connection_held_across_verify_fanout(monkeypatch):
    fake_pool = _FakePool()

    async def fake_pool_fn():
        return fake_pool

    monkeypatch.setattr(orchestrator, "pool", fake_pool_fn)
    monkeypatch.setattr(orchestrator.search_svc, "warm_searxng", lambda: _noop())
    monkeypatch.setattr(orchestrator.events, "emit", lambda *a, **k: _noop())
    monkeypatch.setattr(orchestrator.s0_intake, "intake", lambda *a, **k: _ret("some claim text"))
    monkeypatch.setattr(
        orchestrator.s1_normalize, "normalize",
        lambda *a, **k: _ret(NormalizerOutput(language="en", sub_claims=["claim a", "claim b"])),
    )

    observed = []

    async def fake_verify_with_evidence(*a, **k):
        observed.append(fake_pool.outstanding)
        return SubClaimVerdict(verdict="true", explanation="ok", evidence=[])

    monkeypatch.setattr(orchestrator.verify, "verify_with_evidence", fake_verify_with_evidence)

    async def fake_verdict_stage(con, job_id, **k):
        return None

    monkeypatch.setattr(orchestrator.synthesize, "verdict_stage", fake_verdict_stage)

    await orchestrator.run({"id": "job-1", "submission_id": 5})

    assert len(observed) == 2
    assert observed == [0, 0], "connection was still checked out during verify fan-out"


async def _noop():
    return None


async def _ret(value):
    return value
