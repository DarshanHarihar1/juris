"""Phase 5 verification (design/phase-5-synthesis-output.md). Offline tests exercise the
VerdictCard gates via the pure build_card(): citation-lock (uncited fact stripped), tag
whitelist, rebuttal ≤400 + URL, slug/models_used, rating→class. A @needs_db test proves
persistence + permalink round-trip (NIM faked, so no model needed)."""
import json
import types

from conftest import needs_db

from app.models import SynthOutput
from app.pipeline import s6_synthesize as s6

EV = [
    {"url": "https://altnews.in/x", "domain": "altnews.in", "stance": "refutes", "credibility": 0.85},
    {"url": "https://pib.gov.in/y", "domain": "pib.gov.in", "stance": "refutes", "credibility": 0.95},
]


async def _emit(*a, **k):
    return None


# --- citation-lock (hard gate) --------------------------------------------------
def test_citation_lock_gate():
    out = SynthOutput(
        one_liner_native="Yeh dava galat hai.",
        explanation_native="The wall is not visible unaided [e:e1]. It was secretly built by aliens who control the weather.",
        manipulation_tags=[], rebuttal_card_native="Galat dava. https://altnews.in/x")
    card = s6.build_card("cid-1", "claim en", "claim native", "FALSE", 90, "consensus", EV, out)
    assert "[e:e1]" in card.explanation_native
    assert "aliens" not in card.explanation_native.lower()      # uncited factual sentence stripped


# --- tag whitelist --------------------------------------------------------------
def test_tag_whitelist():
    out = SynthOutput(one_liner_native="x", explanation_native="ok [e:e1].",
                      manipulation_tags=["miracle-cure", "totally-made-up", "fake-urgency"],
                      rebuttal_card_native="see https://pib.gov.in/y")
    card = s6.build_card("cid-2", "c", "c", "FALSE", 80, "consensus", EV, out)
    assert set(card.manipulation_tags) == {"miracle-cure", "fake-urgency"}


# --- rebuttal constraints -------------------------------------------------------
def test_rebuttal_constraints():
    no_url = SynthOutput(one_liner_native="x", explanation_native="ok [e:e1].",
                         manipulation_tags=[], rebuttal_card_native="This claim is false, please verify before sharing.")
    card = s6.build_card("cid-3", "c", "c", "FALSE", 80, "consensus", EV, no_url)
    assert len(card.rebuttal_card_native) <= 400 and "http" in card.rebuttal_card_native   # URL appended

    long = SynthOutput(one_liner_native="x", explanation_native="ok [e:e1].",
                       manipulation_tags=[], rebuttal_card_native="x" * 600 + " https://pib.gov.in/y")
    card2 = s6.build_card("cid-4", "c", "c", "FALSE", 80, "consensus", EV, long)
    assert len(card2.rebuttal_card_native) <= 400                # truncated


# --- slug + models_used ---------------------------------------------------------
def test_slug_and_models_used():
    out = SynthOutput(one_liner_native="x", explanation_native="ok [e:e1].",
                      manipulation_tags=[], rebuttal_card_native="https://pib.gov.in/y")
    card = s6.build_card("abcd1234-0000-0000-0000-000000000000", "The Great Wall claim", "c",
                         "FALSE", 90, "consensus", EV, out)
    assert card.slug.startswith("the-great-wall-claim-") and card.slug.endswith("abcd1234")
    assert card.models_used["synthesizer"] and any(k.startswith("juror_") for k in card.models_used)

    tcard = s6.build_card("cid-5", "c", "c", "FALSE", 70, "trial", EV, out)
    assert {"prosecutor", "defense", "judge"} <= set(tcard.models_used)


def test_rating_to_class():
    assert s6.rating_to_class("False") == "FALSE"
    assert s6.rating_to_class("Misleading/Partly false") == "MISLEADING"
    assert s6.rating_to_class("Mostly true") == "TRUE"
    assert s6.rating_to_class(None) == "UNVERIFIABLE"


# --- persistence + permalink round-trip (DB only; NIM faked) --------------------
@needs_db
async def test_persist_and_permalink(monkeypatch):
    from app import db

    async def fake_call(role_name, messages, response_schema=None, **k):
        return types.SimpleNamespace(parsed=SynthOutput(
            one_liner_native="Yeh dava galat hai.",
            explanation_native="The Great Wall is not visible unaided [e:e1].",
            manipulation_tags=["miracle-cure"], rebuttal_card_native="Galat. https://altnews.in/x"))

    monkeypatch.setattr(s6.nim, "call", fake_call)
    monkeypatch.setattr(s6.events, "emit", _emit)

    con = await (await db.pool()).acquire()
    try:
        sub = await con.fetchval(
            "insert into submissions (channel, user_hash, media_type, raw_text) "
            "values ('web','test','text',$1) returning id", "phase5 permalink")
        claim_id = await con.fetchval(
            "insert into claims (submission_id, text_original, text_norm, text_norm_native, claim_type) "
            "values ($1,$2,$2,$2,'factual') returning id", sub, "Great Wall visible from space")

        card = await s6.synthesize(
            con, "00000000-0000-0000-0000-000000000000", claim_id,
            claim_en="Great Wall visible from space", claim_native="Great Wall visible from space",
            lang="en", verdict="FALSE", confidence=90, path="consensus", evidence=EV)

        row = await con.fetchrow("select verdict, confidence, card, path from verdicts where slug=$1", card.slug)
        assert row is not None and row["verdict"] == "FALSE" and row["path"] == "consensus"
        assert row["confidence"] >= 70                          # cacheable (Phase 2 loop)
        stored = json.loads(row["card"])
        assert stored["slug"] == card.slug and stored["one_liner_native"] == "Yeh dava galat hai."
        assert "aliens" not in stored["explanation_native"].lower()
    finally:
        await con.execute("delete from submissions where id = $1", sub)   # cascades claims + verdicts
        await (await db.pool()).release(con)
