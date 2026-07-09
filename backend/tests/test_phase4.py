"""Phase 4 — AND-combine + Stage-3 format/summary (design/phase-4-verdict-e2e.md)."""
import pytest

from app.models import SubClaimVerdict
from app.pipeline import s6_synthesize as s6
from app.pipeline.s6_synthesize import VerifiedPart
from app.services.nim import NimResponse

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("inputs,expected", [
    (["true"], "true"),
    (["false"], "false"),
    (["unverifiable"], "unverifiable"),
    (["true", "true"], "true"),
    (["true", "false"], "false"),
    (["false", "unverifiable"], "false"),
    (["true", "unverifiable"], "unverifiable"),
    ([], "unverifiable"),
])
def test_and_combine_table(inputs, expected):
    assert s6.and_combine(inputs) == expected


def test_format_single_no_llm():
    scv = SubClaimVerdict(
        verdict="false",
        explanation="Siddaramaiah is the CM.",
        evidence=["https://pib.gov.in/cm"],
    )
    out = s6.format_single(scv)
    assert out.explanation_native == "Siddaramaiah is the CM."
    assert "FALSE" in out.one_liner_native
    assert "https://pib.gov.in/cm" in out.rebuttal_card_native


async def test_single_claim_verdict_stage_skips_nim(monkeypatch):
    calls = []

    async def boom(*a, **k):
        calls.append(1)
        raise AssertionError("Stage-3 must not call NIM for N=1")

    monkeypatch.setattr(s6.nim, "call", boom)

    emits = []

    async def fake_emit(*a, **k):
        emits.append((a, k))

    monkeypatch.setattr(s6.events, "emit", fake_emit)

    class FakeCon:
        async def execute(self, *a, **k):
            return None

    part = VerifiedPart(
        claim_id="cid-1",
        sub_claim="DKS is the CM of Karnataka.",
        scv=SubClaimVerdict(
            verdict="false",
            explanation="Siddaramaiah is CM.",
            evidence=["https://example.com/cm"],
        ),
    )
    card = await s6.verdict_stage(
        FakeCon(), "job-1", claim_id="cid-1", original="DKS is the CM of Karnataka.",
        lang="en", parts=[part],
    )
    assert calls == []
    assert card.verdict == "FALSE"
    assert card.explanation_native == "Siddaramaiah is CM."
    assert card.evidence and card.evidence[0].url.startswith("http")


async def test_multi_claim_calls_nim_once(monkeypatch):
    calls = []

    async def fake_call(role_name, messages, response_schema=None, **k):
        calls.append(role_name)
        assert role_name == "synthesizer"
        assert response_schema is s6.SummaryOutput
        return NimResponse(
            text='{"explanation":"Delhi is capital; Modi is PM."}',
            model="openai/gpt-oss-20b",
            parsed=s6.SummaryOutput(explanation="Delhi is capital; Modi is PM."),
        )

    async def _emit(*a, **k):
        return None

    monkeypatch.setattr(s6.nim, "call", fake_call)
    monkeypatch.setattr(s6.events, "emit", _emit)

    class FakeCon:
        async def execute(self, *a, **k):
            return None

    parts = [
        VerifiedPart("c1", "Delhi is the capital of India",
                     SubClaimVerdict(verdict="true", explanation="Delhi is capital.",
                                     evidence=["https://en.wikipedia.org/wiki/New_Delhi"])),
        VerifiedPart("c2", "Modi is the PM of India",
                     SubClaimVerdict(verdict="true", explanation="Modi is PM.",
                                     evidence=["https://www.pmindia.gov.in/"])),
    ]
    card = await s6.verdict_stage(
        FakeCon(), "job-2", claim_id="c1",
        original="delhi and modi is the capital and pm of india",
        lang="en", parts=parts,
    )
    assert calls == ["synthesizer"]
    assert card.verdict == "TRUE"
    assert "Delhi" in card.explanation_native and "Modi" in card.explanation_native
    assert len(card.evidence) >= 1


async def test_multi_false_dominates_and_summary_fallback(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("llm down")

    async def _emit(*a, **k):
        return None

    monkeypatch.setattr(s6.nim, "call", boom)
    monkeypatch.setattr(s6.events, "emit", _emit)

    class FakeCon:
        async def execute(self, *a, **k):
            return None

    parts = [
        VerifiedPart("c1", "A is true",
                     SubClaimVerdict(verdict="true", explanation="A holds.", evidence=[])),
        VerifiedPart("c2", "B is false",
                     SubClaimVerdict(verdict="false", explanation="B fails.",
                                     evidence=["https://example.com/b"])),
    ]
    card = await s6.verdict_stage(
        FakeCon(), "job-3", claim_id="c1", original="A and B", lang="en", parts=parts,
    )
    assert card.verdict == "FALSE"
    assert "[false]" in card.explanation_native or "B fails" in card.explanation_native
    assert any(e.url == "https://example.com/b" for e in card.evidence)
