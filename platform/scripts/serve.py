"""Start the platform (MOD-10).

Assembles the instance, prints what was built and what is missing, then serves.
Refuses to start when preflight refuses — an instance that shares a database, a
collection or a port with a sibling product would be serving another product's
data, and the moment to discover that is before the socket opens, not after.

Run:
    .venv\\Scripts\\python scripts\\serve.py                  # 127.0.0.1:8100
    .venv\\Scripts\\python scripts\\serve.py --port 8101
    .venv\\Scripts\\python scripts\\serve.py --check           # assemble, report, exit
    .venv\\Scripts\\python scripts\\serve.py --host 0.0.0.0    # expose on the LAN

Binding to 0.0.0.0 is deliberately not the default. The admin console is
authenticated with a single bearer token and has no TLS of its own: exposing it
directly means that token crosses the network in clear text. Put it behind the
tunnel or a reverse proxy that terminates TLS.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

# Windows consoles default to cp1252, which cannot encode the catalogue's own
# characters — the site uses "✕", "₹" and en/em dashes. Without this, a report
# containing a rupee sign raises UnicodeEncodeError at the moment it is printed,
# which is the worst place for a crash: the work succeeded and the summary of it
# did not. The scraper's entry points force UTF-8 for the same reason.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None,
                        help="defaults to the configured ANITA_API_PORT")
    parser.add_argument("--check", action="store_true",
                        help="assemble and report, but do not serve")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="skip the isolation audit — development only")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    from anita.assembly import assemble
    from anita.errors import ConfigError, IsolationViolation, SecretMissing

    try:
        built = assemble(require_preflight=not args.skip_preflight)
    except (IsolationViolation, SecretMissing, ConfigError) as exc:
        print("BOOT REFUSED\n" + str(exc), file=sys.stderr)
        return 3
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print("=" * 74)
    print("ANITA MEDICAL SYSTEMS — platform")
    print("=" * 74)
    print(built.describe())
    port = args.port or built.settings.isolation.api_port
    print(f"listen    : http://{args.host}:{port}")
    print(f"  chat    : http://{args.host}:{port}/")
    print(f"  console : http://{args.host}:{port}/console")
    print("=" * 74)

    if args.check:
        print("--check: assembled successfully, not serving.")
        return 0

    if built.settings.env.get("ANITA_ADMIN_TOKEN") is None:
        # Not fatal — the surfaces fail closed, so the console simply refuses
        # every request. Saying so here saves an operator working out why.
        print("note: ANITA_ADMIN_TOKEN is unset — the admin console will refuse "
              "every request (it fails closed).", file=sys.stderr)

    from anita.server import serve

    serve(built.router, host=args.host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
