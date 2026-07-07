"""WhatsApp channel (LLD §8, Phase 7). Inbound webhook parsing + outbound delivery,
behind an adapter so Twilio (sandbox demo) can be swapped for the Meta Cloud API later.

Privacy (non-negotiable, §35): only a salted hash of wa_id is ever stored/logged. The
raw reply address lives on `submissions.reply_to` for the in-flight job and is nulled
after the verdict is sent — it never reaches events_log (which the public UI reads)."""
import hashlib
import os
import re
from dataclasses import dataclass

import httpx

from ..config import public_base_url

_TWILIO_MSGS = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
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
    """Twilio WhatsApp sandbox adapter (form-encoded webhook + REST send)."""

    def parse_inbound(self, form: dict) -> InboundMsg:
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
    """Meta Cloud API adapter — parse only for now (post-hackathon swap, LLD §8).
    ponytail: send() lands when Meta approval does; parse_inbound exists so the webhook
    handler and its unit test are already provider-agnostic."""

    def parse_inbound(self, payload: dict) -> InboundMsg:
        msg = payload["entry"][0]["changes"][0]["value"]["messages"][0]
        wa_id = msg.get("from", "")
        return InboundMsg(
            wa_id=wa_id, reply_to=f"whatsapp:+{wa_id}",
            text=(msg.get("text", {}).get("body") or "").strip(), msg_sid=msg.get("id", ""),
        )

    async def send(self, reply_to: str, body: str) -> None:  # pragma: no cover
        raise NotImplementedError("Meta Cloud API send not wired yet (v1 uses Twilio sandbox)")


# Active adapter. Swap to MetaWhatsApp() when Cloud API approval lands — one line.
adapter: TwilioWhatsApp | MetaWhatsApp = TwilioWhatsApp()


async def deliver_verdicts(con, submission_id, reply_to: str) -> None:
    """Push all finished verdict cards for a submission, then clear the reply address."""
    rows = await con.fetch(
        """select v.card from verdicts v join claims c on c.id = v.claim_id
           where c.submission_id = $1 order by v.created_at""",
        submission_id,
    )
    if rows:
        import json
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
    mt = MetaWhatsApp().parse_inbound(
        {"entry": [{"changes": [{"value": {"messages": [
            {"from": "919876543210", "id": "wamid.X", "text": {"body": "hi"}}]}}]}]})
    assert mt.wa_id == tw.wa_id and mt.text == "hi", mt        # same shape from both providers
    h = hash_waid("919876543210")
    assert len(h) == 64 and "919876543210" not in h, "raw wa_id must not survive in the hash"
    assert hash_waid("919876543210") == h, "hash must be stable per user"
    msg = format_verdict([{"verdict": "FALSE", "one_liner_native": "No.",
                           "explanation_native": "Water is water [e:e1].", "slug": "s-abc"}])
    assert "❌ FALSE" in msg and "[e:e1]" not in msg and "/v/s-abc" in msg, msg
    print("ok")
