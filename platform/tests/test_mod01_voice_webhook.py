"""US-001 — Twilio voice ingress: signature, admission, TwiML.

These cover the acceptance criteria that are reachable without a carrier:
AC-4 (capacity full -> busy + callback, no stream), AC-5 (invalid signature ->
403 with no side effect), TAC-1 (validate before anything else), TAC-4
(signature is computed against the public URL, not the request's host header)
and TAC-5 (admission is decided before TwiML is emitted).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from anita.channels import signed_payload  # noqa: E402
from anita.speech import AdmissionController  # noqa: E402
from anita.voice_webhook import (  # noqa: E402
    BUSY_MESSAGE,
    VoiceWebhook,
    build_busy_twiml,
    build_stream_twiml,
    websocket_url,
)

TOKEN = "test-auth-token-not-a-real-one"
PUBLIC = "https://anita.example.com"


def _webhook(admission=None, callback="+12792365960"):
    return VoiceWebhook(
        auth_token=TOKEN,
        public_base_url=PUBLIC,
        admission=admission,
        callback_number=callback,
    )


def _signed(params, path="/twilio/voice"):
    return signed_payload(TOKEN, PUBLIC + path, params)


CALL = {"CallSid": "CA1234567890", "From": "+15551234567", "To": "+12792365960"}


class TestSignature:
    def test_a_valid_signature_is_admitted_and_gets_a_stream(self):
        status, body, ctype = _webhook().voice(params=CALL, signature=_signed(CALL))

        assert status == 200
        assert ctype == "application/xml"
        assert "<Connect>" in body and "<Stream" in body
        assert "wss://anita.example.com/twilio/media-stream" in body

    def test_the_call_id_travels_with_the_stream(self):
        """The WS handler learns which call it holds from the stream itself."""
        _, body, _ = _webhook().voice(params=CALL, signature=_signed(CALL))
        assert 'value="CA1234567890"' in body

    def test_an_invalid_signature_is_refused_with_an_empty_body(self):
        """AC-5: 403, and the body says nothing."""
        status, body, _ = _webhook().voice(params=CALL, signature="not-a-signature")
        assert status == 403
        assert body == ""

    def test_a_missing_signature_is_refused(self):
        status, body, _ = _webhook().voice(params=CALL, signature=None)
        assert status == 403
        assert body == ""

    def test_a_tampered_body_is_refused(self):
        """T-05: signing one call and posting another must not validate."""
        signature = _signed(CALL)
        tampered = dict(CALL, From="+15559999999")
        status, body, _ = _webhook().voice(params=tampered, signature=signature)
        assert status == 403
        assert body == ""

    def test_the_signature_is_checked_against_the_public_url_not_the_host_header(self):
        """TAC-4 / T-16, and the reason `public_base_url` is a setting.

        Twilio signs the URL it called; a Cloudflare tunnel rewrites `Host`. A
        validator that used the request's own host would refuse every webhook
        and the product would appear dead. The handler never sees the request
        host — it signs against the configured public URL — so a request
        delivered with a tunnel host header still validates.
        """
        # Signed for the public URL...
        signature = signed_payload(TOKEN, "https://anita.example.com/twilio/voice", CALL)
        # ...and the tunnel delivered it. The handler only ever uses PUBLIC.
        status, body, _ = _webhook().voice(params=CALL, signature=signature)
        assert status == 200
        assert "<Stream" in body


class TestAdmission:
    def test_a_refused_call_hears_busy_and_opens_no_stream(self):
        """AC-4 / TAC-5: capacity full -> busy TwiML, never a stream URL."""
        admission = AdmissionController(max_concurrent_calls=1)
        hook = _webhook(admission)

        # First call takes the only slot.
        assert hook.voice(params=CALL, signature=_signed(CALL))[0] == 200

        second = {"CallSid": "CA0000000002", "From": "+15550000002", "To": "+12792365960"}
        status, body, _ = hook.voice(params=second, signature=_signed(second))

        assert status == 200
        assert "<Stream" not in body, "a refused call must never be handed a stream"
        assert "busy" in body.lower()
        assert "CA0000000002" not in body or "connect" not in body.lower()

    def test_the_refusal_offers_a_callback_number(self):
        admission = AdmissionController(max_concurrent_calls=0)
        _, body, _ = _webhook(admission).voice(params=CALL, signature=_signed(CALL))

        assert "<Say" in body
        assert "1 2 7 9 2 3 6 5 9 6 0" in body, (
            "the callback number must be spaced so it is read digit by digit"
        )

    def test_admission_is_counted_on_the_controller(self):
        """The refusal is recorded on the four-outcome axis (US-016 AC-2)."""
        admission = AdmissionController(max_concurrent_calls=0)
        _webhook(admission).voice(params=CALL, signature=_signed(CALL))
        assert admission.outcomes["refused"] == 1

    def test_a_refused_call_consumes_nothing(self):
        """AC-5's "no side effect" also holds for a refusal: no slot is taken."""
        admission = AdmissionController(max_concurrent_calls=1)
        hook = _webhook(admission)

        one = {"CallSid": "CA1", "To": "+12792365960"}
        two = {"CallSid": "CA2", "To": "+12792365960"}
        hook.voice(params=one, signature=_signed(one))
        hook.voice(params=two, signature=_signed(two))  # refused

        assert admission.active_calls() == 1, (
            "the refused call must not have taken a slot"
        )


class TestReplayAndAttribution:
    """The two channel-table protections, at the webhook.

    Twilio retries a webhook it believes failed, and a signature proves only
    that Twilio sent it — not that this instance owns the number it was sent
    to.
    """

    @staticmethod
    def _store():
        from anita.channel_store import ChannelStore

        store = ChannelStore(":memory:")
        store.register_endpoint(
            channel_kind="voice_inbound", provider="twilio",
            address="+12792365960", instance_id="anita",
            secret_ref="TWILIO_AUTH_TOKEN",
        )
        return store

    def _hook(self, store):
        return VoiceWebhook(
            auth_token=TOKEN, public_base_url=PUBLIC, admission=None,
            callback_number="+12792365960", store=store,
            instance_id="anita", address="+12792365960",
        )

    def test_the_first_delivery_gets_a_stream(self):
        store = self._store()
        _, body, _ = self._hook(store).voice(params=CALL, signature=_signed(CALL))
        assert "<Stream" in body

    def test_a_replayed_call_sid_gets_no_second_stream(self):
        """A replay must not open a second media stream for one call."""
        store = self._store()
        hook = self._hook(store)
        hook.voice(params=CALL, signature=_signed(CALL))

        status, body, _ = hook.voice(params=CALL, signature=_signed(CALL))

        assert status == 200, "200 so Twilio stops retrying"
        assert "<Stream" not in body, "a replay opened a second stream"

    def test_a_different_call_still_gets_a_stream(self):
        store = self._store()
        hook = self._hook(store)
        hook.voice(params=CALL, signature=_signed(CALL))

        other = {"CallSid": "CA9999999999", "To": "+12792365960"}
        _, body, _ = hook.voice(params=other, signature=_signed(other))
        assert "<Stream" in body

    def test_a_call_to_a_number_we_do_not_own_is_refused(self):
        """Edge case 14: valid signature, someone else's number."""
        store = self._store()
        foreign = {"CallSid": "CA-foreign", "To": "+15550001111"}

        status, body, _ = self._hook(store).voice(
            params=foreign, signature=_signed(foreign)
        )

        assert status == 403
        assert body == ""

    def test_the_foreign_call_is_recorded_not_dropped(self):
        store = self._store()
        foreign = {"CallSid": "CA-foreign", "To": "+15550001111"}
        self._hook(store).voice(params=foreign, signature=_signed(foreign))

        assert store.event_disposition("CA-foreign") == "rejected"

    def test_a_refused_foreign_call_consumes_no_capacity(self):
        from anita.speech import AdmissionController

        admission = AdmissionController(max_concurrent_calls=1)
        store = self._store()
        hook = VoiceWebhook(
            auth_token=TOKEN, public_base_url=PUBLIC, admission=admission,
            callback_number="+12792365960", store=store,
            instance_id="anita", address="+12792365960",
        )
        foreign = {"CallSid": "CA-foreign", "To": "+15550001111"}
        hook.voice(params=foreign, signature=_signed(foreign))

        assert admission.active_calls() == 0
        assert admission.outcomes["refused"] == 0, (
            "a foreign number is not a capacity refusal and must not be counted "
            "as one"
        )

    def test_without_a_store_the_webhook_still_answers(self):
        """An instance with no database answers; it just cannot deduplicate."""
        _, body, _ = _webhook().voice(params=CALL, signature=_signed(CALL))
        assert "<Stream" in body


class TestStatusCallback:
    def test_a_terminal_status_releases_the_slot(self):
        admission = AdmissionController(max_concurrent_calls=1)
        hook = _webhook(admission)
        hook.voice(params=CALL, signature=_signed(CALL))
        assert admission.active_calls() == 1

        status_params = {"CallSid": "CA1234567890", "CallStatus": "completed"}
        signature = _signed(status_params, path="/twilio/status")
        status, _, _ = hook.status(params=status_params, signature=signature)

        assert status == 200
        assert admission.active_calls() == 0

    def test_a_status_callback_must_also_be_signed(self):
        admission = AdmissionController(max_concurrent_calls=1)
        hook = _webhook(admission)
        hook.voice(params=CALL, signature=_signed(CALL))

        status, _, _ = hook.status(
            params={"CallSid": "CA1234567890", "CallStatus": "completed"},
            signature="bad",
        )
        assert status == 403
        assert admission.active_calls() == 1, "an unsigned callback must change nothing"


class TestTwiML:
    def test_the_stream_url_is_derived_from_the_public_base(self):
        assert websocket_url("https://a.example.com") == "wss://a.example.com/twilio/media-stream"
        assert websocket_url("http://localhost:8100/") == "ws://localhost:8100/twilio/media-stream"
        assert websocket_url("") == "/twilio/media-stream"

    def test_stream_twiml_cannot_be_broken_out_of_by_a_quote(self):
        """A quote in the URL must stay inside the attribute.

        Asserted by round-tripping through a parser rather than by looking for
        `&quot;`: `quoteattr` picks whichever delimiter is safe, so it emits
        single quotes here and the value needs no escaping at all. What matters
        is that the attribute still holds exactly the original string and no
        extra attribute was smuggled in.
        """
        import xml.etree.ElementTree as ET

        url = 'wss://x.example.com/a"b'
        call_id = 'CA"1 onload="x'
        root = ET.fromstring(build_stream_twiml(url, call_id))

        stream = root.find("./Connect/Stream")
        assert stream is not None
        assert stream.get("url") == url, "the URL must survive intact"
        param = stream.find("Parameter")
        assert param is not None
        assert param.get("value") == call_id
        assert param.get("onload") is None, "no attribute may be smuggled in"

    def test_the_twiml_carries_a_token_when_a_minter_is_installed(self):
        """§8.6: the media-stream socket is public, so the TwiML must hand the
        relay the proof that this call was admitted. The token names the call,
        so a token minted for one call cannot open another's stream."""
        import xml.etree.ElementTree as ET

        from anita.browser_voice import SessionTokens

        tokens = SessionTokens("test-secret")
        hook = VoiceWebhook(
            auth_token=TOKEN, public_base_url=PUBLIC, stream_tokens=tokens
        )
        status, body, _ = hook.voice(params=dict(CALL), signature=_signed(CALL))
        assert status == 200

        params = {
            p.get("name"): p.get("value")
            for p in ET.fromstring(body).findall("./Connect/Stream/Parameter")
        }
        assert params["call_id"] == "CA1234567890"
        verified = tokens.verify(params["token"])
        assert verified.session_id == "CA1234567890"

    def test_no_token_is_emitted_without_a_minter(self):
        """The webhook stays usable with no signer (tests, tools): no token is
        written, and the relay that reads the same object requires none."""
        status, body, _ = _webhook().voice(params=dict(CALL), signature=_signed(CALL))
        assert status == 200
        assert "token" not in body

    def test_busy_twiml_escapes_the_message(self):
        xml = build_busy_twiml('Sorry <all> "lines" are busy')
        assert "<all>" not in xml
        assert "&lt;all&gt;" in xml

    def test_busy_twiml_omits_the_number_when_none_is_configured(self):
        xml = build_busy_twiml(BUSY_MESSAGE, "")
        assert "<Pause" not in xml

    def test_twiml_is_well_formed(self):
        """Both shapes must parse — Twilio rejects malformed TwiML at the edge."""
        import xml.etree.ElementTree as ET

        for xml in (
            build_stream_twiml("wss://a.example.com/twilio/media-stream", "CA1"),
            build_busy_twiml(BUSY_MESSAGE, "+12792365960"),
        ):
            root = ET.fromstring(xml)
            assert root.tag == "Response"


class TestTheRoutesAreReachable:
    """The webhook is only useful if Twilio can actually POST to it.

    Everything above tests `VoiceWebhook` as an object. These drive the real
    ASGI app over HTTP — which is what catches a route that was built and
    wired but never registered, the state this was in before.
    """

    @staticmethod
    def _client(webhook):
        from fastapi.testclient import TestClient

        from anita.server import create_app
        from anita.surfaces import Router

        return TestClient(create_app(Router(voice=webhook)))

    def test_a_signed_webhook_over_http_returns_twiml(self):
        client = self._client(_webhook())
        response = client.post(
            "/twilio/voice",
            data=CALL,
            headers={"X-Twilio-Signature": _signed(CALL)},
        )
        assert response.status_code == 200
        assert "<Stream" in response.text

    def test_the_form_body_is_what_gets_validated(self):
        """Twilio signs the sorted form fields, so the raw form must be used.

        A handler that validated a parsed subset — or an empty dict — would
        accept a signature Twilio never issued for this body.
        """
        client = self._client(_webhook())
        response = client.post(
            "/twilio/voice",
            data=CALL,
            headers={"X-Twilio-Signature": _signed(CALL)},
        )
        assert response.status_code == 200

    def test_an_unsigned_request_over_http_is_403_with_an_empty_body(self):
        client = self._client(_webhook())
        response = client.post("/twilio/voice", data=CALL)
        assert response.status_code == 403
        assert response.text == ""

    def test_a_tampered_form_over_http_is_refused(self):
        client = self._client(_webhook())
        tampered = dict(CALL, From="+15559999999")
        response = client.post(
            "/twilio/voice",
            data=tampered,
            headers={"X-Twilio-Signature": _signed(CALL)},
        )
        assert response.status_code == 403

    def test_the_status_route_is_registered_and_releases_capacity(self):
        admission = AdmissionController(max_concurrent_calls=1)
        client = self._client(_webhook(admission))

        client.post("/twilio/voice", data=CALL,
                    headers={"X-Twilio-Signature": _signed(CALL)})
        assert admission.active_calls() == 1

        status_params = {"CallSid": "CA1234567890", "CallStatus": "completed"}
        response = client.post(
            "/twilio/status",
            data=status_params,
            headers={"X-Twilio-Signature": _signed(status_params, path="/twilio/status")},
        )
        assert response.status_code == 200
        assert admission.active_calls() == 0

    def test_no_voice_routes_are_served_without_a_public_address(self):
        """No tunnel means no legitimate caller, so the paths do not exist.

        Registering them anyway would expose an endpoint that can only ever
        refuse, and a 404 is the honest answer to a request that cannot be
        verified.
        """
        from fastapi.testclient import TestClient

        from anita.server import create_app
        from anita.surfaces import Router

        client = TestClient(create_app(Router()))
        assert client.post("/twilio/voice", data=CALL).status_code == 404
        assert client.post("/twilio/status", data=CALL).status_code == 404
