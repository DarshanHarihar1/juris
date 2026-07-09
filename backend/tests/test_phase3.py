"""Stream E v2 verification tests.

These tests describe the single iterative Verify agent contract from
design/v2-rearchitecture.md. They intentionally retire the old S3 investigator
tests: v2 has one decision-making loop, a merged search+auto-fetch tool, and
hard temporal filtering.
"""
from datetime import date
import inspect
import json
import types

import pytest

pytestmark = pytest.mark.asyncio


class DotDict(dict):
    """Dict with attribute access, matching both row and object-style helpers."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


def _claim(**overrides):
    data = {
        "text_norm": "As of July 2026, D.K. Shivakumar is Chief Minister of Karnataka.",
        "text_norm_native": "As of July 2026, D.K. Shivakumar is Chief Minister of Karnataka.",
        "time_sensitive": True,
        "is_time_sensitive": True,
        "as_of_date": "2026-07-09",
        "volatility": "slow",
    }
    data.update(overrides)
    return DotDict(data)


def _tool_response(name, args):
    arguments = args if isinstance(args, str) else json.dumps(args)
    function = types.SimpleNamespace(name=name, arguments=arguments)
    call = types.SimpleNamespace(
        id="call_0",
        type="function",
        function=function,
        name=name,
        args=args,
    )
    return types.SimpleNamespace(
        content="",
        text="",
        tool_call=call,
        tool_calls=[call],
        model_dump=lambda exclude_none=True: {"role": "assistant", "content": ""},
    )


async def _run_verify(v2, claim):
    """Call whichever public loop entrypoint v2 exposes."""
    for name in ("verify", "verify_claim", "run"):
        fn = getattr(v2, name, None)
        if not fn:
            continue
        params = list(inspect.signature(fn).parameters.values())
        required_positional = [
            p for p in params
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            and p.default is p.empty
        ]
        if len(required_positional) <= 2:
            return await fn("job-1", claim)
        if len(required_positional) == 3:
            return await fn("job-1", "claim-1", claim)
        return await fn(_FakeCon(), "job-1", "claim-1", claim)
    raise AssertionError("app.pipeline.verify must expose verify(), verify_claim(), or run()")


class _FakeCon:
    async def execute(self, *args, **kwargs):
        return None

    async def fetchval(self, *args, **kwargs):
        return "row-1"


def _patch_tool_runner(v2, monkeypatch):
    async def fake_tool(*args, **kwargs):
        return [{
            "id": "e1",
            "url": "https://pib.gov.in/karnataka-cm",
            "domain": "pib.gov.in",
            "title": "Karnataka CM",
            "snippet": "Siddaramaiah is the Chief Minister of Karnataka.",
            "published_at": "2026-07-08",
            "credibility": 0.95,
            "fetch_failed": False,
        }]

    for name in ("run_tool", "_run_tool"):
        if hasattr(v2, name):
            monkeypatch.setattr(v2, name, fake_tool)


def _final(verdict="UNVERIFIABLE"):
    return {
        "verdict": verdict,
        "confidence": 55,
        "explanation": "Available evidence does not settle the claim [e:e1].",
        "key_evidence": ["e1"],
        "evidence_conflict": "none",
        "used_parametric_knowledge": False,
    }


def _is_forced_final(kwargs):
    tool_choice = str(kwargs.get("tool_choice", ""))
    tools_blob = json.dumps(kwargs.get("tools", ""), default=str)
    return "final_verdict" in tool_choice or (
        "final_verdict" in tools_blob and "search" not in tools_blob and "fetch_page" not in tools_blob
    )


async def test_verify_loop_budget_exhaustion_forces_verdict(monkeypatch):
    from app.pipeline import verify as v2

    _patch_tool_runner(v2, monkeypatch)
    calls = []

    async def fake_chat(*args, **kwargs):
        calls.append(kwargs)
        if _is_forced_final(kwargs):
            return _tool_response("final_verdict", _final())
        return _tool_response("search", {"query": f"karnataka cm {len(calls)}"})

    monkeypatch.setattr(v2.nim, "chat", fake_chat)

    result = await _run_verify(v2, _claim())

    assert result.verdict == "UNVERIFIABLE"
    assert any(_is_forced_final(call) for call in calls)


async def test_invalid_final_verdict_args_retry_once(monkeypatch):
    from app.pipeline import verify as v2

    calls = []
    responses = iter([
        _tool_response("final_verdict", {
            "verdict": "FALSE",
            "confidence": "high",
            "explanation": "Bad confidence type [e:e1].",
            "key_evidence": ["e1"],
        }),
        _tool_response("final_verdict", _final("FALSE")),
    ])

    async def fake_chat(*args, **kwargs):
        calls.append(kwargs)
        return next(responses)

    monkeypatch.setattr(v2.nim, "chat", fake_chat)

    result = await _run_verify(v2, _claim())

    assert result.verdict == "FALSE"
    assert len(calls) == 2


def test_temporal_filter_drops_pre_event_hits():
    from app.services import search

    hits = [
        DotDict(url="https://news.example/pre", published_at=date(2026, 7, 1)),
        DotDict(url="https://news.example/post", published_at=date(2026, 7, 8)),
        DotDict(url="https://news.example/undated", published_at=None),
    ]

    filtered = search.temporal_filter(hits, _claim(as_of_date=date(2026, 7, 9)))

    urls = {hit["url"] for hit in filtered}
    assert "https://news.example/pre" not in urls
    assert "https://news.example/post" in urls
    assert "https://news.example/undated" in urls


async def test_autofetch_failure_degrades_to_snippet_and_flag(monkeypatch):
    from app.services import search

    async def fake_web_search(*args, **kwargs):
        return [
            DotDict(
                url="https://pib.gov.in/source",
                domain="pib.gov.in",
                title="Official update",
                snippet="Siddaramaiah is Chief Minister of Karnataka.",
                published_at=date(2026, 7, 8),
                credibility=0.95,
            )
        ]

    async def fake_jina_fetch(*args, **kwargs):
        raise RuntimeError("reader timeout")

    monkeypatch.setattr(search, "web_search", fake_web_search)
    monkeypatch.setattr(search, "fetch_page", fake_jina_fetch)
    monkeypatch.setattr(search.credibility, "score", lambda domain: 0.95)

    rows = await search.search("karnataka chief minister", claim=_claim())

    assert rows[0]["snippet"] == "Siddaramaiah is Chief Minister of Karnataka."
    assert rows[0].get("content") in (None, "")
    assert rows[0]["fetch_failed"] is True
