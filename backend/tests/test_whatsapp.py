"""Phase 7 WhatsApp adapter unit tests (no DB, no network).
Covers the adapter boundary (Twilio + Meta parse to one shape, §36), the privacy
invariant (only a salted hash is stored, §35), and forwardable-text formatting."""
import os

import pytest

os.environ.setdefault("WA_HASH_SALT", "unit-test-salt")

from app.services import whatsapp  # noqa: E402


def test_twilio_and_meta_parse_to_same_shape():
    tw = whatsapp.TwilioWhatsApp().parse_inbound(
        {"WaId": "919876543210", "From": "whatsapp:+919876543210", "Body": " Claim ", "MessageSid": "SM1"})
    meta = whatsapp.MetaWhatsApp().parse_inbound(
        {"entry": [{"changes": [{"value": {"messages": [
            {"from": "919876543210", "id": "wamid.X", "text": {"body": "Claim"}}]}}]}]})
    assert tw.wa_id == meta.wa_id == "919876543210"
    assert tw.text == meta.text == "Claim"          # Body is trimmed
    assert tw.reply_to == meta.reply_to == "whatsapp:+919876543210"


def test_hash_is_salted_stable_and_leaks_no_raw_id():
    h = whatsapp.hash_waid("919876543210")
    assert len(h) == 64 and "919876543210" not in h          # raw id never survives
    assert whatsapp.hash_waid("919876543210") == h           # stable per user
    assert whatsapp.hash_waid("911111111111") != h           # distinct users differ


def test_hash_requires_salt(monkeypatch):
    monkeypatch.delenv("WA_HASH_SALT", raising=False)
    with pytest.raises(RuntimeError):
        whatsapp.hash_waid("919876543210")


def test_format_verdict_strips_citations_and_links_permalink(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://juris.example")
    msg = whatsapp.format_verdict([{
        "verdict": "FALSE", "one_liner_native": "Lemon water does not cure cancer.",
        "explanation_native": "No evidence supports it [e:e1]. Doctors disagree [e:e2].", "slug": "lemon-abcd1234"}])
    assert "❌ FALSE" in msg
    assert "[e:" not in msg                                   # citation tags stripped for chat
    assert "https://juris.example/v/lemon-abcd1234" in msg
    assert "Reply 'R'" in msg


def test_ack_twiml_escapes_and_wraps():
    xml = whatsapp.ack_twiml("watch <it> & win")
    assert xml.startswith("<?xml") and "<Message>" in xml
    assert "&lt;it&gt;" in xml and "&amp;" in xml            # payload escaped, not raw
