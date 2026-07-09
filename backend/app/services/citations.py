"""Citation-lock for the v2 verifier and synthesizer.

Every factual assertion must ground itself in the evidence log via an `[e:id]`
tag. `validate()` strips uncited factual sentences; `enforce()` also normalizes
verdict citations and key evidence ids against the known evidence rows.
"""
import re

from ..models import Verdict

_CITE = re.compile(r"\[e:[^\]]+\]")
_CITE_ID = re.compile(r"\[e:([^\]]+)\]")
_SENT_SPLIT = re.compile(r"(?<=[.!?।])\s+")   # includes the Devanagari danda (।) for Hindi


def _is_factual_assertion(sentence: str) -> bool:
    """Heuristic: a non-trivial declarative sentence asserts fact and needs a citation.
    Questions and very short fragments (transitions, rhetoric) are exempt."""
    s = sentence.strip()
    if s.endswith("?"):
        return False
    return len(s.split()) > 4          # ponytail: word-count heuristic; tighten if it over/under-strips


def validate(text: str) -> tuple[str, list[str]]:
    """Return (clean_text, violations). A violation is an uncited factual sentence,
    which is removed from the returned text."""
    kept: list[str] = []
    violations: list[str] = []
    for sent in _SENT_SPLIT.split((text or "").strip()):
        if not sent:
            continue
        if _CITE.search(sent) or not _is_factual_assertion(sent):
            kept.append(sent)
        else:
            violations.append(sent)
    return " ".join(kept), violations


def _evidence_ids(evidence_log: list[dict]) -> set[str]:
    return {str(e.get("id")) for e in evidence_log if e.get("id")}


def _canonical_evidence_id(raw: str, valid: set[str]) -> str | None:
    candidate = (raw or "").strip()
    if candidate in valid:
        return candidate
    candidate = candidate.removeprefix("e:")
    if candidate in valid:
        return candidate
    if candidate.isdigit() and f"e{candidate}" in valid:
        return f"e{candidate}"
    if candidate.startswith("e") and candidate[1:].isdigit() and candidate in valid:
        return candidate
    return None


def _repair_text(text: str, valid: set[str]) -> str:
    def repl(match: re.Match) -> str:
        evidence_id = _canonical_evidence_id(match.group(1), valid)
        return f"[e:{evidence_id}]" if evidence_id else ""

    return _CITE_ID.sub(repl, text or "")


def enforce(verdict: Verdict, evidence_log: list[dict]) -> Verdict:
    """Strip unsupported verdict citations and remove uncited factual sentences.
    key_evidence is normalized to existing evidence row ids (e1, e2, ...)."""
    valid = _evidence_ids(evidence_log)
    repaired = _repair_text(verdict.explanation, valid)
    explanation, _violations = validate(repaired)

    key_evidence: list[str] = []
    for item in verdict.key_evidence:
        evidence_id = _canonical_evidence_id(str(item).strip().strip("[]"), valid)
        if evidence_id and evidence_id not in key_evidence:
            key_evidence.append(evidence_id)

    return verdict.model_copy(update={
        "explanation": explanation,
        "key_evidence": key_evidence,
    })
