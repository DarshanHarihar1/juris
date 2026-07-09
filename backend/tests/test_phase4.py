"""Phase 4 verification (design/phase-4-verdict-engine.md). All offline (faked NIM):
- agreement math (the money path): weighted-plurality consensus vs 3-way escalation;
- citation-lock: uncited factual sentence stripped + logged, cited sentence untouched;
- anonymization: the Judge payload carries no model identifiers;
- branch wiring: high agreement → consensus dict, low → escalate (None);
- trial: prosecutor/defense/judge produce a 5-class Ruling; over-budget → expedited."""
import types                           # async tests auto-detected via asyncio_mode=auto (pytest.ini)


def _resp(parsed):
    return types.SimpleNamespace(text="", model="m", parsed=parsed)


async def _emit(*a, **k):
    return None


# --- agreement math (the money path) --------------------------------------------
def test_agreement_money_path():
    from app.models import JurorVote
    from app.pipeline.s4_fastpath import agreement

    winner, share, conf = agreement([
        JurorVote(verdict="FALSE", confidence=0.9),
        JurorVote(verdict="FALSE", confidence=0.8),
        JurorVote(verdict="TRUE", confidence=0.4),
    ])
    assert winner == "FALSE" and abs(share - (1.7 / 2.1)) < 1e-6 and share >= 0.75
    assert conf == 85                                              # mean winning confidence → 0-100

    _, split_share, _ = agreement([
        JurorVote(verdict="TRUE", confidence=0.5),
        JurorVote(verdict="FALSE", confidence=0.5),
        JurorVote(verdict="MISLEADING", confidence=0.5),
    ])
    assert split_share < 0.75                                     # 3-way split → escalate


def test_render_evidence_marks_stale_sources():
    from app.pipeline.s4_fastpath import render_evidence

    text, _idmap = render_evidence([
        {
            "id": "1",
            "question": "Who is the CM of Karnataka?",
            "answer": "Siddaramaiah",
            "answerable": True,
            "domain": "boomlive.in",
            "credibility": 0.85,
            "published_at": "2024-05-01",
        }
    ], as_of_date="2026-07-09")
    assert "2024-05-01" in text
    assert "stale" in text.lower()


# --- citation-lock --------------------------------------------------------------
def test_citation_lock():
    from app.services import citations

    clean, violations = citations.validate(
        "The earth is provably flat according to many sources. Vaccines are safe and effective [e:e2].")
    assert "Vaccines are safe and effective [e:e2]." in clean
    assert "flat" not in clean and any("flat" in v for v in violations)

    c2, v2 = citations.validate("Water boils at 100 degrees at sea level [e:e1].")
    assert v2 == [] and c2 == "Water boils at 100 degrees at sea level [e:e1]."   # fully cited untouched


# --- anonymization --------------------------------------------------------------
def test_judge_payload_anonymized():
    from app.pipeline import s5_trial as s5

    payload = s5.judge_payload("some claim", "[e:e1] altnews.in: title — snippet", [
        {"side": "Side 1", "round": 1, "text": "It is false [e:e1]."},
        {"side": "Side 2", "round": 1, "text": "It is true [e:e1]."},
    ]).lower()
    for fam in ["llama", "deepseek", "qwen", "gpt-oss", "kimi", "nvidia", "meta", "moonshot", "openai", "prosecutor", "defense"]:
        assert fam not in payload
    assert "side 1" in payload and "side 2" in payload


# --- S4 branch wiring -----------------------------------------------------------
async def test_deliberate_consensus_and_escalate(monkeypatch):
    from app.models import JurorVote
    from app.pipeline import s4_fastpath as s4

    monkeypatch.setattr(s4, "role", lambda n: ["m1", "m2", "m3"])
    monkeypatch.setattr(s4.events, "emit", _emit)
    ev = [{"id": "1", "domain": "altnews.in", "stance": "refutes", "credibility": 0.85}]

    consensus = {"m1": JurorVote(verdict="FALSE", confidence=0.9),
                 "m2": JurorVote(verdict="FALSE", confidence=0.8),
                 "m3": JurorVote(verdict="TRUE", confidence=0.4)}
    monkeypatch.setattr(s4.nim, "call", lambda rn, msgs, **k: _wrap(consensus[k["model_id"]]))
    res = await s4.deliberate("job", "claim", "c", ev)
    assert res and res["path"] == "consensus" and res["verdict"] == "FALSE"

    split = {"m1": JurorVote(verdict="TRUE", confidence=0.6),
             "m2": JurorVote(verdict="FALSE", confidence=0.6),
             "m3": JurorVote(verdict="MISLEADING", confidence=0.6)}
    monkeypatch.setattr(s4.nim, "call", lambda rn, msgs, **k: _wrap(split[k["model_id"]]))
    assert await s4.deliberate("job", "claim", "c", ev) is None    # escalate


async def test_deliberate_caps_confidence_for_stale_time_sensitive_evidence(monkeypatch):
    from app.models import JurorVote
    from app.pipeline import s4_fastpath as s4

    monkeypatch.setattr(s4, "role", lambda n: ["m1", "m2", "m3"])
    monkeypatch.setattr(s4.events, "emit", _emit)

    votes = {
        "m1": JurorVote(verdict="FALSE", confidence=0.9, evidence_addresses_claim=True),
        "m2": JurorVote(verdict="FALSE", confidence=0.8, evidence_addresses_claim=True),
        "m3": JurorVote(verdict="FALSE", confidence=0.7, evidence_addresses_claim=True),
    }
    monkeypatch.setattr(s4.nim, "call", lambda rn, msgs, **k: _wrap(votes[k["model_id"]]))

    ev = [{
        "id": "1",
        "question": "Who is the CM of Karnataka?",
        "answer": "Siddaramaiah",
        "answerable": True,
        "domain": "boomlive.in",
        "credibility": 0.85,
        "published_at": "2024-05-01",
    }]
    res = await s4.deliberate("job", "claim", "c", ev, is_time_sensitive=True, as_of_date="2026-07-09")
    assert res is not None
    assert res["confidence"] < 80


def _wrap(parsed):
    async def _coro():
        return _resp(parsed)
    return _coro()


# --- S5 trial -------------------------------------------------------------------
async def test_trial_ruling_and_expedited(monkeypatch):
    from app.models import Argument, Ruling
    from app.pipeline import s5_trial as s5

    async def fake_call(role_name, messages, response_schema=None, model_id=None, tools=None):
        if role_name == "judge":
            return _resp(Ruling(verdict="UNVERIFIABLE", confidence=0.5, decisive_evidence_ids=["e1"]))
        return _resp(Argument(text="The claim is unsupported [e:e1].", search_query=None))

    monkeypatch.setattr(s5.nim, "call", fake_call)
    monkeypatch.setattr(s5.events, "emit", _emit)

    class FakeCon:
        async def execute(self, *a):
            return None

    ev = [{"id": "1", "domain": "altnews.in", "stance": "mentions", "credibility": 0.85}]
    res = await s5.run(FakeCon(), "job", "claim", "c", ev)
    assert res["path"] == "trial" and res["verdict"] == "UNVERIFIABLE" and res["expedited"] is False

    monkeypatch.setattr(s5, "TRIAL_BUDGET", -1.0)                  # force over-budget
    res2 = await s5.run(FakeCon(), "job", "claim", "c", ev)
    assert res2["expedited"] is True                              # ruled on transcript-so-far
