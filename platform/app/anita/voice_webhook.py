"""Twilio voice ingress: signature, admission, TwiML, and the status callback.

US-001 (TRD-02). Three things happen in a fixed order when a call arrives, and
the order is the contract (`TAC-1`, `TAC-5`):

1. **Validate the signature** against the configured *public* base URL. A
   failure is 403 with an empty body, before any work is done and before the
   body is trusted for anything (`AC-5`, `TAC-4`, `DG-06`).
2. **Decide admission** — before TwiML is emitted, never after. A refused call
   hears a busy message and a callback offer, and **no media stream is opened**
   (`AC-4`, `TAC-5`).
3. **Emit TwiML** — `<Connect><Stream>` for an admitted call.

Why the public URL is configured rather than read from the request: Twilio signs
the URL it called, and a tunnel rewrites `Host`. A validator that used the
request's own host header would fail every webhook closed and the product would
simply appear dead (`MOD-01` §6.1). The tunnel is the reason this is a setting,
not a derivation.
"""

from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr

TwiML_HEADER = '<?xml version="1.0" encoding="UTF-8"?>'
MEDIA_STREAM_PATH = "/twilio/media-stream"

# Said when capacity is full. Deliberately not an apology loop: it states what
# happened, offers the callback, and gets off the line. `AC-4` requires the
# caller hears *something* — never silence.
BUSY_MESSAGE = (
    "Sorry, all our lines are busy right now. "
    "Please leave it a moment and call back, or ask us for a callback on the website."
)


def websocket_url(public_base_url: str) -> str:
    """The `wss://` URL of the media stream, derived from the public base URL.

    Derived rather than configured separately: two settings that must agree are
    two settings that can disagree, and the symptom would be a stream Twilio
    cannot open.
    """
    base = (public_base_url or "").rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return base + MEDIA_STREAM_PATH


def build_stream_twiml(ws_url: str, call_id: str) -> str:
    """TwiML that hands the call to our media stream.

    `call_id` is passed as a `<Parameter>` so the WebSocket handler learns which
    call it is holding from the stream itself, rather than correlating against
    some other table it would have to keep in step.
    """
    return (
        f"{TwiML_HEADER}\n"
        "<Response>\n"
        "  <Connect>\n"
        f"    <Stream url={quoteattr(ws_url)}>\n"
        f"      <Parameter name=\"call_id\" value={quoteattr(call_id)}/>\n"
        "    </Stream>\n"
        "  </Connect>\n"
        "</Response>\n"
    )


def build_busy_twiml(message: str = BUSY_MESSAGE, callback_number: str = "") -> str:
    """TwiML for a refused call: say why, name a callback number, hang up.

    Twilio's own `<Say>` is used rather than a pre-synthesised asset because
    this is the one sentence that must work when the local TTS stack is the
    thing that is unavailable. The source used a bundled WAV here; this is the
    reason `voice_admission.asset_path` returns None and the carrier speaks
    instead (`US-016` AC-5).
    """
    parts = [TwiML_HEADER, "<Response>"]
    parts.append(f'  <Say voice="Polly.Aditi">{escape(message)}</Say>')
    if callback_number:
        parts.append("  <Pause length=\"1\"/>")
        parts.append(
            f'  <Say voice="Polly.Aditi">Or call us on '
            f"{escape(_spell_digits(callback_number))}.</Say>"
        )
    parts.append("</Response>")
    return "\n".join(parts) + "\n"


def _spell_digits(number: str) -> str:
    """Space the digits so a text-to-speech engine reads a number digit by digit.

    Without this a number is read as one enormous integer, which is unusable to
    a caller trying to write it down.
    """
    return " ".join(ch for ch in str(number) if ch.isdigit() or ch == "+")


class VoiceWebhook:
    """The ingress decision, as an object so it can be tested without a socket."""

    def __init__(
        self,
        *,
        auth_token: str,
        public_base_url: str,
        admission=None,
        callback_number: str = "",
        store=None,
        instance_id: str = "",
        address: str = "",
        channel_kind: str = "voice_inbound",
    ) -> None:
        self.auth_token = auth_token
        self.public_base_url = public_base_url
        self.admission = admission
        self.callback_number = callback_number
        # MOD-01's channel tables. Optional so the webhook is usable without a
        # store (tests, and an instance with no database), but wired in
        # production — the two protections below are not decorations.
        self.store = store
        self.instance_id = instance_id
        self.address = address
        self.channel_kind = channel_kind

    def voice(self, *, params: dict, signature: str | None) -> tuple[int, str, str]:
        """Handle `POST /twilio/voice`. Returns ``(status, body, content_type)``.

        On refusal the body is empty (`TAC-4`) — an error page tells an attacker
        which check they failed, and tells Twilio nothing useful.
        """
        from .channels import SignatureError, validate_twilio_signature

        url = (self.public_base_url or "").rstrip("/") + "/twilio/voice"
        try:
            validate_twilio_signature(self.auth_token, url, params, signature)
        except SignatureError:
            # Deliberately not logged with the params: the body of a rejected
            # webhook is the caller's, and `REC-14`/`TAC-8` forbid putting a
            # caller's number in a log line.
            return 403, "", "text/plain"

        call_id = str(params.get("CallSid") or "")

        # Attribution: a valid signature proves Twilio sent it, not that this
        # instance owns the number it was sent to. Answering for a number we do
        # not own would make this product speak for the other one (`CG-1`,
        # `BRD-03`, edge case 14).
        endpoint = self._attribute(params)
        if endpoint is False:
            if self.store is not None:
                self.store.mark_rejected(call_id, channel_kind=self.channel_kind)
            return 403, "", "text/plain"

        # Replay: Twilio retries a webhook it believes failed. A replayed
        # CallSid must not open a second stream or start a second conversation.
        if self.store is not None and call_id:
            if self.store.claim_event(
                call_id, channel_kind=self.channel_kind,
                endpoint_id=(endpoint or {}).get("id"),
            ) == "duplicate":
                # 200, so Twilio stops retrying, and deliberately **no
                # `<Stream>`**: the first delivery already owns this call.
                return 200, f"{TwiML_HEADER}\n<Response/>\n", "application/xml"

        # Admission before TwiML. A refused call must never reach the point of
        # being handed a stream URL.
        if self.admission is not None:
            decision = self.admission.admit(call_id)
            if not decision.admitted:
                return 200, build_busy_twiml(
                    getattr(decision, "message", BUSY_MESSAGE) or BUSY_MESSAGE,
                    self.callback_number,
                ), "application/xml"

        return 200, build_stream_twiml(self._ws_url(), call_id), "application/xml"

    def _attribute(self, params: dict):
        """Which endpoint this call arrived on.

        Returns the endpoint row, or ``None`` when no store is wired (nothing to
        check against), or ``False`` when the number is not ours.
        """
        if self.store is None or not self.address:
            return None
        called = str(params.get("To") or "").strip()
        if not called:
            # No `To` at all is not evidence of a foreign number; Twilio always
            # sends it, and refusing on its absence would trade a real
            # protection for a new way to drop calls.
            return None
        if called == self.address:
            return self.store.find_endpoint(
                channel_kind=self.channel_kind,
                address=self.address,
                instance_id=self.instance_id,
            )
        return False

    def status(self, *, params: dict, signature: str | None) -> tuple[int, str, str]:
        """Handle `POST /twilio/status`.

        Twilio reports `completed`, `busy`, `no-answer` and `failed` here. The
        slot is released on any terminal status, so a call that ends without a
        clean stream close cannot leak capacity (`SM-04`) — a dropped callback
        leaves the row alone and `release_stale` reconciles it later.
        """
        from .channels import SignatureError, validate_twilio_signature

        url = (self.public_base_url or "").rstrip("/") + "/twilio/status"
        try:
            validate_twilio_signature(self.auth_token, url, params, signature)
        except SignatureError:
            return 403, "", "text/plain"

        call_id = str(params.get("CallSid") or "")
        if self.admission is not None and call_id:
            self.admission.release(call_id)

        return 200, "", "text/plain"

    def _ws_url(self) -> str:
        return websocket_url(self.public_base_url)
