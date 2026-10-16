"""The outbound dialler, from a terminal.

    python scripts/demo_call.py --list
    python scripts/demo_call.py --add +91XXXXXXXXXX
    python scripts/demo_call.py --to  +91XXXXXXXXXX [--any-time]
    python scripts/demo_call.py --remove +91XXXXXXXXXX

The same operations are on the web page at `GET /dialer`; both are thin
wrappers over `anita.dialer.OutboundDialer`, so a rule cannot hold on one and
not the other. This one exists for the case the page does not cover: a
terminal on the box, and a script that can be run from anywhere.

Numbers live in `ANITA_DEMO_CALL_ALLOWLIST` in `platform/.env`, and the list is
the permission — there is no way to dial a number that is not on it. Adding one
takes effect on the next call, because the product re-reads the file rather
than trusting a copy taken at start-up.

**What this is not.** US-021 — the lead-driven dialling worker — is `BLOCKED`
on the DG-08 compliance review of calling real buyers. This does not close that
review and does not dial without a person asking. One call, to one number, on
request.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.config import load_settings  # noqa: E402
from anita.dialer import DialRefused, OutboundDialer, calling_window  # noqa: E402

ENV_PATH = PLATFORM / ".env"


def build_dialer(settings) -> OutboundDialer:
    """The dialer as the server builds it, from the same configuration."""
    host = (settings.env.get("TUNNEL_HOST", "") or "").strip()
    host = host.replace("https://", "").replace("http://", "").strip("/")
    return OutboundDialer(
        account_sid=(settings.env.get("TWILIO_ACCOUNT_SID", "") or "").strip(),
        auth_token=(settings.env.get("TWILIO_AUTH_TOKEN", "") or "").strip(),
        from_number=(settings.env.get("TWILIO_PHONE_NUMBER", "") or "").strip(),
        public_base_url=f"https://{host}" if host else "",
        env_path=ENV_PATH,
        window=calling_window(settings),
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="demo_call.py",
        description="Manage the dialable numbers, and ring one.",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--to", metavar="+E164", help="place one call to this number")
    action.add_argument("--add", metavar="+E164", help="make a number dialable")
    action.add_argument("--remove", metavar="+E164", help="stop a number being dialable")
    action.add_argument("--list", action="store_true", help="show the dialable numbers")
    parser.add_argument("--any-time", action="store_true",
                        help="allow dialling outside the configured calling window")
    args = parser.parse_args(argv)

    dialer = build_dialer(load_settings(None))  # reads .env via config._environment

    try:
        if args.list:
            numbers = dialer.dialable()
            if not numbers:
                print("No numbers are dialable.")
                print("  Add one: python scripts/demo_call.py --add +91XXXXXXXXXX")
            else:
                print(f"Dialable ({len(numbers)}):")
                for number in numbers:
                    print(f"  {number}")
            return 0

        if args.add:
            numbers = dialer.add(args.add)
            print(f"Dialable: {', '.join(numbers)}")
            return 0

        if args.remove:
            numbers = dialer.remove(args.remove)
            print(f"Dialable: {', '.join(numbers) if numbers else '(nothing)'}")
            return 0

        result = dialer.place(number=args.to, any_time=args.any_time)

    except DialRefused as exc:
        print(f"REFUSED — {exc}")
        return 2

    print(f"placed   : {result['sid']}")
    print(f"status   : {result['status']}")
    print(f"dialling : {result['to']}")
    print(f"from     : {result['from']}")
    print(f"twiml    : {dialer.public_base_url}/twilio/outbound")
    print()
    print("Answer the phone. The assistant greets you, then answers questions")
    print("against the same catalogue as every other channel.")
    print()
    print("If it rings and then dies, or the assistant never speaks, the webhook")
    print("was refused — check `logs/instance.log` for the outbound refusal line.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
