"""URL discovery.

Four independent sources are unioned, because **no single one is complete** on
this site:

* ``sitemap.xml`` — 31 unique URLs (the file itself contains 38 ``<loc>``
  entries with duplicates)
* ``sitemap.html`` — omits ``medical-dry-imager-270683.html``
* ``products.html`` — the only page listing **all 24** products
* a breadth-first link crawl from the homepage

Any URL found by exactly one source is reported. That is how a future omission
— a page dropped from the sitemap, or one that only appears in the footer —
surfaces as a warning instead of a silent gap in the corpus.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .config import Config
from .fetch import Fetcher
from .models import UrlEntry, UrlManifest

LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
PRODUCT_SLUG_RE = re.compile(r"-\d{5,7}\.html$")


def parse_sitemap_xml(xml: str) -> list[str]:
    """Return the deduplicated ``<loc>`` URLs from a sitemap."""
    seen: dict[str, None] = {}
    for match in LOC_RE.finditer(xml):
        seen.setdefault(match.group(1).strip(), None)
    return list(seen)


def extract_internal_links(html: str, base_url: str) -> set[str]:
    """Collect same-host ``.html`` links from a page."""
    soup = BeautifulSoup(html, "lxml")
    host = urlparse(base_url).netloc
    links: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.netloc != host:
            continue
        if not parsed.path.endswith(".html") and parsed.path != "/":
            continue
        # Normalise away queries and fragments — they are the same page here.
        links.add(f"{parsed.scheme}://{parsed.netloc}{parsed.path}")
    return links


def classify_url(url: str, config: Config) -> str:
    """Apply the page-type rules from config."""
    from .parse import classify

    return classify(url, config)


def discover(config: Config, fetcher: Fetcher, *, max_pages: int | None = None) -> UrlManifest:
    """Run discovery and return the URL manifest."""
    base = config.site.base_url

    # --- source 1: sitemap.xml -------------------------------------------
    sitemap_urls = parse_sitemap_xml(fetcher.load_sitemap())

    # --- source 2 & 3: seed pages that link to the catalogue -------------
    link_urls: set[str] = {base + "/"}
    seeds = [base + "/", f"{base}/products.html", f"{base}/sitemap.html"]
    for seed in seeds:
        try:
            html = fetcher.get(seed).text
        except Exception:  # noqa: BLE001 - a missing seed must not stop discovery
            continue
        link_urls |= extract_internal_links(html, base)

    # --- source 4: BFS one level out from everything found so far --------
    frontier = sorted(link_urls - set(sitemap_urls))
    for url in frontier[: max_pages or 40]:
        try:
            html = fetcher.get(url).text
        except Exception:  # noqa: BLE001
            continue
        link_urls |= extract_internal_links(html, base)

    sitemap_set = {u.rstrip("/") or u for u in sitemap_urls}
    link_set = {u.rstrip("/") or u for u in link_urls}
    all_urls = sorted(sitemap_set | link_set)

    entries: list[UrlEntry] = []
    for url in all_urls:
        from_sitemap = url in sitemap_set
        from_link = url in link_set
        source = "both" if (from_sitemap and from_link) else ("sitemap" if from_sitemap else "link")
        path = urlparse(url).path or "/"
        entries.append(
            UrlEntry(
                url=url,
                path=path,
                page_type=classify_url(url, config),  # type: ignore[arg-type]
                source=source,
            )
        )

    by_type: dict[str, int] = {}
    for entry in entries:
        by_type[entry.page_type] = by_type.get(entry.page_type, 0) + 1

    manifest = UrlManifest(
        generated_at=datetime.now(timezone.utc),
        base_url=base,
        total=len(entries),
        by_type=by_type,
        entries=entries,
        sitemap_only=sorted(sitemap_set - link_set),
        link_only=sorted(link_set - sitemap_set),
    )
    return manifest


def product_urls(manifest: UrlManifest) -> list[str]:
    return [e.url for e in manifest.entries if PRODUCT_SLUG_RE.search(e.path)]
