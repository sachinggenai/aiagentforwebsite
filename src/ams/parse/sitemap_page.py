"""HTML sitemap page parser.

Note: ``sitemap.html`` is **not** a complete page list — it omits
``medical-dry-imager-270683.html``. Discovery unions it with ``sitemap.xml``,
``products.html`` and a link crawl precisely because no single source is
complete.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..dom import drop_chrome_lines, smart_text
from ..models import Section
from .shared import parse_generic


def parse(soup: BeautifulSoup, url: str) -> PageDocument:
    """Parse the HTML sitemap page."""
    doc = parse_generic(soup, url, "sitemap_page")

    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        if not href.endswith(".html") or href.startswith("http"):
            continue
        if href in seen:
            continue
        seen.add(href)
        label = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
        entries.append({"url": href, "label": label})

    doc.relations = {"sitemap_entries": entries}
    body = drop_chrome_lines(smart_text(soup.body)) if soup.body else ""
    if body:
        doc.sections.append(
            Section(key="sitemap_listing", heading="Sitemap", text=body, char_count=len(body))
        )
    return doc
