"""Point the ANITA number's Twilio webhooks at a tunnel host.

A Cloudflare *quick* tunnel mints a new hostname every time it restarts, so the
number's webhooks go stale whenever the tunnel does — and a stale webhook is a
number that rings and then does nothing. This makes re-pointing one command.

    python scripts/update_twilio_webhook.py <tunnel-host>

Updates, with read-back verification:
  1. Voice webhook    -> https://<host>/twilio/voice    (POST)
  2. Status callback  -> https://<host>/twilio/status   (POST)

Both **POST**, unlike the sibling product's voice webhook, which is GET. ANITA's
routes are declared `@app.post` in `server.py`; pointing Twilio at GET here
would return 405 with no TwiML and the call would fail silently on the carrier's
side.

The number comes from `TWILIO_PHONE_NUMBER`, and must match the endpoint
registered in `channel_endpoint` at boot — a webhook delivered for a number this
instance does not own is refused with 403 even when the signature is valid.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.config import load_settings  # noqa: E402

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2

VOICE_PATH = "/twilio/voice"
STATUS_PATH = "/twilio/status"


def _update_with_retry(number, **kwargs):
    """`number.update()` with retries on transient API errors."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return number.update(**kwargs)
        except Exception as exc:  # noqa: BLE001 - network blips / 5xx from Twilio
            last_exc = exc
            if attempt < MAX_RETRIES:
                print(f"    retry {attempt}/{MAX_RETRIES} after: {exc}")
                time.sleep(RETRY_DELAY_SECONDS)
    raise last_exc


def main(tunnel_host: str) -> int:
    from twilio.rest import Client

    settings = load_settings(None)  # reads .env via config._environment
    sid = settings.env.get("TWILIO_ACCOUNT_SID", "")
    token = settings.env.get("TWILIO_AUTH_TOKEN", "")
    phone_number = settings.env.get("TWILIO_PHONE_NUMBER", "")

    if not sid or not token:
        print("MISSING TWILIO CREDENTIALS — check platform/.env")
        return 1
    if not phone_number:
        print("MISSING TWILIO_PHONE_NUMBER — check platform/.env")
        return 1

    voice_url = f"https://{tunnel_host}{VOICE_PATH}"
    status_url = f"https://{tunnel_host}{STATUS_PATH}"

    print(f"number : {phone_number}")
    print(f"voice  : {voice_url}")
    print(f"status : {status_url}")
    print()

    client = Client(sid, token)
    try:
        numbers = list(client.incoming_phone_numbers.list(phone_number=phone_number))
        if not numbers:
            print(f"NUMBER {phone_number} NOT FOUND in this Twilio account")
            return 1

        for number in numbers:
            updated = _update_with_retry(
                number, voice_url=voice_url, voice_method="POST"
            )
            print(f"Voice URL: {number.voice_url}")
            print(f"        -> {updated.voice_url}  ({updated.voice_method})")

        for number in numbers:
            updated = _update_with_retry(
                number,
                status_callback=status_url,
                status_callback_method="POST",
            )
            print(f"Status URL: {number.status_callback or '(none)'}")
            print(f"        -> {updated.status_callback}  "
                  f"({updated.status_callback_method})")

        # Read back from a fresh fetch. The update's own echo would report the
        # value we sent whether or not Twilio stored it.
        verified = True
        for number in client.incoming_phone_numbers.list(phone_number=phone_number):
            if number.voice_url != voice_url:
                print(f"VERIFY FAIL: voice_url={number.voice_url!r} != {voice_url!r}")
                verified = False
            if number.status_callback != status_url:
                print(
                    f"VERIFY FAIL: status_callback={number.status_callback!r} "
                    f"!= {status_url!r}"
                )
                verified = False

        if verified:
            print("\nVerified: voice and status callbacks match the tunnel host.")
        else:
            print("\nNOT verified — the number's webhooks do not match.")
            return 1

        # WhatsApp is a declared deferral on this product (UC-02), so there is
        # no sandbox to point anywhere yet. Said out loud rather than left as a
        # silent omission, because the sibling script does configure one.
        print(
            "\nWhatsApp: not configured — inbound WhatsApp is deferred (UC-02)\n"
            "  in config/anita.yaml until the text and lead product is validated\n"
            "  over web chat. Nothing to point at yet."
        )
        return 0

    except Exception as exc:  # noqa: BLE001
        print(f"TWILIO API ERROR: {exc}")
        return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/update_twilio_webhook.py <tunnel-host>")
        sys.exit(1)
    sys.exit(main(sys.argv[1].replace("https://", "").replace("http://", "").strip("/")))
