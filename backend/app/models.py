"""Pydantic schemas for the v2 pipeline."""
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
    # embeddings were removed from the v2 runtime path.


# --- S1 normalizer structured output (LLD §5-S1) --------------------------------
class NormalizedClaim(BaseModel):
    text_norm: str
    text_norm_native: str
    claim_type: ClaimType
    is_time_sensitive: bool = False
    as_of_date: str | None = None
    volatility: Literal["static", "slow", "breaking"] = "slow"
    checkworthiness_score: float = 1.0


class NormalizerOutput(BaseModel):
    detected_lang: str                  # "en", "hi", "hi-Latn", ...
    claims: list[NormalizedClaim]       # opinions/greetings already dropped; ≤3 atomic


VerdictClass = Literal["TRUE", "FALSE", "MOSTLY_TRUE", "MISLEADING", "UNVERIFIABLE", "CONFLICTING"]


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


class Verdict(BaseModel):
    verdict: VerdictClass
    confidence: int
    explanation: str
    key_evidence: list[str] = []
    evidence_conflict: Literal["none", "resolved_by_recency", "unresolved"] = "none"
    used_parametric_knowledge: bool = False


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
    path: Literal["verify"]
    models_used: dict[str, str]         # role → model id (transparency + NIM showcase)
