"""Citation-lock (LLD §5-S5). Trial arguments must ground every factual assertion in
the evidence log via an `[e:id]` tag. validate() strips any factual sentence that
carries no citation and returns the violations for logging. Questions, short
connective phrases, and already-cited sentences pass untouched."""
import re

_CITE = re.compile(r"\[e:[^\]]+\]")
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
