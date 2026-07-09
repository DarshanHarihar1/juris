"""Pydantic schemas for the v2 pipeline."""
from datetime import datetime
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, field_validator
from typing import Literal

# ponytail: claim_type still written to DB (NOT NULL); normalize no longer produces it.
ClaimType = Literal["factual", "numeric", "media_context", "quote", "opinion_skip"]


class Submission(BaseModel):
    id: UUID
    channel: Literal["whatsapp", "web"]
    user_hash: str
    media_type: Literal["text", "image", "audio", "url"]
    raw_text: str | None = None
    media_uri: str | None = None
    detected_lang: str | None = None
    created_at: datetime


class Claim(BaseModel):
    id: UUID
    submission_id: UUID
    text_original: str
    text_norm: str
    text_norm_native: str
    claim_type: ClaimType = "factual"


class NormalizerOutput(BaseModel):
    """Stage 1 output: in-process language + atomic sub-claims (AND-combined later)."""
    language: str
    sub_claims: list[str]


class ExtractOutput(BaseModel):
    """LLM-only extract/decompose payload (language is detected in-process)."""
    sub_claims: list[str]


class SubClaimVerdict(BaseModel):
    """Stage 2 agent output (design Phase 3)."""
    verdict: Literal["true", "false", "unverifiable"]
    explanation: str
    evidence: list[str] = []  # retrieved URLs

    @field_validator("verdict", mode="before")
    @classmethod
    def _normalize_verdict(cls, v):
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("explanation")
    @classmethod
    def _nonempty_explanation(cls, v: str) -> str:
        text = (v or "").strip()
        if not text:
            raise ValueError("explanation must be a non-empty string")
        return text

    @field_validator("evidence", mode="before")
    @classmethod
    def _coerce_evidence(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list):
            raise ValueError("evidence must be a list of URLs")
        out: list[str] = []
        for item in v:
            if not isinstance(item, str):
                continue
            u = item.strip()
            try:
                p = urlparse(u)
                if p.scheme in ("http", "https") and p.netloc:
                    out.append(u)
            except Exception:
                continue
        return out


# DB / WhatsApp still use uppercase labels (CHECK constraint). Map at persist boundary.
VerdictClass = Literal["TRUE", "FALSE", "UNVERIFIABLE"]


def to_db_verdict(v: str) -> VerdictClass:
    m = {"true": "TRUE", "false": "FALSE", "unverifiable": "UNVERIFIABLE",
         "TRUE": "TRUE", "FALSE": "FALSE", "UNVERIFIABLE": "UNVERIFIABLE"}
    return m.get(v, "UNVERIFIABLE")  # type: ignore[return-value]


class EvidenceRef(BaseModel):
    url: str
    domain: str
    stance: str | None = None
    date: str | None = None


class SynthOutput(BaseModel):
    """Synthesizer LLM return. Phase 4 replaces with rule format + optional summary."""
    one_liner_native: str
    explanation_native: str
    rebuttal_card_native: str = ""


class VerdictCard(BaseModel):
    slug: str
    claim_native: str
    claim_en: str
    verdict: VerdictClass
    confidence: int
    one_liner_native: str
    explanation_native: str
    evidence: list[EvidenceRef]
    rebuttal_card_native: str = ""
    path: Literal["verify"] = "verify"
    models_used: dict[str, str] = {}
    manipulation_tags: list[str] = []
