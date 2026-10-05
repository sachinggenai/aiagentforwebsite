"""The configurable extra messaging channel (US-002, `DG-07`).

`D1` promised that a further messaging channel would be a *configuration* task,
not a second delivery project. This is the adapter that keeps that promise, and
the honest version of it needs three things the codebase did not have:

* **A registry that can refuse.** Enabling an adapter validates its config and
  probes the provider *before* it is registered. A bad credential disables that
  adapter and **nothing else** — web chat, WhatsApp and voice keep serving
  (`AC-7`), which is the whole point of isolating the failure at the registry
  instead of at the shared gateway.
* **An HMAC gate.** Extra-channel webhooks are not Twilio; they authenticate
  with a per-adapter shared secret over the raw body (`TAC-3`). The raw bytes
  are what is signed, so the gate runs before the JSON is parsed — validating a
  re-serialised payload would accept bodies the provider never signed.
* **Idempotent enable/disable.** Re-enabling an enabled adapter is a no-op
  (`T-35`); two admins enabling at once converge (`T-39`); disabling rejects
  inbound politely and retains the config so re-enabling restores it (`T-40`).

The adapter itself is deliberately small: the format is a flat JSON payload
with a text body and a sender handle. A real provider adapter replaces this
class; the registry, the HMAC gate and the enable/disable contract do not move.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from dataclasses import dataclass
from typing import Callable

from .channels import Channel, InboundEnvelope

logger = logging.getLogger("anita.extra_channel")

# What a capability declaration may contain. Unknown entries are refused at
# validate time rather than silently ignored — a typo in a capability would
# otherwise present as a channel that accepts traffic it cannot carry.
VALID_CAPABILITIES = frozenset({"text", "media_in", "media_out", "audio", "streaming"})

REQUIRED_FIELDS = ("id", "provider", "address", "secret_env")


class AdapterConfigInvalid(ValueError):
    """The adapter's configuration is wrong, and this names the field."""

    def __init__(self, field_name: str, detail: str) -> None:
        super().__init__(f"{field_name}: {detail}")
        self.field = field_name
        self.detail = detail


class AdapterUnreachable(RuntimeError):
    """The provider did not answer the handshake, so the adapter is not enabled."""


@dataclass
class ExtraChannelConfig:
    """One extra channel's declaration. Non-secret; the secret is an env *name*."""

    id: str = ""
    provider: str = ""
    address: str = ""
    secret_env: str = ""
    capabilities: tuple[str, ...] = ("text",)
    inbound: bool = True
    outbound: bool = True
    enabled: bool = False


def validate_config(config: ExtraChannelConfig) -> None:
    """Raise :class:`AdapterConfigInvalid` naming the first bad field.

    Field-by-field rather than a single "invalid config": an operator fixing a
    channel needs to know *which* value, and `T-31` asserts the field name is
    in the error.
    """
    for name in REQUIRED_FIELDS:
        if not str(getattr(config, name) or "").strip():
            raise AdapterConfigInvalid(name, "is required to enable this adapter")

    unknown = sorted(set(config.capabilities) - VALID_CAPABILITIES)
    if unknown:
        raise AdapterConfigInvalid(
            "capabilities", f"unknown entries {unknown}; valid: {sorted(VALID_CAPABILITIES)}"
        )
    if not config.capabilities:
        # `T-33`: a missing declaration defaults to text-only rather than
        # refusing — but an *empty* declaration is a mistake, not a default.
        raise AdapterConfigInvalid("capabilities", "must declare at least one capability")
    if not (config.inbound or config.outbound):
        raise AdapterConfigInvalid(
            "inbound", "a channel with neither inbound nor outbound carries nothing"
        )


def credential_probe(env):
    """The probe the registry runs before enabling a channel.

    A provider-neutral probe is possible because every adapter shares exactly
    one precondition: the HMAC secret its `secret_env` names must exist. A
    reachability check would need the provider's own API, which this module
    deliberately does not know — so the probe answers the question it *can*
    answer, and a provider that is up but hostile still fails at the first send,
    where the outbox retries it and surfaces it (`CG-4`).

    The fifth pass recorded that `assembly` built the registry with no probe at
    all (`§8.27`), which left this leg dead in every shipped deployment — a
    "declares → validates → **probes** → enables" sequence that skipped its
    third step.
    """
    def probe(config: ExtraChannelConfig) -> None:
        name = str(config.secret_env or "").strip()
        if not name:
            raise AdapterConfigInvalid("secret_env", "is required to enable this adapter")
        if not str(env.get(name, "") or "").strip():
            # Raise the registry's own unreachable type: from the operator's
            # side this is "the channel cannot be reached", and the detail
            # names the credential rather than leaking its value.
            raise AdapterUnreachable(
                f"{name} is unset — the adapter cannot authenticate its provider"
            )

    return probe


def validate_hmac(secret: str, body: bytes, header: str | None) -> None:
    """Verify an extra-channel webhook signature over the raw body.

    Accepts ``<hex>`` and ``sha256=<hex>`` — the two shapes providers actually
    send. Constant-time compare: this is a credential check.
    """
    from .channels import SignatureError

    if not secret:
        raise SignatureError("no secret configured for this adapter")
    candidate = (header or "").strip()
    if candidate.lower().startswith("sha256="):
        candidate = candidate.split("=", 1)[1]
    if not candidate:
        raise SignatureError("missing signature header")

    expected = hmac.new(secret.encode("utf-8"), body or b"", hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, candidate.lower()):
        raise SignatureError("signature does not match")


def sign_hmac(secret: str, body: bytes) -> str:
    """The signature the tests and a local simulator send."""
    return hmac.new(secret.encode("utf-8"), body or b"", hashlib.sha256).hexdigest()


@dataclass
class ExtraChannelAdapter:
    """Flat JSON in, flat JSON out. The reference implementation (`DG-07`)."""

    channel: Channel = Channel.EXTRA
    address: str = ""
    channel_id: str = ""

    def parse(self, payload: dict) -> InboundEnvelope:
        handle = str(
            payload.get("from") or payload.get("handle") or payload.get("sender") or ""
        ).strip()
        return InboundEnvelope(
            channel=self.channel,
            text=str(payload.get("text") or payload.get("message") or "").strip(),
            identity=handle or None,
            session_id=handle or None,
            media_urls=tuple(payload.get("media") or ()),
            raw=payload,
        )

    def render(self, text: str, envelope: InboundEnvelope) -> dict:
        return {
            "type": "extra",
            "channel_id": self.channel_id,
            "body": text,
            "to": envelope.identity,
        }


@dataclass
class EnableResult:
    channel_id: str
    enabled: bool
    changed: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "enabled": self.enabled,
            "changed": self.changed,
            "detail": self.detail,
        }


class AdapterRegistry:
    """Where extra channels are enabled, disabled, and refused (UC-14).

    Holds config, not connections: `enable` validates and probes, and only then
    is the adapter reachable through `get`. A failure here cannot touch the
    built-in channels because it never reaches the gateway — which is exactly
    what `AC-7` asks for.
    """

    def __init__(self, *, probe: Callable[[ExtraChannelConfig], None] | None = None) -> None:
        self._configs: dict[str, ExtraChannelConfig] = {}
        self._adapters: dict[str, ExtraChannelAdapter] = {}
        self._enabled: set[str] = set()
        self._probe = probe
        self._lock = threading.RLock()

    def declare(self, config: ExtraChannelConfig) -> None:
        """Register the *declaration*. Not enabled — declared config can exist
        for a channel that is switched off, which is what re-enable restores."""
        with self._lock:
            self._configs[config.id] = config

    def config(self, channel_id: str) -> ExtraChannelConfig | None:
        with self._lock:
            return self._configs.get(channel_id)

    def get(self, channel_id: str) -> ExtraChannelAdapter | None:
        with self._lock:
            return self._adapters.get(channel_id) if channel_id in self._enabled else None

    def enabled(self) -> list[str]:
        with self._lock:
            return sorted(self._enabled)

    def is_enabled(self, channel_id: str) -> bool:
        with self._lock:
            return channel_id in self._enabled

    def enable(self, channel_id: str) -> EnableResult:
        """Enable a declared channel. Raises on a bad config or a dead provider.

        Idempotent: enabling an enabled channel returns `changed=False` and
        does not re-probe — a second admin clicking the same button is not a
        second handshake (`T-39`).
        """
        with self._lock:
            config = self._configs.get(channel_id)
            if config is None:
                raise AdapterNotFound(channel_id)

            if channel_id in self._enabled:
                return EnableResult(channel_id, enabled=True, changed=False,
                                    detail="already enabled")

            validate_config(config)
            if self._probe is not None:
                # A provider that does not answer must not take the other
                # channels down with it: the raise happens here, before this
                # adapter is reachable, and nothing else is touched (`AC-7`).
                self._probe(config)

            self._adapters[channel_id] = ExtraChannelAdapter(
                address=config.address, channel_id=channel_id
            )
            self._enabled.add(channel_id)
            logger.info("extra channel %r enabled", channel_id)
            return EnableResult(channel_id, enabled=True, changed=True)

    def disable(self, channel_id: str) -> EnableResult:
        """Stop serving inbound; keep the declaration so re-enable restores."""
        with self._lock:
            if channel_id not in self._enabled:
                return EnableResult(channel_id, enabled=False, changed=False,
                                    detail="already disabled")
            self._enabled.discard(channel_id)
            self._adapters.pop(channel_id, None)
            logger.info("extra channel %r disabled", channel_id)
            return EnableResult(channel_id, enabled=False, changed=True)

    def describe(self) -> dict:
        with self._lock:
            return {
                "declared": sorted(self._configs),
                "enabled": sorted(self._enabled),
                "channels": {
                    channel_id: {
                        "capabilities": list(config.capabilities),
                        "inbound": config.inbound,
                        "outbound": config.outbound,
                        "provider": config.provider,
                    }
                    for channel_id, config in self._configs.items()
                    if channel_id in self._enabled
                },
            }


class AdapterNotFound(KeyError):
    """No declaration exists for this channel id."""


class ExtraChannelIngress:
    """Inbound extra-channel webhooks: HMAC, then the same answer path.

    Deliberately parallel to :class:`anita.messaging.MessagingWebhook` — same
    order (authenticate → dedupe where an event id exists → answer → outbox),
    different authentication. Not one class with a pluggable gate: the two
    differ in what a *rejection* means (Twilio retries; a generic provider may
    not), and folding them together would hide that.
    """

    def __init__(
        self,
        *,
        registry: AdapterRegistry,
        gateway,
        answer: Callable[..., str],
        secrets: dict[str, str] | None = None,
        outbox=None,
        sender=None,
        store=None,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.registry = registry
        self.gateway = gateway
        self.answer = answer
        self.secrets = secrets or {}
        self.outbox = outbox
        self.sender = sender
        self.store = store
        self._on_event = on_event

    def handle(self, channel_id: str, *, body: bytes,
               signature: str | None) -> tuple[int, dict]:
        """One inbound webhook. Returns ``(status, json_body)``."""
        from .channels import RateLimited, SignatureError

        config = self.registry.config(channel_id)
        if config is None:
            return 404, {"error": "no such channel"}
        if not config.inbound:
            return 405, {"error": "this channel accepts outbound only"}
        if not self.registry.is_enabled(channel_id):
            # Disabled but declared: refused politely, config retained (`T-40`).
            return 200, {"accepted": False, "reason": "channel disabled"}

        secret = self.secrets.get(config.secret_env, "")
        try:
            validate_hmac(secret, body, signature)
        except SignatureError as exc:
            self._reject("/channels/inbound", "hmac", str(exc))
            return 401, {"error": "signature rejected"}

        try:
            payload = json.loads(body or b"{}")
        except Exception:  # noqa: BLE001 - unparseable is a bad request, not a 500
            return 400, {"error": "body is not valid JSON"}
        if not isinstance(payload, dict):
            return 400, {"error": "body must be a JSON object"}

        adapter = self.registry.get(channel_id)
        assert adapter is not None  # enabled above
        envelope = adapter.parse(payload)

        try:
            self.gateway.rate_limiter.check(f"extra:{envelope.key}")
        except RateLimited:
            self._reject("/channels/inbound", "rate_limit", "")
            return 429, {"error": "rate limited"}

        if not envelope.text:
            return 200, {"accepted": True, "reply": ""}

        try:
            answer = self.answer(envelope.text, session_id=envelope.key)
        except Exception:  # noqa: BLE001 - an engine failure still owes a reply
            logger.exception("extra channel: the answer path failed")
            answer = "Something went wrong on our side. Please try again shortly."

        rendered = adapter.render(str(answer or ""), envelope)
        if config.outbound and self.outbox is not None:
            from .outbox import dispatch_now

            dispatch_now(
                self.outbox, self.sender,
                channel_kind=Channel.EXTRA.value,
                payload={**rendered, "channel": Channel.EXTRA.value},
                conversation_id=envelope.key,
            )
        return 200, {"accepted": True, "reply": rendered["body"]}

    def _reject(self, route: str, reason: str, detail: str) -> None:
        self._emit("extra_channel.rejected", {"reason": reason, "detail": detail})
        if self.store is not None:
            try:
                self.store.record_rejection(route=route, reason=reason)
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
    "AdapterConfigInvalid", "AdapterNotFound", "AdapterRegistry",
    "AdapterUnreachable", "EnableResult", "ExtraChannelAdapter",
    "ExtraChannelConfig", "ExtraChannelIngress", "VALID_CAPABILITIES",
    "sign_hmac", "validate_config", "validate_hmac",
]
