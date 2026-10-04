"""Category listing page parser.

Verified structure: each product is a ``div.prod-full-list-view`` card holding a
model tag, the product name, a price line, and an attribute list.

Each attribute is an ``<li title="Condition: New">Condition: New</li>`` — the
``title`` attribute duplicates the visible text. That duplication is a free
integrity check: the two are parsed independently and any disagreement is
recorded as a warning, so extraction drift shows up immediately instead of
silently producing wrong specifications.

Scoping to the card container (rather than every ``<a>`` on the page) matters:
the sidebar repeats the full product navigation on every page, which previously
made each category appear to contain all 24 products.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from ..dom import flat_text, smart_text
from ..models import Section
from ..normalize import clean_text
from .shared import parse_generic

# ``.prod-full-list-view`` is a single wrapper around the whole listing; the
# per-product card is the ``.prod`` inside it. Selecting the wrapper would
# collapse a 10-product category into one entry.
CARD_SELECTOR = ".prod-full-list-view .prod"
PRODUCT_LINK_RE = re.compile(r"(?P<href>[^\"']*-\d{5,7}\.html)$")
PRICE_RE = re.compile(r"Price\s*:?\s*([\d,]+)\s*([A-Z]{3})?\s*/?\s*([A-Za-z]+)?")
MODEL_TAG_RE = re.compile(r"^\s*\(([^)]{1,30})\)")

# Attributes that are not product specifications.
NON_SPEC_LABELS = {"send inquiry", "request to call back", "price", "get a price/quote"}


def _parse_attributes(card: Tag) -> tuple[dict[str, str], list[str]]:
    """Read ``<li title="Label: Value">`` rows, cross-checking title vs text."""
    attributes: dict[str, str] = {}
    warnings: list[str] = []

    for item in card.select("li"):
        title = item.get("title")
        visible = clean_text(item.get_text(" ", strip=True))

        if title and ":" in title:
            label, _, value = title.partition(":")
            label, value = clean_text(label), clean_text(value)
            if label.casefold() in NON_SPEC_LABELS:
                continue
            visible_value = visible.split(":", 1)[-1].strip() if ":" in visible else visible
            visible_norm = re.sub(r"\s+", " ", visible_value)
            value_norm = re.sub(r"\s+", " ", value)

            if label and value:
                # The template writes inches into title attributes without
                # escaping them ("title=... 14" x 17" ..."), so the parser
                # truncates at the stray quote. When the attribute value is a
                # prefix of the rendered text, the rendered text is the
                # complete one — prefer it.
                if visible_norm.startswith(value_norm) and visible_norm != value_norm:
                    attributes[label] = visible_value
                    warnings.append(
                        f"title_attribute_truncated:{label}:"
                        f"kept_visible_text={visible_value!r}"
                    )
                else:
                    attributes[label] = value
                    # Consistency check: title attribute vs rendered text.
                    if visible_norm and visible_norm != value_norm:
                        warnings.append(
                            f"attribute_mismatch:{label}:"
                            f"title={value!r} visible={visible_value!r}"
                        )
            continue

        if ":" in visible:
            label, _, value = visible.partition(":")
            label, value = clean_text(label), clean_text(value)
            if label and value and label.casefold() not in NON_SPEC_LABELS:
                attributes[label] = value

    return attributes, warnings


def parse(soup: BeautifulSoup, url: str) -> PageDocument:
    """Parse a category page."""
    doc = parse_generic(soup, url, "category")

    # Category name: prefer the breadcrumb's last non-product crumb.
    category_name = None
    for crumb in doc.breadcrumb:
        if crumb.name.casefold() not in ("home", "products"):
            category_name = crumb.name
    if not category_name:
        heading = soup.find(["h1", "h2"])
        if heading:
            category_name = clean_text(flat_text(heading))
    doc.category_name = category_name

    listed: list[dict[str, object]] = []
    warnings: list[str] = []

    for card in soup.select(CARD_SELECTOR):
        card_text = smart_text(card)
        if not card_text:
            continue

        link = None
        for anchor in card.select("a[href]"):
            href = anchor.get("href", "")
            if PRODUCT_LINK_RE.search(href):
                link = href
                break

        model_match = MODEL_TAG_RE.match(card_text)
        model = clean_text(model_match.group(1)) if model_match else None

        # The product name is the first non-tag line of the card.
        lines = [clean_text(l) for l in card_text.splitlines() if clean_text(l)]
        name = None
        for line in lines:
            if line.startswith("(") or line.casefold().startswith(("price", "send inquiry", "request")):
                continue
            name = line
            break
        if not name and link:
            name = clean_text(anchor.get_text(" ", strip=True))

        price_match = PRICE_RE.search(card_text)
        price = int(price_match.group(1).replace(",", "")) if price_match else None
        price_unit = clean_text(price_match.group(3)) if price_match and price_match.group(3) else None

        attributes, attr_warnings = _parse_attributes(card)
        warnings.extend(attr_warnings)

        if not (name or link):
            continue

        product_id_match = re.search(r"-(\d{5,7})\.html$", link or "")
        listed.append(
            {
                "name": name,
                "url": link,
                "product_id": product_id_match.group(1) if product_id_match else None,
                "price": price,
                "price_unit": price_unit,
                "attributes": attributes,
                "attribute_count": len(attributes),
                "model": model,
            }
        )

    doc.listed_products = listed
    doc.relations = {
        "category": category_name,
        "products": [p["url"] for p in listed],
        "product_count": len(listed),
    }

    if listed:
        listing_text = "\n".join(
            f"{p['name']}"
            + (f" (model {p['model']})" if p["model"] else "")
            + (f" — price {p['price']} INR/{p['price_unit'] or 'unit'}" if p["price"] else "")
            + ("; " + "; ".join(f"{k}: {v}" for k, v in (p["attributes"] or {}).items())
               if p["attributes"] else "")
            for p in listed
        )
        doc.sections.append(
            Section(
                key="category_listing",
                heading=category_name or "Category",
                text=listing_text,
                char_count=len(listing_text),
            )
        )

    doc.parse_warnings = warnings
    return doc
