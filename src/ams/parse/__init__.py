"""Page-type parsers.

Each parser turns a :class:`bs4.BeautifulSoup` document into a
:class:`ams.models.PageDocument`. Extraction is **container-anchored** (stable
CSS classes verified against the live site) rather than heading-text-anchored —
heading text is used only to *label* a block, never to locate it. This matters
because sections are not always present (5 of 24 products have no spec block at
all, and one has FAQ pairs with no "FAQs of" heading).
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from ..models import PageDocument


def parse_page(soup: BeautifulSoup, url: str) -> PageDocument:
    """Dispatch to the parser for ``url``'s page type."""
    from ..config import load_config
    from . import (
        category,
        company,
        contact,
        home,
        product,
        products_index,
        sitemap_page,
    )

    config = load_config()
    page_type = classify(url, config)

    dispatch = {
        "product": product.parse,
        "category": category.parse,
        "products_index": products_index.parse,
        "company_profile": company.parse,
        "contact": contact.parse,
        "sitemap_page": sitemap_page.parse,
        "home": home.parse,
    }
    parser = dispatch.get(page_type)
    if parser is None:
        raise ValueError(f"No parser for page type {page_type!r} ({url})")
    return parser(soup, url)


def classify(url: str, config=None) -> str:
    """Determine a URL's page type from ``config/site.yaml`` rules."""
    import re
    from urllib.parse import urlparse

    if config is None:
        from ..config import load_config

        config = load_config()

    path = urlparse(url).path or "/"
    for rule in config.page_types:
        kind = rule.get("type", "other")
        match = rule.get("match")
        if match == "regex":
            if re.search(rule["pattern"], path):
                return kind
        elif match == "exact":
            if path in rule.get("paths", []):
                return kind
        elif match == "fallback":
            return kind
    return "other"
