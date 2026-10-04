"""Channel gateway — the edge (MOD-01, TRD-01 … TRD-06).

Every channel normalises into one :class:`InboundEnvelope` and renders replies
in its own native form. This is the module decision **D1** grows, and the reason
it exists separately: the source platform fused five channels into one
4,133-line file, so adding a channel meant editing every other channel's hot
path.

Two security controls live here because this is the only place they *can* live
without leaking into the answer engine (`DG-06`, `CV-03`):

* **Twilio signature validation** — the source platform had none. Without it,
  anyone who knows the webhook URL can post fake inbound messages and place
  calls. Buyer surfaces stay open to buyers; the *webhook* is authenticated.
* **Per-session rate limiting** — bounds abuse without adding buyer friction.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Protocol, Sequence
from urllib.parse import urlparse


class Channel(str, Enum):
    WEB_CHAT = "web_chat"
    WHATSAPP = "whatsapp"
    VOICE_INBOUND = "voice_inbound"
    VOICE_OUTBOUND = "voice_outbound"
    BROWSER_MIC = "browser_mic"
    EXTRA = "extra"           # the configurable adapter (D1)


# What each channel can actually do. Declared rather than assumed: a channel
# that cannot carry audio must refuse an audio request with an explanation, not
# silently behave differently from the others.
CAPABILITIES: dict[Channel, set[str]] = {
    Channel.WEB_CHAT: {"text", "rich_text"},
    Channel.WHATSAPP: {"text", "media_in"},
    Channel.VOICE_INBOUND: {"audio", "text"},
    Channel.VOICE_OUTBOUND: {"audio", "text"},
    Channel.BROWSER_MIC: {"audio", "text"},
    Channel.EXTRA: {"text"},
}

CHANNEL_ALIASES: dict[str, Channel] = {
    "twilio_voice": Channel.VOICE_INBOUND,
    "twilio_whatsapp": Channel.WHATSAPP,
    "streamlit": Channel.WEB_CHAT,
    "web": Channel.WEB_CHAT,
    "mic": Channel.BROWSER_MIC,
}


class ChannelError(ValueError):
    """The channel cannot carry what was asked of it."""


class SignatureError(ValueError):
    """A webhook signature did not validate."""


class RateLimited(RuntimeError):
    """The caller exceeded its allowance."""


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InboundEnvelope:
    """One inbound message, normalised across every channel.

    ``identity`` is what makes cross-channel continuity possible (`BRD-02`) —
    and per `CV-02` it is present only where it genuinely exists. A web session
    with no phone number has a session identity, not a buyer identity, and the
    orchestrator must not pretend otherwise.
    """

    channel: Channel
    text: str = ""
    # Phone number in E.164, WhatsApp handle, or session token. None when the
    # caller is genuinely anonymous.
    identity: str | None = None
    session_id: str | None = None
    media_urls: tuple[str, ...] = ()
    is_audio: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def has_identity(self) -> bool:
        return bool(self.identity)

    @property
    def key(self) -> str:
        """The key a conversation is grouped under.

        Identity when there is one, session otherwise — so an anonymous web
        visitor still gets a coherent conversation rather than a new one per
        message.
        """
        return self.identity or self.session_id or "anonymous"

    def supports(self, capability: str) -> bool:
        return capability in CAPABILITIES.get(self.channel, set())

    def require(self, capability: str) -> None:
        if not self.supports(capability):
            raise ChannelError(
                f"channel {self.channel.value!r} cannot carry {capability!r} "
                f"(supports: {sorted(CAPABILITIES.get(self.channel, set()))})"
            )


# ---------------------------------------------------------------------------
# Twilio signature validation (TRD-06)
# ---------------------------------------------------------------------------
def validate_twilio_signature(
    auth_token: str,
    url: str,
    params: dict[str, str],
    signature: str | None,
) -> None:
    """Verify an ``X-Twilio-Signature`` header, per Twilio's documented scheme.

    Twilio signs ``url + sorted(key + value)`` with HMAC-SHA1 over the auth
    token. The source platform performed **no** validation at all, which meant
    any POST to the webhook URL was treated as a real inbound message.

    Raises :class:`SignatureError` on any mismatch. Comparison is
    constant-time: a timing-variable compare on a signature check is a real
    (if unfashionable) attack.
    """
    if not auth_token:
        raise SignatureError("no auth token configured; cannot validate signature")
    if not signature:
        raise SignatureError("missing X-Twilio-Signature header")

    payload = url
    for key in sorted(params):
        payload += key + str(params[key])

    expected = hmac.new(
        auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1
    ).digest()

    import base64

    candidate = base64.b64encode(expected).decode("ascii")
    if not hmac.compare_digest(candidate, signature):
        raise SignatureError("signature does not match")


def signed_payload(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Compute a signature. Used by tests and by local webhook simulators."""
    import base64

    payload = url + "".join(key + str(params[key]) for key in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


# ---------------------------------------------------------------------------
# Rate limiting (DG-06)
# ---------------------------------------------------------------------------
class RateLimiter:
    """Fixed-window limiter keyed by session or identity.

    Deliberately simple: at this volume a token bucket's smoothness buys
    nothing, and a fixed window is far easier to reason about during an
    incident — "how many has this number sent in the last minute" has an exact
    answer.
    """

    def __init__(self, limit: int = 30, window_s: float = 60.0) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str, *, now: float | None = None) -> None:
        """Raise :class:`RateLimited` when the key is over its allowance."""
        moment = now if now is not None else time.monotonic()
        recent = [hit for hit in self._hits.get(key, []) if moment - hit < self.window_s]
        if len(recent) >= self.limit:
            self._hits[key] = recent
            raise RateLimited(
                f"{key} exceeded {self.limit} requests in {self.window_s:.0f}s"
            )
        recent.append(moment)
        self._hits[key] = recent

    def remaining(self, key: str, *, now: float | None = None) -> int:
        moment = now if now is not None else time.monotonic()
        recent = [hit for hit in self._hits.get(key, []) if moment - hit < self.window_s]
        return max(0, self.limit - len(recent))

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------
class ChannelAdapter(Protocol):
    """Turns a provider-specific payload into an envelope and back."""

    channel: Channel

    def parse(self, payload: dict) -> InboundEnvelope: ...

    def render(self, text: str, envelope: InboundEnvelope) -> dict: ...


@dataclass
class WebChatAdapter:
    """Web and mobile-web chat: JSON in, JSON out."""

    channel: Channel = Channel.WEB_CHAT

    def parse(self, payload: dict) -> InboundEnvelope:
        return InboundEnvelope(
            channel=self.channel,
            text=(payload.get("message") or payload.get("text") or "").strip(),
            identity=payload.get("phone"),
            session_id=payload.get("session_id"),
            raw=payload,
        )

    def render(self, text: str, envelope: InboundEnvelope) -> dict:
        return {"type": "message", "text": text, "session_id": envelope.session_id}


@dataclass
class WhatsAppAdapter:
    """Twilio WhatsApp: form-encoded in, message out."""

    channel: Channel = Channel.WHATSAPP

    def parse(self, payload: dict) -> InboundEnvelope:
        sender = str(payload.get("From") or "")
        # Twilio prefixes the channel: "whatsapp:+9198...". The bare number is
        # what groups conversations across channels.
        number = sender.split(":", 1)[1] if ":" in sender else sender
        media = tuple(
            payload.get(f"MediaUrl{i}") for i in range(int(payload.get("NumMedia") or 0))
            if payload.get(f"MediaUrl{i}")
        )
        return InboundEnvelope(
            channel=self.channel,
            text=(payload.get("Body") or "").strip(),
            identity=number or None,
            session_id=number or None,
            media_urls=media,
            raw=payload,
        )

    def render(self, text: str, envelope: InboundEnvelope) -> dict:
        return {"type": "whatsapp", "body": text, "to": envelope.identity}


@dataclass
class VoiceAdapter:
    """Twilio voice: media streams carry audio, callbacks carry status."""

    channel: Channel = Channel.VOICE_INBOUND

    def parse(self, payload: dict) -> InboundEnvelope:
        return InboundEnvelope(
            channel=self.channel,
            text=(payload.get("SpeechResult") or payload.get("text") or "").strip(),
            identity=payload.get("From") or payload.get("phone"),
            session_id=payload.get("stream_sid") or payload.get("CallSid"),
            is_audio=bool(payload.get("stream_sid")),
            raw=payload,
        )

    def render(self, text: str, envelope: InboundEnvelope) -> dict:
        # TwiML. The source platform's templates are reused in shape; only the
        # binding to the ANITA number differs (D3).
        return {
            "type": "twiml",
            "xml": f"<?xml version='1.0' encoding='UTF-8'?><Response><Say>{text}</Say></Response>",
        }


class ChannelGateway:
    """Normalises inbound traffic and renders replies (MOD-01).

    Adapters are registered by channel, so **a new channel is a new adapter and
    nothing else changes** — the property D1 requires and the source platform's
    single-file design could not provide.
    """

    def __init__(
        self,
        adapters: Sequence[ChannelAdapter] = (),
        *,
        rate_limiter: RateLimiter | None = None,
        twilio_auth_token: str = "",
        public_base_url: str = "",
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._adapters: dict[Channel, ChannelAdapter] = {}
        for adapter in adapters:
            self.register(adapter)
        self.rate_limiter = rate_limiter or RateLimiter()
        self.twilio_auth_token = twilio_auth_token
        self.public_base_url = public_base_url.rstrip("/")
        self._on_event = on_event

    def register(self, adapter: ChannelAdapter) -> None:
        self._adapters[adapter.channel] = adapter

    def channels(self) -> list[str]:
        return sorted(channel.value for channel in self._adapters)

    def _emit(self, event: str, payload: dict) -> None:
        # Best-effort emission, never on the critical path (MOD-08's contract).
        if self._on_event:
            try:
                self._on_event(event, payload)
            except Exception:  # noqa: BLE001 - telemetry must never fail a turn
                pass

    def accept(
        self,
        channel: Channel | str,
        payload: dict,
        *,
        signature: str | None = None,
        request_url: str | None = None,
        verified: bool = False,
    ) -> InboundEnvelope:
        """Normalise an inbound payload into an envelope.

        Webhook channels are authenticated; buyer-facing sessions are
        rate-limited. Per `CV-03` these are different surfaces with different
        controls — authenticating buyers would block the traffic the product
        exists to serve.
        """
        resolved = channel if isinstance(channel, Channel) else (
            CHANNEL_ALIASES.get(channel) or Channel(channel)
        )
        adapter = self._adapters.get(resolved)
        if adapter is None:
            raise ChannelError(f"no adapter registered for channel {resolved.value!r}")

        if not verified and resolved in {
            Channel.VOICE_INBOUND, Channel.VOICE_OUTBOUND, Channel.WHATSAPP
        }:
            url = request_url or f"{self.public_base_url}{self._webhook_path(resolved)}"
            validate_twilio_signature(self.twilio_auth_token, url, payload, signature)

        envelope = adapter.parse(payload)
        self.rate_limiter.check(envelope.key)

        self._emit("channel.inbound", {
            "channel": resolved.value,
            "has_identity": envelope.has_identity,
            "is_audio": envelope.is_audio,
            "text_chars": len(envelope.text),
        })
        return envelope

    def render(self, text: str, envelope: InboundEnvelope) -> dict:
        adapter = self._adapters.get(envelope.channel)
        if adapter is None:
            raise ChannelError(f"no adapter registered for channel {envelope.channel.value!r}")
        return adapter.render(text, envelope)

    @staticmethod
    def _webhook_path(channel: Channel) -> str:
        return {
            Channel.VOICE_INBOUND: "/twilio/voice",
            Channel.VOICE_OUTBOUND: "/twilio/outbound",
            Channel.WHATSAPP: "/twilio/whatsapp",
        }.get(channel, "/")

    def describe(self) -> dict:
        return {
            "channels": self.channels(),
            "capabilities": {
                channel.value: sorted(caps) for channel, caps in CAPABILITIES.items()
                if channel in self._adapters
            },
            "signature_validation": bool(self.twilio_auth_token),
            "rate_limit": {"limit": self.rate_limiter.limit, "window_s": self.rate_limiter.window_s},
        }


def default_gateway(**kwargs) -> ChannelGateway:
    """A gateway with every core adapter registered."""
    return ChannelGateway(
        [WebChatAdapter(), WhatsAppAdapter(), VoiceAdapter(channel=Channel.VOICE_INBOUND)], **kwargs
    )
