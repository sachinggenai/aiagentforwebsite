"""Fetch every discovered URL into the raw cache.

Sequential and deliberately slow: the host returns HTTP 403 under sustained
crawling, so this pauses between requests, backs off exponentially, and stops
cleanly if the block returns. Progress is cached, so a stopped run loses
nothing — re-run to resume.

Usage:
    python scripts/02_fetch.py [--force] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ams.config import force_utf8, load_config  # noqa: E402
from ams.fetch import CircuitBreakerTripped, Fetcher  # noqa: E402
from ams.models import UrlManifest  # noqa: E402

# A CloudFront/WAF block returns 403 with a tiny "Request blocked." body. Such a
# response must never be mistaken for a real page, and must never be cached.
BLOCK_BODY_MARKERS = ("Request blocked", "Access Denied")
MIN_REAL_PAGE_BYTES = 5_000


def looks_blocked(status: int, text: str) -> bool:
    if status in (403, 429):
        return True
    return len(text) < MIN_REAL_PAGE_BYTES and any(m in text for m in BLOCK_BODY_MARKERS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Re-fetch cached pages")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    force_utf8()
    config = load_config()
    config.ensure_dirs()

    manifest_path = config.path("url_manifest")
    if not manifest_path.exists():
        print("No url_manifest.json — run scripts/01_discover.py first.")
        return 1
    manifest = UrlManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))

    urls = [e.url for e in manifest.entries]
    if args.limit:
        urls = urls[: args.limit]

    fetched = cached = blocked = failed = 0
    problems: list[tuple[str, str]] = []

    with Fetcher(config, force=args.force) as fetcher:
        try:
            fetcher.load_robots()
            for index, url in enumerate(urls, start=1):
                try:
                    result = fetcher.get(url)
                except PermissionError as exc:
                    problems.append((url, f"robots disallowed: {exc}"))
                    continue
                except CircuitBreakerTripped as exc:
                    print(f"\n!! {exc}")
                    break
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    problems.append((url, f"{type(exc).__name__}: {exc}"))
                    continue

                if looks_blocked(result.status, result.text):
                    blocked += 1
                    problems.append((url, f"blocked (HTTP {result.status})"))
                    # Remove any poisoned cache entry so a later run retries it.
                    html_path, meta_path = fetcher._paths_for(url)  # noqa: SLF001
                    for path in (html_path, meta_path):
                        if path.exists():
                            path.unlink()
                    break

                if result.from_cache:
                    cached += 1
                    marker = "cached"
                else:
                    fetched += 1
                    marker = "fetched"
                print(f"  [{index:>3}/{len(urls)}] {marker:8} {result.status} "
                      f"{len(result.text) // 1024:>4}KB  {url}")

            print(f"\nnewly fetched: {fetched}   from cache: {cached}   "
                  f"blocked: {blocked}   failed: {failed}")
            print(f"fetcher stats: {json.dumps(fetcher.stats)}")
        finally:
            pass

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for url, reason in problems:
            print(f"    {url}\n        {reason}")

    return 1 if (blocked or problems) else 0


if __name__ == "__main__":
    raise SystemExit(main())
