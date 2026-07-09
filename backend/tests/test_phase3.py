"""Phase 3 verification tests (design/phase-3-verification.md)."""
from datetime import date
import json
import types

import pytest
from pydantic import ValidationError

pytestmark = pytest.mark.asyncio


class DotDict(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


def _tool_response(name, args):
    arguments = args if isinstance(args, str) else json.dumps(args)
    function = types.SimpleNamespace(name=name, arguments=arguments)
    call = types.SimpleNamespace(id="call_0", type="function", function=function)
    return types.SimpleNamespace(
        content="",
        tool_calls=[call],
        model_dump=lambda exclude_none=True: {
            "role": "assistant", "content": "", "tool_calls": [{
                "id": "call_0", "type": "function",
                "function": {"name": name, "arguments": arguments},
            }],
        },
    )


def _json_response(payload: dict):
    text = json.dumps(payload)
    return types.SimpleNamespace(
        content=text,
        tool_calls=None,
        model_dump=lambda exclude_none=True: {"role": "assistant", "content": text},
    )


async def _noop_emit(*a, **k):
    return None


def test_subclaim_verdict_schema():
    from app.models import SubClaimVerdict

    ok = SubClaimVerdict(verdict="false", explanation="Siddaramaiah is CM.", evidence=["https://pib.gov.in/x"])
    assert ok.verdict == "false"
    # uppercase coerced
    assert SubClaimVerdict(verdict="TRUE", explanation="ok", evidence=[]).verdict == "true"
    with pytest.raises(ValidationError):
        SubClaimVerdict(verdict="MOSTLY_TRUE", explanation="x", evidence=[])
    with pytest.raises(ValidationError):
        SubClaimVerdict(verdict="true", explanation="  ", evidence=[])
    # non-URL evidence dropped
    v = SubClaimVerdict(verdict="true", explanation="ok", evidence=["not-a-url", "https://ok.example/"])
    assert v.evidence == ["https://ok.example/"]


def test_verify_prompt_lists_search_tool():
    from app.pipeline import verify as v2

    assert v2.TOOL_NAMES == ["search"]
    assert "You have access to ONE tool" in v2.VERIFY_PROMPT
    assert "search" in v2.VERIFY_PROMPT
    assert "Today is {today}" in v2.VERIFY_PROMPT
    assert "final_verdict" not in v2.TOOL_NAMES
    assert "factcheck_search" not in v2.VERIFY_PROMPT
    assert "fetch_page" not in v2.VERIFY_PROMPT


def test_temporal_guard_rejects_parametric_time_sensitive():
    from app.models import SubClaimVerdict
    from app.pipeline import verify as v2

    claim = "DKS is the CM of Karnataka."
    bad = SubClaimVerdict(verdict="true", explanation="I recall DKS is CM.", evidence=[])
    assert v2._temporal_guard_ok(claim, bad) is False
    ok = SubClaimVerdict(verdict="false", explanation="Siddaramaiah is CM.",
                         evidence=["https://example.com/cm"])
    assert v2._temporal_guard_ok(claim, ok) is True
    unver = SubClaimVerdict(verdict="unverifiable", explanation="unclear", evidence=[])
    assert v2._temporal_guard_ok(claim, unver) is True
    static = SubClaimVerdict(verdict="true", explanation="Water boils at 100C.", evidence=[])
    assert v2._temporal_guard_ok("Water boils at 100C at sea level.", static) is True


def test_is_time_sensitive_heuristic():
    from app.services import search

    assert search._is_time_sensitive("DKS is the CM of Karnataka.") is True
    assert search._is_time_sensitive("The Earth orbits the Sun.") is False
    assert search._is_time_sensitive({"text_norm": "x", "is_time_sensitive": True}) is True


def test_temporal_filter_drops_pre_event_hits():
    from app.services import search

    hits = [
        DotDict(url="https://news.example/pre", published_at=date(2026, 7, 1)),
        DotDict(url="https://news.example/post", published_at=date(2026, 7, 8)),
        DotDict(url="https://news.example/undated", published_at=None),
    ]
    claim = {"is_time_sensitive": True, "as_of_date": date(2026, 7, 9)}
    filtered = search.temporal_filter(hits, claim)
    urls = {hit["url"] for hit in filtered}
    assert "https://news.example/pre" not in urls
    assert "https://news.example/post" in urls
    assert "https://news.example/undated" in urls


async def test_temporal_guard_forces_research(monkeypatch):
    from app.pipeline import verify as v2

    monkeypatch.setattr(v2.events, "emit", _noop_emit)
    calls = []

    async def fake_search(*a, **k):
        return [{
            "id": "e1", "url": "https://pib.gov.in/cm", "domain": "pib.gov.in",
            "title": "CM", "content": "Siddaramaiah is Chief Minister.", "score": 0.9,
        }]

    responses = iter([
        _json_response({"verdict": "true", "explanation": "parametric only", "evidence": []}),
        _tool_response("search", {"query": "karnataka chief minister 2026"}),
        _json_response({
            "verdict": "false",
            "explanation": "Siddaramaiah is the CM.",
            "evidence": ["https://pib.gov.in/cm"],
        }),
    ])

    async def fake_chat(*a, **k):
        calls.append(k)
        return next(responses)

    monkeypatch.setattr(v2, "_search_tool", fake_search)
    monkeypatch.setattr(v2.nim, "chat", fake_chat)

    out = await v2.verify_with_evidence("job-1", "DKS is the CM of Karnataka.", claim_id=None)
    assert out.verdict == "false"
    assert out.evidence and out.evidence[0].startswith("http")
    assert len(calls) >= 2


async def test_schema_retry_then_settle(monkeypatch):
    from app.pipeline import verify as v2

    monkeypatch.setattr(v2.events, "emit", _noop_emit)
    responses = iter([
        _json_response({"verdict": "TRUE", "explanation": "", "evidence": []}),  # fails nonempty explanation
        _json_response({
            "verdict": "unverifiable",
            "explanation": "Not enough evidence.",
            "evidence": [],
        }),
    ])

    async def fake_chat(*a, **k):
        return next(responses)

    monkeypatch.setattr(v2.nim, "chat", fake_chat)
    out = await v2.verify_with_evidence("job-1", "The Earth orbits the Sun.", claim_id=None)
    assert out.verdict == "unverifiable"
    assert out.explanation


async def test_force_verdict_on_budget(monkeypatch):
    from app.models import SubClaimVerdict
    from app.pipeline import verify as v2
    from app.services.nim import NimResponse

    monkeypatch.setattr(v2.events, "emit", _noop_emit)
    monkeypatch.setattr(v2, "thresholds", lambda: {"max_verify_steps": 2, "verify_budget_s": 75})

    async def fake_search(*a, **k):
        return []

    n = {"i": 0}

    async def fake_chat(*a, **k):
        n["i"] += 1
        return _tool_response("search", {"query": f"q{n['i']}"})

    async def fake_call(role_name, messages, response_schema=None, **k):
        assert response_schema is SubClaimVerdict
        return NimResponse(
            text='{"verdict":"unverifiable","explanation":"No evidence found.","evidence":[]}',
            model="llama-3.3-70b-versatile",
            parsed=SubClaimVerdict(
                verdict="unverifiable", explanation="No evidence found.", evidence=[],
            ),
        )

    monkeypatch.setattr(v2, "_search_tool", fake_search)
    monkeypatch.setattr(v2.nim, "chat", fake_chat)
    monkeypatch.setattr(v2.nim, "call", fake_call)

    out = await v2.verify_with_evidence("job-1", "Some obscure claim about X.", claim_id=None)
    assert out.verdict == "unverifiable"
    assert isinstance(out.explanation, str) and out.explanation


async def test_autofetch_failure_degrades(monkeypatch):
    from app.services import search

    async def fake_web_search(*args, **kwargs):
        return [DotDict(
            url="https://pib.gov.in/source", domain="pib.gov.in", title="Official",
            snippet="Siddaramaiah is Chief Minister of Karnataka.",
            published_at=date(2026, 7, 8), score=0.95,
        )]

    async def fake_jina(*a, **k):
        raise RuntimeError("timeout")

    monkeypatch.setattr(search, "web_search", fake_web_search)
    monkeypatch.setattr(search, "fetch_page", fake_jina)
    rows = await search.search("karnataka cm", claim="DKS is the CM of Karnataka.")
    assert rows[0]["snippet"]
    assert rows[0]["fetch_failed"] is True
