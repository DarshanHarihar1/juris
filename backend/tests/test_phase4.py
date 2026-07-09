"""Stream E v2 citation enforcement tests.

Jury, trial, anonymized transcript, and agreement-math tests were retired with
the v2 rearchitecture. Citation-lock remains a hard gate for Verify and
Synthesize output.
"""


def test_citation_lock():
    from app.services import citations

    clean, violations = citations.validate(
        "The earth is provably flat according to many sources. Vaccines are safe and effective [e:e2].")
    assert "Vaccines are safe and effective [e:e2]." in clean
    assert "flat" not in clean and any("flat" in v for v in violations)

    c2, v2 = citations.validate("Water boils at 100 degrees at sea level [e:e1].")
    assert v2 == [] and c2 == "Water boils at 100 degrees at sea level [e:e1]."   # fully cited untouched


def test_citation_enforce_strips_uncited_sentences_and_invalid_key_evidence():
    from app.models import Verdict
    from app.services import citations

    verdict = Verdict(
        verdict="FALSE",
        confidence=82,
        explanation=(
            "The official source names Siddaramaiah as Chief Minister [e:e1]. "
            "An unrelated uncited sentence should not survive."
        ),
        key_evidence=["e1", "missing"],
        evidence_conflict="none",
        used_parametric_knowledge=False,
    )
    enforced = citations.enforce(verdict, [{"id": "e1", "url": "https://pib.gov.in/cm"}])

    assert enforced.explanation == "The official source names Siddaramaiah as Chief Minister [e:e1]."
    assert enforced.key_evidence == ["e1"]
