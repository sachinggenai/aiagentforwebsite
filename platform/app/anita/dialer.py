"""Outbound demo dialling: the allowlist, and the one call it permits.

The browser dialer (`GET /dialer`) and `scripts/demo_call.py` are two faces of
this one object, so the guard cannot differ between them. Both read the same
list, apply the same calling window, and place the call the same way.

**What this is not.** US-021 — the lead-driven dialling worker — is `BLOCKED`
on the DG-08 compliance review of calling real buyers. Nothing here closes that
review, writes to `OutboundQueue`, or dials without a person asking. It places
one call, to a number on an operator-maintained list, when an operator says so.

**The list is the permission.** It lives in `ANITA_DEMO_CALL_ALLOWLIST`, and
`VoiceWebhook.outbound` re-reads it per call from the same file — so a number
added here is dialable on the next call, and a number removed here is refused
by the product even if something else places the call.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from .voice_webhook import ALLOWLIST_KEY, OUTBOUND_PATH, CallAllowList, normalise_number

logger = logging.getLogger("anita.dialer")


class DialRefused(Exception):
    """The call was not placed, for a reason the operator can act on.

    Distinct from a carrier failure: this one is the platform's own decision,
    so the message says which rule stopped it and what would change that.
    """


def calling_window(settings):
    """The configured calling window, as the object that owns the rule.

    `OutboundQueue` is the single definition of when this platform is willing
    to dial. A dialer with its own copy of that rule would be a second
    definition, and the two would disagree the first time anyone edited the
    config.
    """
    from zoneinfo import ZoneInfo

    from .leads import OutboundQueue

    out = settings.outbound
    try:
        offset = datetime.now(ZoneInfo(out.timezone)).utcoffset()
        hours = offset.total_seconds() / 3600 if offset else 5.5
    except Exception:  # noqa: BLE001 - a missing tz database must not block a demo
        hours = 5.5
    return OutboundQueue(
        max_attempts=out.max_attempts,
        window_start=out.window_start,
        window_end=out.window_end,
        timezone_offset_hours=hours,
    )


# ---------------------------------------------------------------------------
# The list, as a file
# ---------------------------------------------------------------------------
def read_numbers(env_path: Path) -> list[str]:
    """The configured numbers, ordered so the page does not reshuffle on edit."""
    return sorted(CallAllowList.from_file(env_path)._numbers)  # noqa: SLF001


def write_numbers(env_path: Path, numbers: list[str]) -> None:
    """Rewrite the allowlist line, leaving every other byte of `.env` alone.

    `.env` holds live credentials, so this is deliberately the narrowest edit
    that works: one line's value changes, and the file keeps its own line
    endings and its byte-order mark. Anything cleverer would be a secrets file
    being rewritten by a demo tool.
    """
    raw = env_path.read_bytes()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    value = ",".join(numbers)

    kept: list[str] = []
    replaced = False
    for line in raw.decode("utf-8-sig").splitlines(keepends=True):
        stripped = line.strip()
        if not stripped.startswith("#") and "=" in stripped:
            if stripped.split("=", 1)[0].strip() == ALLOWLIST_KEY:
                kept.append(f"{ALLOWLIST_KEY}={value}{_ending(line)}")
                replaced = True
                continue
        kept.append(line)

    if not replaced:
        if kept and not kept[-1].endswith(("\n", "\r")):
            kept.append("\n")
        kept.append(f"\n# ---- Outbound demo calls ----\n{ALLOWLIST_KEY}={value}\n")

    data = "".join(kept).encode("utf-8")
    env_path.write_bytes(b"\xef\xbb\xbf" + data if had_bom else data)


def _ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    return "\n"


# ---------------------------------------------------------------------------
class OutboundDialer:
    """Places one outbound call, through the guards the product enforces.

    Deliberately holds no Twilio client until a call is placed: the console and
    the dialer page are served by an instance that may have no carrier
    credentials at all, and a dialer that failed to construct would take those
    surfaces down with it.
    """

    def __init__(
        self,
        *,
        account_sid: str = "",
        auth_token: str = "",
        from_number: str = "",
        public_base_url: str = "",
        env_path: Path | str | None = None,
        window=None,
        client_factory=None,
    ) -> None:
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number
        # Read once at boot, like everything else derived from it: the public
        # URL is what Twilio fetches the call's TwiML from and what the
        # signature is computed against, and those must be the same string.
        self.public_base_url = (public_base_url or "").rstrip("/")
        self.env_path = Path(env_path) if env_path else None
        # A callable, so the window stays the queue's rule rather than a copy.
        self.window = window
        # Injected in tests; the default keeps the import off the boot path.
        self._client_factory = client_factory

    # -- state -------------------------------------------------------------
    @property
    def configured(self) -> bool:
        """Whether a call could be placed at all."""
        return bool(
            self.account_sid and self.auth_token and self.from_number
            and self.public_base_url
        )

    def missing(self) -> list[str]:
        """What is absent, named for the operator rather than as a boolean."""
        absent = []
        if not self.account_sid:
            absent.append("TWILIO_ACCOUNT_SID")
        if not self.auth_token:
            absent.append("TWILIO_AUTH_TOKEN")
        if not self.from_number:
            absent.append("TWILIO_PHONE_NUMBER")
        if not self.public_base_url:
            absent.append("TUNNEL_HOST")
        return absent

    def dialable(self) -> list[str]:
        if self.env_path is None:
            return []
        return read_numbers(self.env_path)

    def allows(self, number: str) -> bool:
        return CallAllowList.from_value(",".join(self.dialable())).allows(number)

    # -- editing the list --------------------------------------------------
    def add(self, number: str) -> list[str]:
        target = normalise_number(number)
        if not target:
            raise DialRefused(f"could not read a phone number from {number!r}")
        current = self.dialable()
        if target in current:
            return current
        if self.env_path is None:
            raise DialRefused("no configuration file to record the number in")
        write_numbers(self.env_path, [*current, target])
        logger.info("dialer: allowlist grew to %d number(s)", len(current) + 1)
        return self.dialable()

    def remove(self, number: str) -> list[str]:
        target = normalise_number(number)
        current = self.dialable()
        if target not in current:
            return current
        if self.env_path is None:
            raise DialRefused("no configuration file to remove the number from")
        write_numbers(self.env_path, [n for n in current if n != target])
        logger.info("dialer: allowlist shrank to %d number(s)", len(current) - 1)
        return self.dialable()

    # -- calling -----------------------------------------------------------
    def place(self, *, number: str, any_time: bool = False) -> dict:
        """Place one call. Raises `DialRefused`, or returns what Twilio said.

        The checks run in the order that keeps a mistake cheap: configuration,
        then the list, then the hour, and only then the carrier. A typo is
        therefore refused before anything rings.
        """
        absent = self.missing()
        if absent:
            raise DialRefused(
                "the dialer is not configured — missing " + ", ".join(absent)
            )

        target = normalise_number(number)
        if not target:
            raise DialRefused(f"could not read a phone number from {number!r}")

        allowlist = CallAllowList.from_value(",".join(self.dialable()))
        if not allowlist:
            raise DialRefused(
                "no numbers are dialable — add one on the left before calling"
            )
        if not allowlist.allows(target):
            raise DialRefused(
                f"{target} is not on the dialable list. Add it deliberately if "
                f"you mean to ring it."
            )

        if self.window is not None and not any_time and not self.window.in_window():
            out = self.window
            raise DialRefused(
                f"outside the calling window ({out.window_start}-{out.window_end}). "
                f"That window is a condition of dialling; override it knowingly."
            )

        twiml_url = f"{self.public_base_url}{OUTBOUND_PATH}"
        status_url = f"{self.public_base_url}/twilio/status"

        try:
            call = self._client().calls.create(
                to=target,
                from_=self.from_number,
                url=twiml_url,
                method="POST",
                status_callback=status_url,
                status_callback_method="POST",
            )
        except Exception as exc:  # noqa: BLE001 - the carrier's reason is the answer
            raise DialRefused(f"the carrier refused the call: {exc}") from exc

        logger.info("dialer: placed a call (%s)", getattr(call, "status", "?"))
        return {
            "sid": getattr(call, "sid", ""),
            "status": getattr(call, "status", ""),
            "to": target,
            "from": self.from_number,
        }

    def _client(self):
        if self._client_factory is not None:
            return self._client_factory()
        from twilio.rest import Client

        return Client(self.account_sid, self.auth_token)
