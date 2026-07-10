"""Synthesis tests retained for v2 (Phase 1 stub).

Offline tests exercise VerdictCard assembly via build_card(): rebuttal <=400 + URL,
slug/models_used. Citation-lock and manipulation-tag whitelist were removed.
"""
import json

from conftest import needs_db

from app.models import SynthOutput
from app.pipeline import synthesize as synth

EV = [
    {
        "id": "e1",
        "url": "https://altnews.in/x",
        "domain": "altnews.in",
        "content": "The Great Wall is not visible unaided from space.",
        "snippet": "The Great Wall is not visible unaided from space.",
        "fetch_failed": False,
    },
    {
        "id": "e2",
        "url": "https://pib.gov.in/y",
        "domain": "pib.gov.in",
        "content": "Official source also rejects the claim.",
        "snippet": "Official source also rejects the claim.",
        "fetch_failed": False,
    },
]


async def _emit(*a, **k):
    return None


def test_build_card_keeps_explanation():
    out = SynthOutput(
        one_liner_native="Yeh dava galat hai.",
        explanation_native="The wall is not visible unaided.",
        rebuttal_card_native="Galat dava. https://altnews.in/x")
    card = synth.build_card("cid-1", "claim en", "claim native", "FALSE", 90, "verify", EV, out)
    assert "wall is not visible" in card.explanation_native
    assert card.manipulation_tags == []


def test_rebuttal_constraints():
    no_url = SynthOutput(one_liner_native="x", explanation_native="ok.",
                         rebuttal_card_native="This claim is false, please verify before sharing.")
    card = synth.build_card("cid-3", "c", "c", "FALSE", 80, "verify", EV, no_url)
    assert len(card.rebuttal_card_native) <= 400 and "http" in card.rebuttal_card_native

    long = SynthOutput(one_liner_native="x", explanation_native="ok.",
                       rebuttal_card_native="x" * 600 + " https://pib.gov.in/y")
    card2 = synth.build_card("cid-4", "c", "c", "FALSE", 80, "verify", EV, long)
    assert len(card2.rebuttal_card_native) <= 400


def test_slug_and_models_used():
    out = SynthOutput(one_liner_native="x", explanation_native="ok.",
                      rebuttal_card_native="https://pib.gov.in/y")
    card = synth.build_card("abcd1234-0000-0000-0000-000000000000", "The Great Wall claim", "c",
                         "FALSE", 90, "verify", EV, out)
    assert card.slug.startswith("the-great-wall-claim-") and card.slug.endswith("abcd1234")
    assert card.models_used["synthesizer"]
    assert not any(k.startswith("juror_") for k in card.models_used)
    assert {"prosecutor", "defense", "judge"}.isdisjoint(card.models_used)


@needs_db
async def test_persist_and_permalink(monkeypatch):
    from app import db

    monkeypatch.setattr(synth.events, "emit", _emit)

    con = await (await db.pool()).acquire()
    try:
        sub = await con.fetchval(
            "insert into submissions (channel, user_hash, media_type, raw_text) "
            "values ('web','test','text',$1) returning id", "phase5 permalink")
        claim_id = await con.fetchval(
            "insert into claims (submission_id, text_original, text_norm, text_norm_native, claim_type) "
            "values ($1,$2,$2,$2,'factual') returning id", sub, "Great Wall visible from space")

        card = await synth.synthesize(
            con, "00000000-0000-0000-0000-000000000000", claim_id,
            claim_en="Great Wall visible from space",
            claim_native="Great Wall visible from space",
            lang="en",
            verdict="FALSE", confidence=90, path="verify", evidence=EV,
            original="Great Wall visible from space",
            explanation_seed="The Great Wall is not visible unaided.")
        row = await con.fetchrow("select slug, verdict, card from verdicts where claim_id = $1", claim_id)
        assert row["slug"] == card.slug and row["verdict"] == "FALSE"
        dumped = json.loads(row["card"]) if isinstance(row["card"], str) else row["card"]
        assert "not visible" in dumped["explanation_native"]
        await con.execute("delete from verdicts where claim_id = $1", claim_id)
        await con.execute("delete from claims where id = $1", claim_id)
        await con.execute("delete from submissions where id = $1", sub)
    finally:
        await (await db.pool()).release(con)
