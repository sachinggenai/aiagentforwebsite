"""Catalog index (showroom) page parser.

``/products.html`` is the only page listing **all 24 products** — the homepage
omits 7 and ``sitemap.html`` omits 1. It groups products under categories with a
per-category count, making it the authoritative seed for product discovery.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..dom import drop_chrome_lines, flat_text, smart_text
from ..models import Section
from ..normalize import clean_text
from .shared import parse_generic

# Lines that mark the end of the catalogue listing.
STOP_LINES = frozenset(
    {
        "quick links",
        "our products",
        "send inquiry",
        "home page",
        "company profile",
        "contact us",
        "sitemap",
        "gst : 27aaaca3337r1zd",
        "terms of use",
    }
)


def parse(soup: BeautifulSoup, url: str) -> PageDocument:
    """Parse the showroom / catalog index page."""
    doc = parse_generic(soup, url, "products_index")

    groups: list[dict[str, object]] = []
    seen: set[str] = set()
    current: dict[str, object] | None = None

    scope = soup.select_one(".showroom") or soup.select_one(".container") or soup
    text = drop_chrome_lines(smart_text(scope))

    # The template emits each category as a count on its own line followed by the
    # category name:
    #
    #     (10)
    #     Fuji CR Imaging Systems
    #     FCR Prima Tm Imaging Equipment
    #     ...
    #
    # so the count must be carried forward to the next line rather than matched
    # inline. The declared count is a free cross-check against the parsed list.
    pending_count: int | None = None
    for line in text.splitlines():
        line = clean_text(line)
        if not line:
            continue

        count_match = re.fullmatch(r"\((\d{1,3})\)", line)
        if count_match:
            pending_count = int(count_match.group(1))
            continue

        if pending_count is not None:
            current = {
                "category": line,
                "declared_count": pending_count,
                "products": [],
            }
            groups.append(current)
            pending_count = None
            continue

        # Stop at the footer: without this the final category absorbs every
        # remaining footer and navigation line as if they were products.
        if line.casefold() in STOP_LINES:
            current = None
            continue

        if current is not None and line != "...More":
            products = current["products"]
            if isinstance(products, list) and len(line) < 80:
                products.append(line)

    for anchor in scope.select("a[href]"):
        href = anchor.get("href", "")
        if re.search(r"-\d{5,7}\.html$", href):
            seen.add(href)

    doc.listed_products = groups
    doc.relations = {
        "catalog_groups": groups,
        "product_urls": sorted(seen),
        "product_count": len(seen),
    }
    if text:
        doc.sections.append(
            Section(key="catalog", heading="Showroom", text=text, char_count=len(text))
        )
    return doc
