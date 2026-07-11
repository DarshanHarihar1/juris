"""S0 Intake (LLD §5-S0). Resolve any media type down to plain text, then trim +
collapse whitespace. text: as-is. url: fetch + strip HTML. image: OCR via a NIM
vision model. The rest of the pipeline (S1→S6) only ever sees text. audio deferred."""
import base64
import html
import logging
import os
import re

import httpx

from ..config import role
from ..services import mesh

log = logging.getLogger("juris.intake")

_WS = re.compile(r"\s+")
_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")
_MAX_CHARS = 5000          # cap page/OCR text; S1 only needs the claim, not the whole article
_FETCH_TIMEOUT = 12.0

_OCR_PROMPT = ("Transcribe ALL visible text in this image exactly, in reading order. "
              "Output only the transcribed text — no commentary, no description.")


async def intake(media_type: str, raw_text: str | None, media_uri: str | None) -> str:
    """media_type ∈ {text, url, image}. url/image resolve to text; on failure → ''
    (the orchestrator turns empty intake into a 'nothing to verify' terminal)."""
    if media_type == "url":
        text = await _fetch_url_text(media_uri or raw_text or "")
    elif media_type == "image":
        text = await _ocr(media_uri or raw_text or "")
    else:
        text = raw_text or ""
    return _WS.sub(" ", text).strip()[:_MAX_CHARS]


async def _fetch_url_text(url: str) -> str:
    """GET the URL and strip HTML to text. Any failure → '' (never crash the job).
    ponytail: regex de-tag, not a DOM parser — good enough to feed S1, which extracts
    the check-worthy claim from the noise. Add selectolax/readability if pages get messy."""
    if not url.startswith(("http://", "https://")):
        return ""
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0 (JurisBot)"})
            r.raise_for_status()
            body = r.text
    except Exception:
        return ""
    body = _SCRIPT_STYLE.sub(" ", body)
    return html.unescape(_TAGS.sub(" ", body))


async def _ocr(image_ref: str) -> str:
    """OCR via a NIM vision model. image_ref is a data URL, a public https URL, or a
    Twilio MediaUrl (fetched with account credentials). Any failure → ''."""
    resolved = await _resolve_image_ref(image_ref)
    if not resolved:
        return ""
    model = role("ocr")["model"]
    try:
        msg = await mesh.chat(model, [{"role": "user", "content": [
            {"type": "text", "text": _OCR_PROMPT},
            {"type": "image_url", "image_url": {"url": resolved}},
        ]}])
        return msg.content or ""
    except Exception:
        return ""


async def _resolve_image_ref(image_ref: str) -> str:
    """Return a URL the vision model can read (data: or public https)."""
    if not image_ref:
        return ""
    if image_ref.startswith("data:image/"):
        return image_ref
    if not image_ref.startswith(("http://", "https://")):
        return ""
    if "api.twilio.com" in image_ref:
        return await _twilio_media_data_url(image_ref)
    return image_ref


async def _twilio_media_data_url(url: str) -> str:
    """Download a Twilio-hosted WhatsApp attachment (auth required) → data URL."""
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        log.warning("Twilio media fetch skipped: TWILIO_ACCOUNT_SID/AUTH_TOKEN unset")
        return ""
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=True) as c:
            r = await c.get(url, auth=(sid, token))
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
            if not ctype.startswith("image/"):
                return ""
            b64 = base64.b64encode(r.content).decode("ascii")
            return f"data:{ctype};base64,{b64}"
    except Exception:
        log.exception("Twilio media fetch failed url=%r", url)
        return ""
