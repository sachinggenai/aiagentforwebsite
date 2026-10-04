"""Discover every URL on the site and write the URL manifest.

Usage:
    python scripts/01_discover.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ams.config import force_utf8, load_config  # noqa: E402
from ams.discover import discover  # noqa: E402
from ams.fetch import Fetcher  # noqa: E402


def main() -> int:
    force_utf8()
    config = load_config()
    config.ensure_dirs()

    with Fetcher(config) as fetcher:
        robots = fetcher.load_robots()
        print(f"robots.txt: {len(robots.disallow)} disallow rule(s)")
        for rule in robots.disallow:
            print(f"    {rule}")

        manifest = discover(config, fetcher)

    path = config.path("url_manifest")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    print(f"\ndiscovered {manifest.total} unique URL(s)")
    for page_type, count in sorted(manifest.by_type.items()):
        print(f"    {page_type:16} {count}")

    if manifest.sitemap_only:
        print(f"\nin sitemap but not linked: {len(manifest.sitemap_only)}")
        for url in manifest.sitemap_only:
            print(f"    {url}")
    if manifest.link_only:
        print(f"\nlinked but not in sitemap: {len(manifest.link_only)}")
        for url in manifest.link_only:
            print(f"    {url}")

    print(f"\nfetch stats: {fetcher.stats}")
    print(f"manifest -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
