"""WhatsApp channel (LLD §8, Phase 7). Inbound webhook parsing + outbound delivery,
behind an adapter so Twilio (sandbox demo) can be swapped for the Meta Cloud API later.

Privacy (non-negotiable, §35): only a salted hash of wa_id is ever stored/logged. The
raw reply address lives on `submissions.reply_to` for the in-flight job and is nulled
after the verdict is sent — it never reaches events_log (which the public UI reads)."""
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass

import httpx

from ..config import public_base_url

_TWILIO_MSGS = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
_META_GRAPH = "https://graph.facebook.com/{ver}/{phone_id}/messages"
_E_TAG = re.compile(r"\s*\[e:[^\]]+\]")   # citation tags — stripped for the forwardable text
_EMOJI = {"TRUE": "✅", "FALSE": "❌", "MISLEADING": "⚠️", "CONFLICTING": "⚠️", "UNVERIFIABLE": "❓"}


@dataclass
class InboundMsg:
    """Provider-agnostic inbound message. Both adapters parse down to this shape."""
    wa_id: str        # digits only, no prefix — the stable per-user id (hash before storing)
    reply_to: str     # channel address to reply to, e.g. "whatsapp:+91..."
    text: str
    msg_sid: str      # provider message id — idempotency key (providers retry on timeout)


def hash_waid(wa_id: str) -> str:
    """Salted SHA-256 of the WhatsApp id. Salt is required — we never store an unsalted id."""
    salt = os.environ.get("WA_HASH_SALT")
    if not salt:
        raise RuntimeError("WA_HASH_SALT not set — refusing to store an unsalted wa_id")
    return hashlib.sha256((salt + wa_id).encode()).hexdigest()


def ack_twiml(message: str) -> str:
    """Synchronous reply body for an inbound webhook — Twilio speaks it, no API call/auth."""
    return f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response><Message>{_xml(message)}</Message></Response>"


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_verdict(cards: list[dict]) -> str:
    """One forwardable message for all of a submission's verdict cards."""
    base = public_base_url()
    blocks = []
    for c in cards:
        emoji = _EMOJI.get(c.get("verdict", ""), "❓")
        expl = _E_TAG.sub("", c.get("explanation_native") or "").strip()
        blocks.append(
            f"{emoji} {c.get('verdict')}: {c.get('one_liner_native')}\n\n"
            f"{expl}\n\nRead the full verdict: {base}/v/{c.get('slug')}"
        )
    return ("\n\n———\n\n".join(blocks) + "\n\nReply 'R' for a short message you can forward.").strip()


class TwilioWhatsApp:
    """Twilio WhatsApp sandbox adapter (form-encoded webhook + synchronous TwiML ack)."""

    synchronous_ack = True   # Twilio takes the reply inline in the webhook response (TwiML)

    def parse_body(self, raw: bytes) -> dict:
        from urllib.parse import parse_qs
        return {k: v[0] for k, v in parse_qs(raw.decode()).items()}

    def verify_challenge(self, params: dict) -> str | None:
        return None          # Twilio has no GET subscription handshake

    def signature_ok(self, raw: bytes, headers) -> bool:
        return True          # Twilio uses X-Twilio-Signature (not validated in v1)

    def parse_inbound(self, form: dict) -> InboundMsg | None:
        frm = form.get("From", "")
        return InboundMsg(
            wa_id=form.get("WaId", ""), reply_to=frm,
            text=(form.get("Body") or "").strip(), msg_sid=form.get("MessageSid", ""),
        )

    async def send(self, reply_to: str, body: str) -> None:
        sid = os.environ["TWILIO_ACCOUNT_SID"]
        token = os.environ["TWILIO_AUTH_TOKEN"]
        frm = os.environ["TWILIO_WHATSAPP_FROM"]   # e.g. "whatsapp:+14155238886"
        async with httpx.AsyncClient(timeout=15) as cx:
            r = await cx.post(_TWILIO_MSGS.format(sid=sid), auth=(sid, token),
                              data={"From": frm, "To": reply_to, "Body": body})
            r.raise_for_status()


class MetaWhatsApp:
    """Meta WhatsApp Cloud API adapter (official Graph API).

    Two mechanics differ from Twilio (LLD §8):
      • Webhook is verified once by a GET handshake (hub.challenge) and every POST is
        signed (X-Hub-Signature-256, HMAC-SHA256 over the raw body with the app secret).
      • There is no synchronous reply — the webhook must 200 fast; every message (including
        the ack) is pushed via POST /messages. Free-form text is allowed inside the 24h
        customer-service window, which our reply-to-an-inbound-claim flow always sits in."""

    synchronous_ack = False   # Meta replies out-of-band via the send API, not in the webhook body

    def parse_body(self, raw: bytes) -> dict:
        return json.loads(raw or b"{}")

    def verify_challenge(self, params: dict) -> str | None:
        """GET subscription handshake: echo hub.challenge iff the verify token matches."""
        if params.get("hub.mode") == "subscribe" and \
                params.get("hub.verify_token") == os.environ.get("WHATSAPP_VERIFY_TOKEN"):
            return params.get("hub.challenge")
        return None

    def signature_ok(self, raw: bytes, headers) -> bool:
        """Validate X-Hub-Signature-256. Skipped only if no app secret is configured
        (local/dev); in prod WHATSAPP_APP_SECRET must be set so spoofed POSTs are rejected."""
        secret = os.environ.get("WHATSAPP_APP_SECRET")
        if not secret:
            return True
        got = headers.get("X-Hub-Signature-256", "")
        want = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(want, got)

    def parse_inbound(self, payload: dict) -> InboundMsg | None:
        """Extract the first inbound text message. Returns None for the non-message
        callbacks Meta also delivers here (delivery/read statuses, non-text media)."""
        try:
            msg = payload["entry"][0]["changes"][0]["value"]["messages"][0]
        except (KeyError, IndexError, TypeError):
            return None
        if "text" not in msg:                       # image/audio/status → nothing to verify
            return None
        wa_id = msg.get("from", "")
        return InboundMsg(
            wa_id=wa_id, reply_to=f"whatsapp:+{wa_id}",
            text=(msg.get("text", {}).get("body") or "").strip(), msg_sid=msg.get("id", ""),
        )

    async def send(self, reply_to: str, body: str) -> None:
        token = os.environ["WHATSAPP_ACCESS_TOKEN"]
        phone_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
        ver = os.environ.get("WHATSAPP_API_VERSION", "v21.0")
        to = re.sub(r"\D", "", reply_to)            # "whatsapp:+919..." → "919..." (E.164 digits)
        url = _META_GRAPH.format(ver=ver, phone_id=phone_id)
        async with httpx.AsyncClient(timeout=15) as cx:
            r = await cx.post(
                url, headers={"Authorization": f"Bearer {token}"},
                json={"messaging_product": "whatsapp", "recipient_type": "individual",
                      "to": to, "type": "text", "text": {"preview_url": True, "body": body}})
            r.raise_for_status()


def _select_adapter() -> "TwilioWhatsApp | MetaWhatsApp":
    """Provider chosen by WHATSAPP_PROVIDER (default 'meta'; 'twilio' for the old sandbox)."""
    return TwilioWhatsApp() if os.environ.get("WHATSAPP_PROVIDER", "meta").lower() == "twilio" \
        else MetaWhatsApp()


# Active adapter, selected at import from the environment.
adapter: TwilioWhatsApp | MetaWhatsApp = _select_adapter()


async def deliver_verdicts(con, submission_id, reply_to: str) -> None:
    """Push all finished verdict cards for a submission, then clear the reply address."""
    rows = await con.fetch(
        """select v.card from verdicts v join claims c on c.id = v.claim_id
           where c.submission_id = $1 order by v.created_at""",
        submission_id,
    )
    if rows:
        cards = [json.loads(r["card"]) for r in rows]
        await adapter.send(reply_to, format_verdict(cards))
    await _clear_reply_to(con, submission_id)


async def deliver_text(con, submission_id, reply_to: str, message: str) -> None:
    """Push a plain text message (terminal 'nothing to verify' path), then clear reply address."""
    await adapter.send(reply_to, message)
    await _clear_reply_to(con, submission_id)


async def _clear_reply_to(con, submission_id) -> None:
    await con.execute("update submissions set reply_to = null where id = $1", submission_id)


if __name__ == "__main__":  # self-check: parsing + privacy + formatting, no network
    os.environ["WA_HASH_SALT"] = "test-salt"
    tw = TwilioWhatsApp().parse_inbound(
        {"WaId": "919876543210", "From": "whatsapp:+919876543210", "Body": " hi ", "MessageSid": "SM1"})
    assert tw == InboundMsg("919876543210", "whatsapp:+919876543210", "hi", "SM1"), tw
    meta = MetaWhatsApp()
    mt = meta.parse_inbound(
        {"entry": [{"changes": [{"value": {"messages": [
            {"from": "919876543210", "id": "wamid.X", "text": {"body": "hi"}}]}}]}]})
    assert mt.wa_id == tw.wa_id and mt.text == "hi", mt        # same shape from both providers
    assert meta.parse_inbound(                                 # status callback → nothing to verify
        {"entry": [{"changes": [{"value": {"statuses": [{"status": "read"}]}}]}]}) is None
    os.environ["WHATSAPP_VERIFY_TOKEN"] = "vt"
    assert meta.verify_challenge(
        {"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"}) == "42"
    assert meta.verify_challenge({"hub.mode": "subscribe", "hub.verify_token": "no"}) is None
    h = hash_waid("919876543210")
    assert len(h) == 64 and "919876543210" not in h, "raw wa_id must not survive in the hash"
    assert hash_waid("919876543210") == h, "hash must be stable per user"
    msg = format_verdict([{"verdict": "FALSE", "one_liner_native": "No.",
                           "explanation_native": "Water is water [e:e1].", "slug": "s-abc"}])
    assert "❌ FALSE" in msg and "[e:e1]" not in msg and "/v/s-abc" in msg, msg
    print("ok")
