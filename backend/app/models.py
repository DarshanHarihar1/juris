"""Pydantic schemas (LLD §4). Phase 1 covers Submission, Claim, and the
structured I/O for the S1 normalizer. Evidence/Trial/Verdict land in later phases."""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from typing import Literal

ClaimType = Literal["factual", "numeric", "media_context", "quote", "opinion_skip"]


# --- domain rows (LLD §4.1–4.2) -------------------------------------------------
class Submission(BaseModel):
    id: UUID
    channel: Literal["whatsapp", "web"]
    user_hash: str                      # salted hash; no raw phone numbers
    media_type: Literal["text", "image", "audio", "url"]
    raw_text: str | None = None
    media_uri: str | None = None
    detected_lang: str | None = None
    created_at: datetime


class Claim(BaseModel):
    id: UUID
    submission_id: UUID
    text_original: str
    text_norm: str                      # self-contained, English pivot
    text_norm_native: str               # same claim in source language
    claim_type: ClaimType
    # embedding filled in Phase 2; omitted here.


# --- S1 normalizer structured output (LLD §5-S1) --------------------------------
class NormalizedClaim(BaseModel):
    text_norm: str
    text_norm_native: str
    claim_type: ClaimType


class NormalizerOutput(BaseModel):
    detected_lang: str                  # "en", "hi", "hi-Latn", ...
    claims: list[NormalizedClaim]       # opinions/greetings already dropped; ≤3 atomic


# --- S4/S5 verdict engine (LLD §4.4, §5-S4/S5) ----------------------------------
VerdictClass = Literal["TRUE", "FALSE", "MISLEADING", "UNVERIFIABLE", "CONFLICTING"]


class JurorVote(BaseModel):
    """One fast-path juror reading claim + evidence log (no browsing)."""
    verdict: VerdictClass
    confidence: float                   # 0–1
    key_evidence_ids: list[str] = []    # evidence tags (e1, e2, …) the vote leans on
    reasoning_short: str = ""


class Argument(BaseModel):
    """One prosecutor/defense turn. Factual sentences must carry [e:id] citations."""
    text: str
    search_query: str | None = None     # optional single extra targeted search this side wants


class Ruling(BaseModel):
    """The Judge's verdict over an anonymized transcript + evidence log."""
    verdict: VerdictClass
    confidence: float                   # 0–1
    decisive_evidence_ids: list[str] = []
    reasoning: str = ""


# --- S6 synthesis / VerdictCard (LLD §4.5, §5-S6) -------------------------------
MANIPULATION_TAGS = frozenset({
    "fake-urgency", "authority-impersonation", "old-media-new-context",
    "numeric-truth-effect", "fabricated-quote", "emotional-priming",
    "fake-forward-chain", "miracle-cure", "scam-link",
})


class EvidenceRef(BaseModel):
    url: str
    domain: str
    stance: str | None = None
    date: str | None = None


class SynthOutput(BaseModel):
    """What the synthesizer LLM returns; deterministic card fields are filled around it."""
    one_liner_native: str
    explanation_native: str             # 3–5 sentences, each factual sentence cited [e:id]
    manipulation_tags: list[str] = []
    rebuttal_card_native: str


class VerdictCard(BaseModel):
    slug: str                           # public permalink
    claim_native: str
    claim_en: str
    verdict: VerdictClass
    confidence: int                     # 0–100
    one_liner_native: str
    explanation_native: str
    manipulation_tags: list[str]
    evidence: list[EvidenceRef]
    rebuttal_card_native: str
    path: Literal["cache", "precedent", "consensus", "trial"]
    models_used: dict[str, str]         # role → model id (transparency + NIM showcase)
