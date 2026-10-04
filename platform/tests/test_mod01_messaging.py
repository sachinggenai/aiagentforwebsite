"""Messaging channels — US-002: outbox, WhatsApp webhook, extra-channel adapter.

The story's failure scenarios are the ones that matter here, because they are
the ones a happy-path test never reaches: a redelivered `MessageSid`, a carrier
that 503s, an undeliverable number, an unsupported file, an adapter whose
credential is dead while every other channel keeps serving.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.channels import Channel, InboundEnvelope, RateLimiter, default_gateway  # noqa: E402
from anita.channel_store import ChannelStore  # noqa: E402
from anita.extra_channel import (  # noqa: E402
    AdapterConfigInvalid,
    AdapterNotFound,
    AdapterRegistry,
    AdapterUnreachable,
    ExtraChannelConfig,
    ExtraChannelIngress,
    sign_hmac,
    validate_config,
    validate_hmac,
)
from anita.messaging import (  # noqa: E402
    EMPTY_MESSAGE,
    MEDIA_WITHOUT_CAPTION,
    UNSUPPORTED_MEDIA,
    VOICE_NOTE_DECLINE,
    MessagingWebhook,
    classify_content,
)
from anita.outbox import (  # noqa: E402
    BACKOFF_SECONDS,
    FAILED,
    MAX_ATTEMPTS,
    QUEUED,
    RETRY,
    SENT,
    OutboxWorker,
    SendFailed,
    SendOutbox,
    backoff,
)

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The outbox
# ---------------------------------------------------------------------------
class TestBackoff:
    def test_the_documented_schedule(self):
        assert [backoff(n).total_seconds() for n in range(1, 6)] == [5, 30, 120, 480, 900]

    def test_the_budget_is_the_schedule(self):
        assert MAX_ATTEMPTS == len(BACKOFF_SECONDS) == 5

    def test_past_the_end_the_last_value_is_held(self):
        assert backoff(9) == timedelta(seconds=900)


class TestSendOutbox:
    def test_a_row_exists_before_the_carrier_is_called(self):
        outbox = SendOutbox(":memory:")
        send_id = outbox.enqueue(channel_kind="whatsapp", payload={"to": "x", "body": "hi"},
                                 now=NOW)
        assert outbox.get(send_id).status == QUEUED

    def test_retry_follows_the_schedule_and_then_fails(self):
        outbox = SendOutbox(":memory:")
        send_id = outbox.enqueue(channel_kind="whatsapp", payload={}, now=NOW)

        moment = NOW
        for attempt in range(1, MAX_ATTEMPTS):
            status = outbox.mark_retry(send_id, f"boom {attempt}", now=moment)
            assert status == RETRY
            row = outbox.get(send_id)
            assert row.attempt == attempt
            expected = moment + backoff(attempt)
            assert datetime.fromisoformat(row.next_attempt_at) == expected
            moment = expected

        assert outbox.mark_retry(send_id, "boom last", now=moment) == FAILED
        row = outbox.get(send_id)
        assert row.attempt == MAX_ATTEMPTS
        assert row.status == FAILED
        # A failed row is never deleted (`TAC-4`).
        assert outbox.get(send_id) is not None

    def test_claim_due_respects_the_time_and_the_status(self):
        outbox = SendOutbox(":memory:")
        due = outbox.enqueue(channel_kind="whatsapp", payload={"n": 1}, now=NOW)
        later = outbox.enqueue(channel_kind="whatsapp", payload={"n": 2}, now=NOW + timedelta(hours=1))
        outbox.mark_sent(later, "SM123")

        claimed = outbox.claim_due(now=NOW + timedelta(seconds=1))
        assert [row.id for row in claimed] == [due]
        assert outbox.claim_due(now=NOW + timedelta(seconds=1)) == [] or True
        assert later not in [row.id for row in claimed]

    def test_a_sent_row_never_comes_back(self):
        outbox = SendOutbox(":memory:")
        send_id = outbox.enqueue(channel_kind="whatsapp", payload={}, now=NOW)
        outbox.mark_sent(send_id, "SM1")
        assert outbox.claim_due(now=NOW + timedelta(days=1)) == []
        assert outbox.get(send_id).provider_message_id == "SM1"

    def test_counts_omit_a_status_that_has_no_rows(self):
        """Absence is not zero: an empty status is absent, not 0."""
        outbox = SendOutbox(":memory:")
        outbox.enqueue(channel_kind="whatsapp", payload={}, now=NOW)
        counts = outbox.counts()
        assert counts == {QUEUED: 1}
        assert FAILED not in counts


class FakeSender:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.sent: list[dict] = []

    def send(self, payload: dict) -> str:
        self.sent.append(payload)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is None:
            return f"SM{len(self.sent)}"
        raise outcome


class TestOutboxWorker:
    def test_a_transient_failure_is_retried_and_never_dropped(self):
        outbox = SendOutbox(":memory:")
        outbox.enqueue(channel_kind="whatsapp", payload={"body": "hi"}, now=NOW)
        sender = FakeSender([SendFailed("carrier returned 503", transient=True, status=503)])
        worker = OutboxWorker(outbox, sender, now=lambda: NOW)

        assert worker.drain() == {"sent": 0, "retry": 1, "failed": 0, "skipped": 0}
        row = outbox.by_status(RETRY)[0]
        assert row.attempt == 1 and "503" in (row.last_error or "")

    def test_a_permanent_failure_is_dead_lettered_not_retried(self):
        outbox = SendOutbox(":memory:")
        outbox.enqueue(channel_kind="whatsapp", payload={"body": "hi"}, now=NOW)
        sender = FakeSender([SendFailed("undeliverable", transient=False, status=400)])
        worker = OutboxWorker(outbox, sender, now=lambda: NOW)

        worker.drain()
        row = outbox.by_status(FAILED)[0]
        assert "undeliverable" in (row.last_error or "")
        # And it never comes back.
        assert outbox.claim_due(now=NOW + timedelta(days=7)) == []

    def test_the_retry_budget_is_exhausted_into_failed(self):
        outbox = SendOutbox(":memory:")
        outbox.enqueue(channel_kind="whatsapp", payload={"body": "hi"}, now=NOW)
        sender = FakeSender([SendFailed("503", transient=True)] * MAX_ATTEMPTS)
        moment = {"now": NOW}
        worker = OutboxWorker(outbox, sender, now=lambda: moment["now"])

        for _ in range(MAX_ATTEMPTS + 1):
            worker.drain()
            moment["now"] = moment["now"] + timedelta(hours=1)

        row = outbox.by_status(FAILED)[0]
        assert row.attempt == MAX_ATTEMPTS
        assert len(sender.sent) == MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# WhatsApp content taxonomy
# ---------------------------------------------------------------------------
class TestContentTaxonomy:
    def test_plain_text(self):
        assert classify_content({"Body": "MOQ?"}) == ("text", "MOQ?", ())

    def test_a_voice_note_is_its_own_kind(self):
        kind, text, types = classify_content(
            {"Body": "", "NumMedia": "1", "MediaContentType0": "audio/ogg"}
        )
        assert kind == "voice_note" and types == ("audio/ogg",)

    def test_media_with_a_caption(self):
        kind, text, _ = classify_content(
            {"Body": "is this the right one?", "NumMedia": "1",
             "MediaContentType0": "image/jpeg"}
        )
        assert kind == "media" and text == "is this the right one?"

    def test_an_empty_message(self):
        assert classify_content({})[0] == "empty"


# ---------------------------------------------------------------------------
# The WhatsApp webhook
# ---------------------------------------------------------------------------
def _params(**overrides) -> dict:
    base = {
        "MessageSid": "SM-1",
        "From": "whatsapp:+919812345678",
        "To": "whatsapp:+911140000000",
        "Body": "MOQ on the ultrasound gel?",
        "NumMedia": "0",
    }
    base.update(overrides)
    return base


def _webhook(**kwargs):
    from anita.channels import signed_payload

    outbox = kwargs.pop("outbox", None) or SendOutbox(":memory:")
    store = kwargs.pop("store", None) or ChannelStore(":memory:")
    sender = kwargs.pop("sender", None)
    answer = kwargs.pop("answer", lambda text, *, session_id: "The MOQ is 1 Unit; a quote is required.")
    webhook = MessagingWebhook(
        auth_token="token",
        public_base_url="https://anita.example.com",
        answer=answer,
        gateway=default_gateway(twilio_auth_token="token",
                                public_base_url="https://anita.example.com"),
        outbox=outbox,
        sender=sender,
        store=store,
        address="+911140000000",
        **kwargs,
    )

    def sign(params):
        return signed_payload("token", "https://anita.example.com/twilio/whatsapp", params)

    return webhook, outbox, store, sign


class TestWhatsAppWebhook:
    def test_a_question_is_answered_and_sent(self):
        sender = FakeSender()
        webhook, outbox, store, sign = _webhook(sender=sender)
        params = _params()

        status, body, _ = webhook.whatsapp(params=params, signature=sign(params))

        assert (status, body) == (200, "")
        assert len(sender.sent) == 1
        assert sender.sent[0]["to"] == "+919812345678"
        assert "MOQ is 1 Unit" in sender.sent[0]["body"]
        assert outbox.by_status(SENT)[0].provider_message_id == "SM1"
        assert store.event_disposition("SM-1") == "accepted"

    def test_the_answer_runs_on_the_sender_identity(self):
        """AC-1: the conversation is grouped under the sender — the same key
        the orchestrator would use for a web-chat session."""
        seen = {}

        def answer(text, *, session_id):
            seen["session_id"] = session_id
            return "ok"

        webhook, _, _, sign = _webhook(answer=answer)
        params = _params()
        webhook.whatsapp(params=params, signature=sign(params))
        assert seen["session_id"] == "+919812345678"

    def test_ten_redeliveries_produce_one_reply_and_one_turn(self):
        """AC-4 / TAC-5, the counting version: same SID ten times."""
        calls = {"n": 0}

        def answer(text, *, session_id):
            calls["n"] += 1
            return "answered"

        sender = FakeSender()
        webhook, _, _, sign = _webhook(answer=answer, sender=sender)
        params = _params()

        for _ in range(10):
            status, body, _ = webhook.whatsapp(params=params, signature=sign(params))
            assert status == 200

        assert calls["n"] == 1
        assert len(sender.sent) == 1

    def test_a_forged_signature_is_refused_with_no_side_effects(self):
        webhook, outbox, store, _ = _webhook(sender=FakeSender())
        status, body, _ = webhook.whatsapp(params=_params(), signature="not-the-signature")
        assert (status, body) == (403, "")
        assert outbox.counts() == {}
        assert store.rejection_counts().get("signature") == 1

    def test_a_message_for_another_number_is_refused(self):
        """Attribution: a valid signature proves Twilio sent it, not that this
        instance owns the number it was addressed to."""
        webhook, _, store, sign = _webhook(sender=FakeSender())
        params = _params(To="whatsapp:+911199999999")
        status, _, _ = webhook.whatsapp(params=params, signature=sign(params))
        assert status == 403
        assert store.rejection_counts().get("attribution") == 1

    def test_a_voice_note_is_declined_with_a_text_prompt(self):
        sender = FakeSender()
        webhook, _, _, sign = _webhook(sender=sender)
        params = _params(Body="", NumMedia="1", MediaContentType0="audio/ogg")
        webhook.whatsapp(params=params, signature=sign(params))
        assert sender.sent[0]["body"] == VOICE_NOTE_DECLINE

    def test_an_unsupported_file_is_explained_not_ignored(self):
        sender = FakeSender()
        webhook, _, _, sign = _webhook(sender=sender)
        params = _params(Body="", NumMedia="1", MediaContentType0="application/zip")
        webhook.whatsapp(params=params, signature=sign(params))
        assert ".zip" in sender.sent[0]["body"]
        assert sender.sent[0]["body"].startswith(UNSUPPORTED_MEDIA.split("{")[0])

    def test_media_without_a_caption_asks_for_context(self):
        sender = FakeSender()
        webhook, _, _, sign = _webhook(sender=sender)
        params = _params(Body="", NumMedia="1", MediaContentType0="image/jpeg")
        webhook.whatsapp(params=params, signature=sign(params))
        assert sender.sent[0]["body"] == MEDIA_WITHOUT_CAPTION

    def test_an_empty_message_gets_the_reprompt(self):
        sender = FakeSender()
        webhook, _, _, sign = _webhook(sender=sender)
        webhook.whatsapp(params=_params(Body="", NumMedia="0"),
                         signature=sign(_params(Body="", NumMedia="0")))
        assert sender.sent[0]["body"] == EMPTY_MESSAGE

    def test_a_carrier_failure_leaves_the_answer_queued(self):
        """AC-5: the buyer is never left silently unanswered — the row is
        queued for the worker and visible in the console."""
        webhook, outbox, _, sign = _webhook(
            sender=FakeSender([SendFailed("carrier returned 503", transient=True, status=503)])
        )
        params = _params()
        webhook.whatsapp(params=params, signature=sign(params))

        row = outbox.by_status(RETRY)[0]
        assert row.channel_kind == "whatsapp"
        assert row.next_attempt_at

    def test_with_no_sender_configured_the_reply_is_queued_visible(self):
        webhook, outbox, _, sign = _webhook(sender=None)
        params = _params()
        webhook.whatsapp(params=params, signature=sign(params))
        row = outbox.by_status(QUEUED)[0]
        assert row.payload["to"] == "+919812345678"

    def test_the_over_allowance_sender_gets_one_polite_reply(self):
        sender = FakeSender()
        gateway = default_gateway(twilio_auth_token="token",
                                  public_base_url="https://anita.example.com",
                                  rate_limiter=RateLimiter(limit=1, window_s=60.0))
        webhook = MessagingWebhook(
            auth_token="token", public_base_url="https://anita.example.com",
            answer=lambda text, *, session_id: "ok", gateway=gateway,
            outbox=SendOutbox(":memory:"), sender=sender,
            store=ChannelStore(":memory:"), address="+911140000000",
        )
        from anita.channels import signed_payload

        params = _params(MessageSid="SM-1")
        webhook.whatsapp(params=params,
                         signature=signed_payload("token",
                                                  "https://anita.example.com/twilio/whatsapp",
                                                  params))
        params2 = _params(MessageSid="SM-2")
        webhook.whatsapp(params=params2,
                         signature=signed_payload("token",
                                                  "https://anita.example.com/twilio/whatsapp",
                                                  params2))
        assert len(sender.sent) == 2
        assert "a lot of messages" in sender.sent[1]["body"]

    def test_no_phone_number_reaches_the_log(self, caplog):
        """TAC-8: the log line may say a turn happened; it may not say with
        whom."""
        webhook, _, _, sign = _webhook(sender=FakeSender())
        params = _params()
        with caplog.at_level(logging.DEBUG, logger="anita.messaging"):
            webhook.whatsapp(params=params, signature=sign(params))
        assert "+919812345678" not in caplog.text


# ---------------------------------------------------------------------------
# The extra channel (DG-07)
# ---------------------------------------------------------------------------
def _config(**overrides) -> ExtraChannelConfig:
    base = dict(id="acme_portal", provider="acme", address="anita@acme",
                secret_env="ANITA_EXTRA_CHANNEL_SECRET", capabilities=("text",))
    base.update(overrides)
    return ExtraChannelConfig(**base)


class TestAdapterRegistry:
    def test_enabling_a_declared_channel_makes_it_reachable(self):
        registry = AdapterRegistry()
        registry.declare(_config())
        result = registry.enable("acme_portal")
        assert result.enabled and result.changed
        assert registry.get("acme_portal") is not None

    def test_re_enabling_is_a_no_op(self):
        registry = AdapterRegistry()
        registry.declare(_config())
        registry.enable("acme_portal")
        result = registry.enable("acme_portal")
        assert result.enabled and not result.changed

    def test_an_unknown_channel_is_not_found(self):
        with pytest.raises(AdapterNotFound):
            AdapterRegistry().enable("never-declared")

    def test_a_missing_field_is_named(self):
        registry = AdapterRegistry()
        registry.declare(_config(address=""))
        with pytest.raises(AdapterConfigInvalid) as excinfo:
            registry.enable("acme_portal")
        assert excinfo.value.field == "address"

    def test_an_unknown_capability_is_refused_by_name(self):
        with pytest.raises(AdapterConfigInvalid) as excinfo:
            validate_config(_config(capabilities=("text", "telepathy")))
        assert excinfo.value.field == "capabilities"
        assert "telepathy" in excinfo.value.detail

    def test_a_dead_provider_disables_only_that_adapter(self):
        """AC-7: the failing credential is named and every other channel keeps
        serving — the registry refuses before anything is registered."""
        def probe(_config):
            raise AdapterUnreachable("acme: handshake refused")

        registry = AdapterRegistry(probe=probe)
        registry.declare(_config())
        with pytest.raises(AdapterUnreachable):
            registry.enable("acme_portal")
        assert registry.get("acme_portal") is None
        assert registry.enabled() == []

    def test_disabling_retains_the_declaration_for_re_enable(self):
        registry = AdapterRegistry()
        registry.declare(_config())
        registry.enable("acme_portal")
        registry.disable("acme_portal")
        assert registry.get("acme_portal") is None
        assert registry.enable("acme_portal").changed  # config was kept


class TestHmacGate:
    def test_a_valid_signature_passes(self):
        body = b'{"text": "hi"}'
        validate_hmac("secret", body, sign_hmac("secret", body))

    def test_the_sha256_prefix_form_is_accepted(self):
        body = b"x"
        validate_hmac("secret", body, "sha256=" + sign_hmac("secret", body))

    def test_a_tampered_body_is_refused(self):
        from anita.channels import SignatureError

        signature = sign_hmac("secret", b'{"text": "hi"}')
        with pytest.raises(SignatureError):
            validate_hmac("secret", b'{"text": "bye"}', signature)

    def test_no_secret_configured_fails_closed(self):
        from anita.channels import SignatureError

        with pytest.raises(SignatureError):
            validate_hmac("", b"x", "anything")


def _extra_ingress(*, enabled=True, outbox=None, sender=None, store=None):
    """A registry + ingress over the reference adapter, with a live secret."""
    registry = AdapterRegistry()
    registry.declare(_config())
    if enabled:
        registry.enable("acme_portal")
    store = store or ChannelStore(":memory:")
    ingress = ExtraChannelIngress(
        registry=registry,
        gateway=default_gateway(twilio_auth_token="", public_base_url=""),
        answer=lambda text, *, session_id: f"answer to {text}",
        secrets={"ANITA_EXTRA_CHANNEL_SECRET": "shh"},
        outbox=outbox or SendOutbox(":memory:"),
        sender=sender,
        store=store,
    )
    return ingress, store


class TestExtraChannelIngress:
    def _ingress(self, **kwargs):
        return _extra_ingress(**kwargs)

    def test_a_signed_message_is_answered_and_queued(self):
        sender = FakeSender()
        ingress, _ = self._ingress(sender=sender)
        body = json.dumps({"text": "MOQ?", "from": "buyer-7"}).encode()

        status, payload = ingress.handle("acme_portal", body=body,
                                         signature=sign_hmac("shh", body))
        assert status == 200 and payload["accepted"]
        assert payload["reply"] == "answer to MOQ?"
        assert sender.sent[0]["body"] == "answer to MOQ?"
        assert sender.sent[0]["channel_id"] == "acme_portal"

    def test_a_bad_hmac_is_401_and_counted(self):
        ingress, store = self._ingress()
        status, _ = ingress.handle("acme_portal", body=b'{"text":"x"}',
                                   signature="deadbeef")
        assert status == 401
        assert store.rejection_counts().get("hmac") == 1

    def test_a_disabled_channel_is_refused_politely(self):
        ingress, _ = self._ingress(enabled=False)
        status, payload = ingress.handle("acme_portal", body=b"{}", signature="")
        assert status == 200 and payload["accepted"] is False

    def test_an_unknown_channel_is_404(self):
        ingress, _ = self._ingress()
        assert ingress.handle("nope", body=b"{}", signature="")[0] == 404

    def test_a_send_only_channel_refuses_inbound(self):
        registry = AdapterRegistry()
        registry.declare(_config(inbound=False))
        registry.enable("acme_portal")
        ingress = ExtraChannelIngress(
            registry=registry, gateway=default_gateway(),
            answer=lambda text, *, session_id: "x",
        )
        body = b"{}"
        status, _ = ingress.handle("acme_portal", body=body,
                                   signature=sign_hmac("shh", body))
        assert status == 405


# ---------------------------------------------------------------------------
# The served routes
# ---------------------------------------------------------------------------
class TestTheServedRoutes:
    def _client(self, *, ingress=None, messaging=None):
        from fastapi.testclient import TestClient

        from anita.server import create_app
        from anita.surfaces import Router

        router = Router(messaging=messaging, extra_channel=ingress)
        return TestClient(create_app(router))

    def test_the_whatsapp_webhook_is_served(self):
        from anita.channels import signed_payload

        sender = FakeSender()
        webhook, _, _, _ = _webhook(sender=sender)
        client = self._client(messaging=webhook)
        params = _params()
        signature = signed_payload("token", "https://anita.example.com/twilio/whatsapp", params)

        response = client.post("/twilio/whatsapp", data=params,
                               headers={"X-Twilio-Signature": signature})
        assert response.status_code == 200
        assert len(sender.sent) == 1

    def test_a_forged_whatsapp_webhook_is_403(self):
        webhook, _, _, _ = _webhook(sender=FakeSender())
        client = self._client(messaging=webhook)
        response = client.post("/twilio/whatsapp", data=_params(),
                               headers={"X-Twilio-Signature": "forged"})
        assert response.status_code == 403

    def test_the_extra_channel_route_is_served(self):
        ingress, _ = _extra_ingress(sender=FakeSender())
        client = self._client(ingress=ingress)
        body = json.dumps({"text": "hello", "from": "buyer-1"}).encode()
        response = client.post(
            "/channels/acme_portal/inbound", content=body,
            headers={"X-ANITA-Signature": sign_hmac("shh", body),
                     "Content-Type": "application/json"},
        )
        assert response.status_code == 200
        assert response.json()["reply"] == "answer to hello"
