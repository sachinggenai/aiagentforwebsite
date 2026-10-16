"""The outbound dialler: manage the numbers you may ring, and ring one.

    python scripts/demo_call.py --list
    python scripts/demo_call.py --add +919999900000
    python scripts/demo_call.py --to  +919999900000
    python scripts/demo_call.py --remove +919999900000

Numbers are stored in `ANITA_DEMO_CALL_ALLOWLIST` in `platform/.env`, and the
list is the permission — there is no way to dial a number that is not on it.

**What this is not.** US-021 — the lead-driven dialling worker — is `BLOCKED`
on the DG-08 compliance review of calling real buyers. This script does not
close that review, does not put rows in `OutboundQueue`, and must not be read
as the follow-up feature arriving early. It is an operator's instrument: one
call, to one number, started by hand, so somebody can watch the assistant
answer a call the platform placed.

**Two things decide whether a call may be connected, and they are checked in
two places on purpose.**

  * **Here, before dialling.** A number that is not on the list never rings, so
    a typo costs nothing and reaches nobody.
  * **In the product**, when Twilio asks for the TwiML
    (`voice_webhook.VoiceWebhook.outbound`). Twilio tells that handler which
    number it is connecting, and the handler refuses to put the assistant on a
    call to anything that is not on the list — so the guard survives whatever
    placed the call, including a `curl` that skipped this script. That check
    re-reads `.env` per call, so `--add` takes effect immediately.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.config import load_settings  # noqa: E402
from anita.leads import OutboundQueue  # noqa: E402
from anita.voice_webhook import (  # noqa: E402
    ALLOWLIST_KEY,
    OUTBOUND_PATH,
    CallAllowList,
    normalise_number,
)

ENV_PATH = PLATFORM / ".env"


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------
def read_numbers() -> list[str]:
    """The configured numbers, in the order they were written."""
    return sorted(CallAllowList.from_file(ENV_PATH)._numbers)  # noqa: SLF001


def write_numbers(numbers: list[str]) -> None:
    """Rewrite the allowlist line, leaving every other byte of `.env` alone.

    `.env` holds live credentials, so this is deliberately the narrowest edit
    that works: one line's value changes, and the file keeps its own line
    endings and its byte-order mark. Anything cleverer would be a rewrite of a
    secrets file by a demo script.
    """
    raw = ENV_PATH.read_bytes()
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
    ENV_PATH.write_bytes(b"\xef\xbb\xbf" + data if had_bom else data)


def _ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return "\n"


def add_number(number: str) -> int:
    normalised = normalise_number(number)
    if not normalised:
        print(f"Could not read a number from {number!r}")
        return 1
    current = read_numbers()
    if normalised in current:
        print(f"{normalised} is already dialable — nothing to do.")
        return 0
    write_numbers([*current, normalised])
    print(f"Added {normalised}.")
    print(f"  dialable: {', '.join(read_numbers())}")
    return 0


def remove_number(number: str) -> int:
    normalised = normalise_number(number)
    current = read_numbers()
    if normalised not in current:
        print(f"{normalised} is not on the list — nothing to do.")
        return 0
    write_numbers([n for n in current if n != normalised])
    remaining = read_numbers()
    print(f"Removed {normalised}.")
    print(f"  dialable: {', '.join(remaining) if remaining else '(nothing)'}")
    return 0


def show_numbers() -> int:
    numbers = read_numbers()
    if not numbers:
        print("No numbers are dialable.")
        print(f"  Add one: python scripts/demo_call.py --add +91XXXXXXXXXX")
        return 0
    print(f"Dialable ({len(numbers)}):")
    for n in numbers:
        print(f"  {n}")
    return 0


# ---------------------------------------------------------------------------
# Dialling
# ---------------------------------------------------------------------------
def calling_window(settings) -> OutboundQueue:
    """The configured calling window, asked of the object that owns the rule.

    `OutboundQueue` is the single definition of when this platform is willing
    to dial. A demo script with its own copy of that rule would be a second
    definition, and the two would disagree the first time anyone changed the
    config — so the rule is reused and only this script's response to it
    differs.
    """
    from zoneinfo import ZoneInfo

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


def dial(number: str, any_time: bool) -> int:
    destination = normalise_number(number)
    if not destination:
        print(f"Could not read a number from {number!r}")
        return 1

    settings = load_settings(None)  # reads .env via config._environment
    sid = settings.env.get("TWILIO_ACCOUNT_SID", "")
    token = settings.env.get("TWILIO_AUTH_TOKEN", "")
    caller = (settings.env.get("TWILIO_PHONE_NUMBER", "") or "").strip()
    host = (settings.env.get("TUNNEL_HOST", "") or "").strip()
    host = host.replace("https://", "").replace("http://", "").strip("/")

    for value, name in ((sid, "TWILIO_ACCOUNT_SID"), (token, "TWILIO_AUTH_TOKEN")):
        if not value:
            print(f"MISSING {name} — check platform/.env")
            return 1
    if not caller:
        print("MISSING TWILIO_PHONE_NUMBER — check platform/.env")
        return 1
    if not host:
        # Without it there is no public URL for Twilio to fetch TwiML from, so
        # the call would connect and then play nothing at all.
        print("MISSING TUNNEL_HOST — start the stack first (start_services.ps1)")
        return 1

    allowlist = CallAllowList.from_file(ENV_PATH)
    if not allowlist:
        print(f"REFUSED — no numbers are dialable.")
        print(f"  Add this one if you mean it: --add {destination}")
        return 2
    if not allowlist.allows(destination):
        print(f"REFUSED — {destination} is not on the dialable list.")
        print(f"  {len(allowlist)} number(s) are. Add it deliberately: --add {destination}")
        return 2

    queue = calling_window(settings)
    in_window = queue.in_window()
    if not in_window and not any_time:
        out = settings.outbound
        print(f"REFUSED — outside the calling window "
              f"({out.window_start}-{out.window_end} {out.timezone}).")
        print("  That window is a condition of dialling, not advice. For a demo")
        print("  you have chosen to run outside it: pass --any-time.")
        return 2

    twiml_url = f"https://{host}{OUTBOUND_PATH}"
    status_url = f"https://{host}/twilio/status"

    print(f"dialling : {destination}")
    print(f"from     : {caller}")
    print(f"twiml    : {twiml_url}")
    print(f"status   : {status_url}")
    if not in_window:
        print("WARNING  : outside the configured calling window — overridden with --any-time")
    print()

    from twilio.rest import Client

    try:
        call = Client(sid, token).calls.create(
            to=destination,
            from_=caller,
            url=twiml_url,
            method="POST",
            status_callback=status_url,
            status_callback_method="POST",
        )
    except Exception as exc:  # noqa: BLE001 - the carrier's error is the useful output
        print(f"TWILIO REFUSED THE CALL: {exc}")
        return 1

    print(f"placed   : {call.sid}")
    print(f"status   : {call.status}")
    print()
    print("Answer the phone. The assistant greets you, then answers questions")
    print("against the same catalogue as every other channel.")
    print()
    print("If it rings and then dies, or the assistant never speaks, the webhook")
    print("was refused — check `logs/instance.log` for the outbound refusal line.")
    return 0


# ---------------------------------------------------------------------------
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
    parser.add_argument(
        "--any-time",
        action="store_true",
        help="allow dialling outside the configured calling window",
    )
    args = parser.parse_args(argv)

    if args.list:
        return show_numbers()
    if args.add:
        return add_number(args.add)
    if args.remove:
        return remove_number(args.remove)
    return dial(args.to, args.any_time)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
