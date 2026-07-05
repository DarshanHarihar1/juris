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
