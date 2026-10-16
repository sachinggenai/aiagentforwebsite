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

import logging
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

logger = logging.getLogger("anita.voice_webhook")

TwiML_HEADER = '<?xml version="1.0" encoding="UTF-8"?>'
MEDIA_STREAM_PATH = "/twilio/media-stream"

#: Where Twilio fetches the TwiML for a call *we* placed. Shared with
#: `scripts/demo_call.py` rather than spelled twice: the dialler sets this URL
#: on the call and the webhook validates the signature against it, so two
#: copies that drifted would refuse every call the platform made.
OUTBOUND_PATH = "/twilio/outbound"

#: The configuration key holding the dialable numbers. Named once because three
#: things read it — the assembly, the file parser, and the dialler that edits
#: it — and a typo in any one of them would silently disable the guard.
ALLOWLIST_KEY = "ANITA_DEMO_CALL_ALLOWLIST"

#: The channel kind an outbound stream is recorded under. Kept as a value here
#: and asserted against `Channel.VOICE_OUTBOUND` in the tests, because two
#: spellings of one channel name is how a console starts labelling calls with a
#: kind nothing else recognises.
VOICE_OUTBOUND_CHANNEL = "voice_outbound"

#: The kind an inbound stream is recorded under, unchanged from before outbound
#: existed — and the relay's default, which is why the inbound TwiML does not
#: have to name it.
VOICE_INBOUND_CHANNEL = "voice_inbound"

#: The voice Twilio says the outbound greeting with. Polly rather than the
#: local TTS stack, for the same reason the busy message uses it: this is the
#: line that has to work *before* our engine is on the call at all.
SAY_VOICE = "Polly.Aditi"

#: What an outbound call opens with.
#:
#: There is no spoken greeting anywhere else in the product — an inbound caller
#: is expected to speak first, and does. An outbound callee who answers to
#: silence hangs up, so this direction needs one. It names the agent and the
#: company because an outbound call that does not say who is calling is the one
#: kind of call nobody wants to receive.
GREETING_TEMPLATE = (
    "Hello, this is {agent} from {company}. "
    "I am calling about your enquiry. How can I help?"
)


def greeting_text(agent: str, company: str) -> str:
    """The opening line, from configured identity rather than a literal.

    Trailing full stops are stripped before the template re-punctuates: the
    configured company name ends in `LTD.`, and `LTD.. I am calling` is read
    aloud by a speech engine as a stumble.
    """
    name = (agent or "the product assistant").strip().rstrip(".") or "the product assistant"
    organisation = (company or "").strip().rstrip(".") or "our team"
    return GREETING_TEMPLATE.format(agent=name, company=organisation)


def normalise_number(number: str) -> str:
    """Reduce a dialled number to its digits and a leading plus.

    Formatting is not a security boundary, but it is a source of false
    negatives: a configured list holding `+91 99999 00000` and a webhook
    delivering `+919999900000` are the same number, and a check that refused
    one for being typed differently would look like a broken dialler.
    """
    return "".join(ch for ch in str(number or "").strip() if ch.isdigit() or ch == "+")


class CallAllowList:
    """The numbers this instance may dial, and nothing else.

    Outbound is the one direction where the platform chooses who to contact,
    which makes it the one direction that needs a list rather than a rule. The
    check lives here — in the product — and not only in whatever places the
    call: a guard that exists solely in a script is one wrong command line away
    from dialling a stranger, and Twilio's outbound webhook carries the
    destination the call was placed to, so the decision can be made again, by
    us, at the moment the callee picks up.

    Empty by default, and **an empty list allows nothing**. Unset means
    unconfigured, and unconfigured must not quietly mean unrestricted.
    """

    def __init__(self, numbers=()) -> None:
        self._numbers = frozenset(
            value for value in (normalise_number(n) for n in numbers) if value
        )

    @classmethod
    def from_value(cls, raw: str) -> "CallAllowList":
        """Parse the comma-separated configuration value."""
        return cls(part for part in str(raw or "").split(",") if part.strip())

    @classmethod
    def from_file(cls, path) -> "CallAllowList":
        """Read the list from a dotenv-style file.

        The file rather than the value read at boot, because the dialler edits
        this list between calls: a guard holding its boot snapshot would refuse
        a number the operator had just added, and that presents as "the dialler
        is broken" rather than as a stale cache. A missing or unreadable file is
        an empty list, which allows nothing.
        """
        try:
            text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return cls()
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, raw_value = stripped.partition("=")
            if key.strip() == ALLOWLIST_KEY:
                return cls.from_value(raw_value.strip().strip('"').strip("'"))
        return cls()

    def allows(self, number: str) -> bool:
        return normalise_number(number) in self._numbers

    def __bool__(self) -> bool:
        return bool(self._numbers)

    def __len__(self) -> int:
        return len(self._numbers)

# Said when capacity is full. Deliberately not an apology loop: it states what
# happened, offers the callback, and gets off the line. `AC-4` requires the
# caller hears *something* — never silence.
BUSY_MESSAGE = (
    "Sorry, all our lines are busy right now. "
    "Please leave it a moment and call back, or ask us for a callback on the website."
)

#: Twilio `CallStatus` values that mean the call is over. The slot is released
#: on these and on nothing else (§8.32): a number configured with progress
#: callbacks also posts `ringing`/`answered`, and releasing there would free the
#: slot of a call that is still up.
TERMINAL_CALL_STATUSES = frozenset({
    "completed", "busy", "no-answer", "failed", "canceled",
})

#: The statuses that describe a call still in progress. Kept as a set of its own
#: so an unrecognised value — a Twilio addition, or a body that is not a status
#: callback at all — is warned about rather than silently treated as one.
CALL_PROGRESS_STATUSES = frozenset({
    "queued", "initiated", "ringing", "in-progress", "answered",
})


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


def _stream_parameters(call_id: str, token: str = "", kind: str = "") -> str:
    """The `<Parameter>`s the relay reads out of the stream's `start` event."""
    parameters = f'      <Parameter name="call_id" value={quoteattr(call_id)}/>\n'
    if token:
        parameters += f'      <Parameter name="token" value={quoteattr(token)}/>\n'
    if kind:
        parameters += f'      <Parameter name="kind" value={quoteattr(kind)}/>\n'
    return parameters


def _connect_block(ws_url: str, parameters: str) -> str:
    return (
        "  <Connect>\n"
        f"    <Stream url={quoteattr(ws_url)}>\n"
        f"{parameters}"
        "    </Stream>\n"
        "  </Connect>\n"
    )


def build_stream_twiml(ws_url: str, call_id: str, token: str = "", kind: str = "") -> str:
    """TwiML that hands the call to our media stream.

    `call_id` is passed as a `<Parameter>` so the WebSocket handler learns which
    call it is holding from the stream itself, rather than correlating against
    some other table it would have to keep in step.

    `token` is the signed proof that this call was admitted. The media-stream
    socket is public, and a `call_id` alone is a value anyone can guess or
    read out of a log; the token binds the stream to a webhook run that
    actually took an admission slot (`TAC-5`, `TAC-7`).

    `kind` names the call direction for the relay, which would otherwise record
    every stream it runs as an inbound one. **The inbound path omits it on
    purpose**: the relay defaults to `voice_inbound`, so the verified inbound
    TwiML is byte-for-byte what it was before outbound existed, and only the
    direction that needs distinguishing pays for it.
    """
    return (
        f"{TwiML_HEADER}\n"
        "<Response>\n"
        f"{_connect_block(ws_url, _stream_parameters(call_id, token, kind))}"
        "</Response>\n"
    )


def build_outbound_twiml(
    ws_url: str,
    call_id: str,
    token: str = "",
    greeting: str = "",
    voice: str = SAY_VOICE,
) -> str:
    """TwiML for a call this instance placed, fetched when the callee answers.

    `<Say>` before `<Connect>`, and that order is the entire reason this builder
    exists separately. The media stream carries the *answer* but never the
    introduction, so without a line here the person we rang picks up to
    silence — and the only reasonable response to a silent caller is to hang
    up.
    """
    say = f'  <Say voice="{voice}">{escape(greeting)}</Say>\n' if greeting else ""
    return (
        f"{TwiML_HEADER}\n"
        "<Response>\n"
        f"{say}"
        f"{_connect_block(ws_url, _stream_parameters(call_id, token, VOICE_OUTBOUND_CHANNEL))}"
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
        channel_kind: str = VOICE_INBOUND_CHANNEL,
        stream_tokens=None,
        allowlist=None,
        allowlist_provider=None,
        greeting: str = "",
    ) -> None:
        self.auth_token = auth_token
        self.public_base_url = public_base_url
        # Who we are allowed to ring. Defaults to an empty list, which permits
        # nothing — an unconfigured instance must not be a dialler that will
        # call anywhere.
        #
        # `allowlist` is the value read at boot and is the fallback;
        # `allowlist_provider` is a callable that re-reads the list, and
        # production wires one so that adding a number to the dialler takes
        # effect on the next call rather than on the next restart.
        self.allowlist = allowlist if allowlist is not None else CallAllowList()
        self.allowlist_provider = allowlist_provider
        # Said before the media stream connects on a call we placed. Empty is
        # legitimate (the stream still answers); it is just worse.
        self.greeting = greeting
        # Mints the token that proves a media stream belongs to an admitted
        # call. Optional so the webhook works without one (tests, and a
        # process that never mints); when absent, no token is emitted and the
        # relay must not require one — both sides read the same object.
        self.stream_tokens = stream_tokens
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

        return 200, build_stream_twiml(self._ws_url(), call_id, self._stream_token(call_id)), "application/xml"

    def outbound(self, *, params: dict, signature: str | None) -> tuple[int, str, str]:
        """Handle `POST /twilio/outbound` — the TwiML for a call *we* placed.

        Twilio fetches this when the person we dialled picks up, which makes it
        the last moment before our engine is on the line, and therefore the
        last moment at which to decide it should not be. The same three checks
        as inbound, plus one, in the order that keeps a failure cheap:

        1. **the signature**, against this route's own public URL;
        2. **the allowlist** — the destination must be one we chose to dial.
           Checked here as well as wherever the call was placed, because this
           is the check that cannot be bypassed by whatever placed it;
        3. **attribution on `From`**, since our number is the one that called;
        4. replay, admission, then `<Say>` and `<Connect>`.

        A refusal is 403 with an empty body, matching the inbound convention: an
        error page tells an attacker which check they failed and tells Twilio
        nothing it can use.
        """
        from .channels import SignatureError, validate_twilio_signature

        url = (self.public_base_url or "").rstrip("/") + OUTBOUND_PATH
        try:
            validate_twilio_signature(self.auth_token, url, params, signature)
        except SignatureError:
            return 403, "", "text/plain"

        call_id = str(params.get("CallSid") or "")

        # The destination is not logged, and neither is the caller: it is
        # someone's number either way, and `REC-14`/`TAC-8` keep numbers out of
        # log lines whether the call arrived or was placed.
        if not self._current_allowlist().allows(str(params.get("To") or "")):
            logger.warning(
                "outbound: destination is not on the call allowlist — refusing "
                "to connect the assistant"
            )
            if self.store is not None and call_id:
                self.store.mark_rejected(call_id, channel_kind=VOICE_OUTBOUND_CHANNEL)
            return 403, "", "text/plain"

        endpoint = self._attribute(params, on="From")
        if endpoint is False:
            if self.store is not None and call_id:
                self.store.mark_rejected(call_id, channel_kind=VOICE_OUTBOUND_CHANNEL)
            return 403, "", "text/plain"

        if self.store is not None and call_id:
            if self.store.claim_event(
                call_id, channel_kind=VOICE_OUTBOUND_CHANNEL,
                endpoint_id=(endpoint or {}).get("id"),
            ) == "duplicate":
                return 200, f"{TwiML_HEADER}\n<Response/>\n", "application/xml"

        if self.admission is not None:
            decision = self.admission.admit(call_id)
            if not decision.admitted:
                return 200, build_busy_twiml(
                    getattr(decision, "message", BUSY_MESSAGE) or BUSY_MESSAGE,
                    self.callback_number,
                ), "application/xml"

        return 200, build_outbound_twiml(
            self._ws_url(), call_id, self._stream_token(call_id), self.greeting
        ), "application/xml"

    def _current_allowlist(self) -> CallAllowList:
        """The dialable numbers as they stand now, never as they stood at boot.

        A failure to read the list is a **refusal**, not a pass. The one
        outcome this check must never produce is connecting the assistant to a
        number nobody approved because the file could not be parsed.
        """
        if self.allowlist_provider is None:
            return self.allowlist
        try:
            return self.allowlist_provider()
        except Exception:  # noqa: BLE001 - unreadable list means no call
            logger.exception("outbound: could not read the call allowlist — refusing")
            return CallAllowList()

    def _stream_token(self, call_id: str) -> str:
        """The proof the relay requires before it will run this call's stream.

        Minted here, at the one moment the call is known to be admitted, and
        verified on the socket. Without it a public `wss://` connection naming
        any `call_id` would drive the engine and, at teardown, release a slot it
        never held — which is how the admission ceiling was bypassed (§8.6).
        """
        if self.stream_tokens is None or not call_id:
            return ""
        try:
            return self.stream_tokens.mint(call_id)
        except Exception:  # noqa: BLE001 - a token failure must not drop a call
            logger.exception("voice webhook: could not mint a stream token")
            return ""

    def _attribute(self, params: dict, on: str = "To"):
        """Which endpoint this call belongs to.

        Returns the endpoint row, or ``None`` when no store is wired (nothing to
        check against), or ``False`` when the number is not ours.

        `on` names the field carrying **our** number, and it differs by
        direction: `To` for a call that arrived, `From` for one we placed,
        because on an outbound call `To` is the person we rang. The mistake is
        not symmetrical — attributing an outbound webhook on `To` compares our
        number against the callee's and refuses every call we make, which would
        present as "outbound silently does not work".
        """
        if self.store is None or not self.address:
            return None
        ours = str(params.get(on) or "").strip()
        if not ours:
            # No number at all is not evidence of a foreign one; Twilio always
            # sends it, and refusing on its absence would trade a real
            # protection for a new way to drop calls.
            return None
        if ours == self.address:
            return self.store.find_endpoint(
                channel_kind=self.channel_kind,
                address=self.address,
                instance_id=self.instance_id,
            )
        return False

    def status(self, *, params: dict, signature: str | None) -> tuple[int, str, str]:
        """Handle `POST /twilio/status`.

        Twilio reports `completed`, `busy`, `no-answer` and `failed` here, but
        the same URL also carries progress callbacks when a number is configured
        with them. **The slot is released only on a terminal status** (§8.32) —
        releasing on `ringing` or `answered` would free the slot of a call that
        is still up, and the ceiling would quietly stop holding. A call that ends
        without any callback is still reconciled by `release_stale` (`SM-04`).
        """
        from .channels import SignatureError, validate_twilio_signature

        url = (self.public_base_url or "").rstrip("/") + "/twilio/status"
        try:
            validate_twilio_signature(self.auth_token, url, params, signature)
        except SignatureError:
            return 403, "", "text/plain"

        call_id = str(params.get("CallSid") or "")
        if self.admission is not None and call_id:
            status = str(params.get("CallStatus") or "").strip().lower()
            if status in TERMINAL_CALL_STATUSES:
                self.admission.release(call_id)
            elif status in CALL_PROGRESS_STATUSES:
                logger.debug("call status %s is not terminal — the slot is kept", status)
            else:
                # Neither terminal nor a status we know: an absent `CallStatus`
                # means this is not a status callback at all, and a value we do
                # not recognise is one Twilio has added. Keeping the slot is the
                # recoverable error — `release_stale` sweeps it — while freeing a
                # live call's slot is not.
                logger.warning(
                    "call status %r is not terminal — the slot is kept; "
                    "release_stale will reconcile it if the call has ended",
                    status or "(absent)",
                )

        return 200, "", "text/plain"

    def _ws_url(self) -> str:
        return websocket_url(self.public_base_url)
