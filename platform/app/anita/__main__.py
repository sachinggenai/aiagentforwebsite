"""``python -m anita`` — delegates to the boot CLI.

Kept as a thin alias so the documented invocations both work:

    python -m anita --preflight
    python -m anita.boot --preflight
"""

from __future__ import annotations

from .boot import main

if __name__ == "__main__":
    raise SystemExit(main())
