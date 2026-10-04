"""Seed the raw cache from reconnaissance snapshots.

During planning, a handful of pages were saved to disk before the host began
returning HTTP 403. This imports them into the normal cache layout so the parse
and emit stages can be developed and tested *without* touching the network.

These are real captures of the live site — not fixtures — so parsed output is
representative. A later live crawl simply adds the remaining pages; cached
entries are reused rather than re-fetched.

Usage:
    python scripts/seed_cache.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ams.config import force_utf8, load_config  # noqa: E402
from ams.fetch import safe_filename  # noqa: E402

# filename -> live URL it was captured from (canonical URL verified in each file)
SNAPSHOTS: dict[str, str] = {
    "anita_home.html": "https://www.anitamedicalsystems.com/",
    "anita_company.html": "https://www.anitamedicalsystems.com/company-profile.html",
    "anita_contact.html": "https://www.anitamedicalsystems.com/contact-us.html",
    "anita_product.html": "https://www.anitamedicalsystems.com/ultrasound-gel-270694.html",
    "anita_products.html": "https://www.anitamedicalsystems.com/products.html",
    "anita_sitemap.html": "https://www.anitamedicalsystems.com/sitemap.html",
    "pc.html": "https://www.anitamedicalsystems.com/x-ray-cassette-270691.html",
}


def main() -> int:
    force_utf8()
    config = load_config()
    config.ensure_dirs()

    temp_dir = Path(os.environ.get("TEMP", "."))
    raw_dir = config.path("raw")
    seeded = 0

    for filename, url in SNAPSHOTS.items():
        source = temp_dir / filename
        if not source.exists():
            print(f"  skip  {filename} (not found in {temp_dir})")
            continue

        html = source.read_text(encoding="utf-8", errors="replace")
        name = safe_filename(url)
        (raw_dir / name).write_text(html, encoding="utf-8")
        (raw_dir / f"{name}.meta.json").write_text(
            json.dumps(
                {
                    "url": url,
                    "final_url": url,
                    "status": 200,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "content_sha256": hashlib.sha256(
                        html.encode("utf-8", "replace")
                    ).hexdigest(),
                    "bytes": len(html.encode("utf-8", "replace")),
                    "etag": None,
                    "last_modified": None,
                    # Provenance is explicit: these came from a manual recon pass,
                    # not from the automated fetcher.
                    "from_cache": True,
                    "attempt": 1,
                    "seeded_from": filename,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        seeded += 1
        print(f"  seeded {name:44} <- {filename}")

    print(f"\n{seeded} page(s) seeded into {raw_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
