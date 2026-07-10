"""Guards added after the 'trump is president → FALSE' miss (job 30d49016)."""

from app.models import SubClaimVerdict
from app.pipeline import verify
from app.services import search


def _v(verdict, explanation):
    return SubClaimVerdict(verdict=verdict, explanation=explanation, evidence=[])


def test_false_by_absence_is_rejected():
    # The exact explanation from the bad run.
    bad = _v("false", "The search results do not provide any evidence of him being in office.")
    assert verify._false_guard_ok(bad) is False


def test_false_with_contradiction_passes():
    ok = _v("false", "Official records show Biden, not Trump, holds the office.")
    assert verify._false_guard_ok(ok) is True


def test_true_and_unverifiable_never_guarded():
    assert verify._false_guard_ok(_v("true", "no evidence")) is True
    assert verify._false_guard_ok(_v("unverifiable", "no evidence")) is True


def test_downgrade_keeps_evidence():
    v = SubClaimVerdict(verdict="false", explanation="no evidence found", evidence=["https://x"])
    out = verify._downgrade_to_unverifiable(v, [])
    assert out.verdict == "unverifiable"
    assert out.evidence == ["https://x"]


def test_office_holder_claim_detection():
    assert search._is_office_holder_claim("trump is the president of usa") is True
    assert search._is_office_holder_claim("who is the current CEO of Apple") is True
    assert search._is_office_holder_claim("the eiffel tower is in paris") is False


def test_trim_old_evidence_keeps_only_latest():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a", "content": "OLD evidence block 1"},
        {"role": "user", "content": "nudge"},
        {"role": "assistant", "tool_calls": [{"id": "b"}]},
        {"role": "tool", "tool_call_id": "b", "content": "NEW evidence block 2"},
    ]
    verify._trim_old_evidence(msgs)
    assert msgs[2]["content"] == verify._EVIDENCE_STUB       # old blanked
    assert msgs[5]["content"] == "NEW evidence block 2"      # latest kept
    # structure intact: every tool msg still pairs with an assistant tool_call
    assert [m["role"] for m in msgs].count("tool") == 2
