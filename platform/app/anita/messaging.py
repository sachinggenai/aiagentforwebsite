"""Messaging ingress: WhatsApp in, a rendered reply out through the outbox (US-002).

The gap this closes is exact. `channels.py` has carried a `WhatsAppAdapter`
since MOD-01 was written, and it was **reachable by nothing**: no webhook route
was served, and `ChannelGateway.accept()` had zero runtime callers. An inbound
WhatsApp message had nowhere to land.

The order of decisions mirrors `VoiceWebhook`, because the failure modes are
the same ones:

1. **Signature** — validated against the configured public URL before the body
   is trusted for anything (`TAC-3`). Failure is 403 with an empty body.
2. **Attribution** — a valid signature proves Twilio sent it, not that this
   instance owns the number it was addressed to (`CG-1`).
3. **Replay** — `MessageSid` is the dedupe key. Twilio redelivers a webhook it
   believes failed; ten deliveries of one message must produce one reply and
   one turn (`AC-4`, `TAC-5`). The claim is the primary key, so the guarantee
   is the database's.
4. **Content taxonomy** — a voice note is declined with a text prompt (`AC-3`),
   an unsupported file is explained rather than ignored (`AC-8`), media with no
   caption asks for context (edge case 2).
5. **Answer** — the *same* engine the web chat runs, via an injected callable.
6. **Send** — rendered by the originating adapter, written to `outbound_send`
   **before** the carrier is called, then dispatched. A carrier failure leaves
   the row queued for the worker, so the buyer is never silently unanswered
   (`AC-5`).

**What is deliberately synchronous.** The answer is composed on the webhook
thread, which means the ack is not the story's 150 ms on a slow turn. The
alternative — enqueue an answer *job* and ack immediately — buys latency the
carrier does not require (Twilio's webhook budget is ~15 s) and costs a second
kind of queue. The measured time is recorded instead, and the retry/failure
path — the part that actually protects the buyer — is the outbox's.
"""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger("anita.messaging")

# Said when a voice note arrives. `CG-8`: the channel cannot carry it, and
# silence is not a reply.
VOICE_NOTE_DECLINE = (
    "I can't listen to voice notes yet — please type your question here, "
    "or give me a call and I'll answer on the phone."
)

# Unsupported file type (`AC-8`). Names what arrived so the buyer knows the
# message was read, not dropped.
UNSUPPORTED_MEDIA = (
    "I can't open {kind} files yet. Please type your question and I'll answer "
    "here — or call us if it's easier."
)

# Media with no caption (edge case 2). Asked, not guessed.
MEDIA_WITHOUT_CAPTION = (
    "Thanks — I've got your file. Tell me in a line what you need and I'll answer."
)

# A message with no text and no media: almost always a mis-send.
EMPTY_MESSAGE = (
    "I didn't get any text with that. Please type your question and I'll answer."
)

# Media types the channel genuinely handles today: images and PDFs are stored
# and their caption answered. Everything else is named back to the buyer.
SUPPORTED_MEDIA_PREFIXES = ("image/",)
SUPPORTED_MEDIA_TYPES = ("application/pdf",)

# A polite refusal when a sender is over their abuse allowance. Sent *instead
# of* the answer so the buyer learns why the answers stopped.
RATE_LIMITED_REPLY = (
    "That's a lot of messages at once — please give me a moment and try again."
)


def classify_content(params: dict) -> tuple[str, str, tuple[str, ...]]:
    """What kind of inbound this is: ``(kind, text, media_types)``.

    Kinds: ``text`` · ``voice_note`` · ``media`` · ``empty``. Kept as a free
    function because the edge taxonomy is the part worth testing directly, and
    a table of fixtures pinned against a method would need the whole webhook
    constructed to ask about one message.
    """
    text = str(params.get("Body") or "").strip()
    types = tuple(
        str(params.get(f"MediaContentType{i}") or "")
        for i in range(int(params.get("NumMedia") or 0))
    )
    types = tuple(t for t in types if t)

    if types and any(t.startswith("audio/") for t in types):
        return "voice_note", text, types
    if types:
        return "media", text, types
    if text:
        return "text", text, ()
    return "empty", text, ()


def media_supported(media_types: tuple[str, ...]) -> bool:
    return all(
        t.startswith(SUPPORTED_MEDIA_PREFIXES) or t in SUPPORTED_MEDIA_TYPES
        for t in media_types
    )


def unsupported_kind(media_types: tuple[str, ...]) -> str:
    """The buyer-facing name of an unsupported type. Extension when there is
    one, the MIME subtype otherwise — "I can't open .zip files" is a sentence a
    buyer understands; "application/octet-stream" is not."""
    for media_type in media_types:
        if media_type.startswith(SUPPORTED_MEDIA_PREFIXES) or media_type in SUPPORTED_MEDIA_TYPES:
            continue
        subtype = media_type.split("/", 1)[-1].split(";", 1)[0].strip()
        return f".{subtype}" if subtype and "/" not in subtype else media_type
    return "those"


class TwilioMessagesClient:
    """Sends WhatsApp/SMS via Twilio's Messages API.

    Constructed only when the credentials exist; the outbox is where a failure
    goes, so this class only has to classify it. `transient` follows the rule
    the retry policy needs: 5xx, 429 and transport errors are worth another
    attempt; every other 4xx is the provider saying the send can never succeed
    — an undeliverable number is not a temporary condition (`T-26`).
    """

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        from_number: str,
        base_url: str = "https://api.twilio.com",
        timeout: float = 10.0,
    ) -> None:
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.account_sid and self.auth_token and self.from_number)

    def send(self, payload: dict) -> str:
        """Dispatch one rendered send. Returns the provider message id."""
        from .outbox import SendFailed

        if not self.configured:
            raise SendFailed(
                "Twilio messaging credentials are not configured", transient=True
            )

        import httpx

        to = str(payload.get("to") or "")
        body = str(payload.get("body") or "")
        channel = str(payload.get("channel") or "whatsapp")
        prefix = "whatsapp:" if channel == "whatsapp" else ""

        url = f"{self.base_url}/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        try:
            response = httpx.post(
                url,
                auth=(self.account_sid, self.auth_token),
                data={
                    "From": f"{prefix}{self.from_number}",
                    "To": f"{prefix}{to}",
                    "Body": body,
                },
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - transport failure is transient
            raise SendFailed(f"carrier unreachable: {type(exc).__name__}", transient=True) from exc

        if response.status_code >= 500 or response.status_code == 429:
            raise SendFailed(
                f"carrier returned {response.status_code}", transient=True,
                status=response.status_code,
            )
        if response.status_code >= 400:
            # The body of a 4xx names the reason (undeliverable, blocked,
            # invalid number). Kept short and never the buyer's number.
            detail = _safe_error(response)
            raise SendFailed(
                f"carrier refused ({response.status_code}): {detail}",
                transient=False, status=response.status_code,
            )
        try:
            return str(response.json().get("sid") or "")
        except Exception:  # noqa: BLE001 - a 2xx without JSON is still a send
            return ""


def _safe_error(response) -> str:
    """The provider's error message, truncated. Twilio echoes the recipient in
    some error strings; the console shows this, so it is kept to the *message*
    field and never the request echo."""
    try:
        payload = response.json()
        return str(payload.get("message") or "")[:200]
    except Exception:  # noqa: BLE001
        return ""


class MessagingWebhook:
    """Inbound messaging as a testable object — no socket, no carrier."""

    def __init__(
        self,
        *,
        auth_token: str,
        public_base_url: str,
        answer: Callable[..., str],
        gateway,
        outbox=None,
        sender=None,
        store=None,
        instance_id: str = "",
        address: str = "",
        channel_kind: str = "whatsapp",
        callback_number: str = "",
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.auth_token = auth_token
        self.public_base_url = (public_base_url or "").rstrip("/")
        self.answer = answer
        self.gateway = gateway
        self.outbox = outbox
        self.sender = sender
        self.store = store
        self.instance_id = instance_id
        self.address = address
        self.channel_kind = channel_kind
        self.callback_number = callback_number
        self._on_event = on_event

    # -- ingress -----------------------------------------------------------
    def whatsapp(self, *, params: dict, signature: str | None) -> tuple[int, str, str]:
        """Handle `POST /twilio/whatsapp`. Returns ``(status, body, content_type)``."""
        from .channels import Channel, RateLimited, SignatureError, validate_twilio_signature

        message_id = str(params.get("MessageSid") or "")
        url = f"{self.public_base_url}/twilio/whatsapp"

        try:
            validate_twilio_signature(self.auth_token, url, params, signature)
        except SignatureError:
            self._reject("signature")
            return 403, "", "text/plain"

        if not self._attributed(params):
            if self.store is not None and message_id:
                self.store.mark_rejected(message_id, channel_kind=self.channel_kind)
            self._reject("attribution")
            return 403, "", "text/plain"

        # Replay before any work: a redelivery is acknowledged and dropped. No
        # reply, no turn, no lead (`AC-4`, `T-21`).
        if self.store is not None and message_id:
            if self.store.claim_event(
                message_id, channel_kind=self.channel_kind
            ) == "duplicate":
                self._emit("messaging.duplicate", {"channel": self.channel_kind})
                return 200, "", "text/plain"

        try:
            envelope = self.gateway.accept(
                Channel.WHATSAPP, params, verified=True
            )
        except RateLimited:
            # The buyer is over their allowance. One polite send, once — the
            # limiter is what stops the next hundred.
            self._reject("rate_limit")
            self._dispatch_reply(
                self._render(RATE_LIMITED_REPLY, params), params
            )
            return 200, "", "text/plain"
        except Exception as exc:  # noqa: BLE001 - a malformed payload is a bad request
            logger.warning("whatsapp: unparseable inbound (%s)", type(exc).__name__)
            return 400, "", "text/plain"

        reply = self._compose(envelope, params)
        self._dispatch_reply(self._render(reply, params), params)

        # A messaging webhook answers empty: the reply travels through the
        # Messages API, not as TwiML. The 200 is what stops Twilio retrying.
        return 200, "", "text/plain"

    # -- the reply ---------------------------------------------------------
    def _compose(self, envelope, params: dict) -> str:
        """The buyer-facing text for this message."""
        kind, text, media_types = classify_content(params)

        if kind == "voice_note":
            self._emit("messaging.voice_note_declined", {"channel": self.channel_kind})
            return VOICE_NOTE_DECLINE
        if kind == "media":
            if not media_supported(media_types):
                return UNSUPPORTED_MEDIA.format(kind=unsupported_kind(media_types))
            if not text:
                return MEDIA_WITHOUT_CAPTION
            # Supported media with a caption: answer the caption; the files
            # themselves stay on the conversation as `media_urls` (T-16).
        if kind == "empty":
            return EMPTY_MESSAGE

        try:
            answer = self.answer(text, session_id=envelope.key)
        except Exception:  # noqa: BLE001 - an engine failure still owes the buyer a sentence
            logger.exception("whatsapp: the answer path failed")
            answer = (
                "Something went wrong on our side with that question. "
                "Please try again in a moment."
            )
        return str(answer or "").strip() or EMPTY_MESSAGE

    def _render(self, text: str, params: dict) -> dict:
        """Render through the originating adapter — one shape per channel."""
        from .channels import Channel, InboundEnvelope

        envelope = InboundEnvelope(
            channel=Channel.WHATSAPP,
            text="",
            identity=self._sender(params),
            session_id=self._sender(params),
            raw=params,
        )
        rendered = self.gateway.render(text, envelope)
        return {
            "to": rendered.get("to") or self._sender(params),
            "body": rendered.get("body") or text,
            "channel": Channel.WHATSAPP.value,
        }

    # -- outbound ----------------------------------------------------------
    def _dispatch_reply(self, payload: dict, params: dict) -> str:
        """Enqueue, then attempt once (`outbox.dispatch_now`)."""
        if self.outbox is None:
            # No store: best-effort only, and the failure is loud in the log
            # rather than silent at the buyer.
            if self.sender is not None:
                try:
                    self.sender.send(payload)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("whatsapp: unqueued send failed: %s", type(exc).__name__)
            return ""

        from .outbox import dispatch_now

        return dispatch_now(
            self.outbox, self.sender,
            channel_kind=self.channel_kind,
            payload=payload,
            conversation_id=self._sender(params),
        )

    # -- helpers -----------------------------------------------------------
    def _sender(self, params: dict) -> str:
        sender = str(params.get("From") or "")
        return sender.split(":", 1)[1] if ":" in sender else sender

    def _attributed(self, params: dict) -> bool:
        """Whether the message was addressed to this instance's number."""
        if not self.address:
            return True
        addressed = str(params.get("To") or "")
        bare = addressed.split(":", 1)[1] if ":" in addressed else addressed
        return not bare or bare == self.address

    def _reject(self, reason: str) -> None:
        """Count a refusal by class only — never the signature or the body."""
        self._emit("messaging.rejected", {"reason": reason})
        if self.store is not None:
            try:
                self.store.record_rejection(route="/twilio/whatsapp", reason=reason)
            except Exception:  # noqa: BLE001 - counting never fails a refusal
                pass

    def _emit(self, event: str, payload: dict) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event, payload)
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "EMPTY_MESSAGE", "MEDIA_WITHOUT_CAPTION", "MessagingWebhook",
    "SUPPORTED_MEDIA_PREFIXES", "SUPPORTED_MEDIA_TYPES", "TwilioMessagesClient",
    "UNSUPPORTED_MEDIA", "VOICE_NOTE_DECLINE", "classify_content",
    "media_supported", "unsupported_kind",
]
