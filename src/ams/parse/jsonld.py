"""schema.org extraction (JSON-LD and microdata).

A data-quality warning about the JSON-LD ``description`` field
------------------------------------------------------------
The site's ``Product.description`` is **space-corrupted**: it reads
``"An X - Ray cassette is a device used to record X - ray images on a film ."``
The corruption is baked into the JSON as literal spaces, because the field was
generated from the same Draft.js HTML that :func:`ams.dom.smart_text` repairs.
It cannot be reversed reliably here.

So the rule is: **never take prose from JSON-LD.** The DOM ``About`` section is
extracted cleanly and is the canonical prose. The JSON-LD description is kept
only as a fallback and is flagged in ``parse_warnings`` when used.

The structured fields (price, availability, specs, delivery lead time) are
reliable and are preferred from JSON-LD, since they are already typed.
"""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from ..models import BreadcrumbItem, Offer, ProductStructured, SpecItem
from ..normalize import parse_lead_time_days, parse_price, parse_quantity, clean_text

_CORRUPTION_RE = re.compile(r"\s+[-.]\s+")


def looks_corrupted(text: str | None) -> bool:
    """Heuristic: does this text carry Draft.js space-splitting damage?

    Matches the give-away patterns ``"X - Ray"`` and ``"film ."`` — a hyphen or
    full stop with stray spaces on the wrong side.
    """
    if not text:
        return False
    return bool(_CORRUPTION_RE.search(text))


def extract_jsonld_blocks(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Return every parseable ``application/ld+json`` block."""
    blocks: list[dict[str, Any]] = []
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(data, list):
            blocks.extend(x for x in data if isinstance(x, dict))
        elif isinstance(data, dict):
            blocks.append(data)
    return blocks


def _specs_from_additional_property(props: list[Any]) -> list[SpecItem]:
    specs: list[SpecItem] = []
    for prop in props:
        if not isinstance(prop, dict):
            continue
        name = clean_text(str(prop.get("name", "")))
        value = prop.get("value")
        if isinstance(value, dict):
            value = value.get("value", "")
        value = clean_text(str(value if value is not None else ""))
        if name and value:
            specs.append(SpecItem(name=name, value=value))
    return specs


def _offer_from_jsonld(offers: Any, specs: list[SpecItem]) -> Offer | None:
    """Build an :class:`Offer`, enriching it from the spec list.

    ``eligibleQuantity.unitCode`` on this site carries a junk value (``"FTK"``),
    so the MOQ unit is taken from the human-readable spec rows instead.
    """
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        offers = {}

    specs_by_name = {s.name.casefold(): s.value for s in specs}

    raw_price = offers.get("price")
    price_text = str(raw_price) if raw_price is not None else None
    unit = None
    for key in ("unit of price", "price unit"):
        if key in specs_by_name:
            unit = specs_by_name[key].split("/")[-1].strip()
            break

    money = parse_price(price_text, unit=unit) if price_text else None

    moq_value = moq_unit = None
    for key in ("minimum order quantity", "moq"):
        if key in specs_by_name:
            qty = parse_quantity(specs_by_name[key])
            moq_value = qty.value if qty.value is not None else qty.value_min
            moq_unit = qty.unit
            break

    supply_ability = next(
        (v for k, v in specs_by_name.items() if "supply ability" in k), None
    )
    sample_available = next(
        (v for k, v in specs_by_name.items() if k == "sample available"), None
    )
    sample_policy = next(
        (v for k, v in specs_by_name.items() if k == "sample policy"), None
    )
    market = next(
        (v for k, v in specs_by_name.items() if "domestic market" in k), None
    )

    lead = offers.get("deliveryLeadTime") or next(
        (v for k, v in specs_by_name.items() if k == "delivery time"), None
    )
    availability = offers.get("availability")
    if isinstance(availability, str):
        availability = availability.rsplit("/", 1)[-1]

    return Offer(
        price=money.amount_int if money else None,
        price_text=price_text,
        currency=offers.get("priceCurrency") or (money.currency if money else "INR"),
        price_unit=unit,
        availability=availability,
        item_condition=(offers.get("itemCondition") or "").rsplit("/", 1)[-1] or None,
        delivery_lead_time=str(lead) if lead else None,
        moq_value=moq_value,
        moq_unit=moq_unit,
        supply_ability=supply_ability,
        sample_available=sample_available,
        sample_policy=sample_policy,
        main_domestic_market=market,
    )


def extract_product(soup: BeautifulSoup) -> tuple[ProductStructured | None, list[str]]:
    """Extract the ``Product`` JSON-LD block.

    Returns the structured product and a list of data-quality warnings.
    """
    warnings: list[str] = []
    for block in extract_jsonld_blocks(soup):
        if str(block.get("@type", "")).casefold() != "product":
            continue

        specs = _specs_from_additional_property(block.get("additionalProperty", []))
        description = block.get("description")
        if looks_corrupted(description):
            warnings.append(
                "jsonld_description_space_corrupted: JSON-LD description carries "
                "Draft.js space damage; DOM About text used instead"
            )

        image = block.get("image")
        if isinstance(image, list):
            image = image[0] if image else None

        brand = block.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")

        return (
            ProductStructured(
                name=clean_text(str(block.get("name", ""))) or None,
                description=clean_text(str(description)) if description else None,
                image=str(image) if image else None,
                url=block.get("url"),
                brand=clean_text(str(brand)) if brand else None,
                category=clean_text(str(block.get("category", ""))) or None,
                sku=str(block.get("sku")) if block.get("sku") else None,
                specs=specs,
                offer=_offer_from_jsonld(block.get("offers"), specs),
            ),
            warnings,
        )
    return None, warnings


def extract_breadcrumb(soup: BeautifulSoup) -> list[BreadcrumbItem]:
    """Extract the BreadcrumbList microdata."""
    items: list[BreadcrumbItem] = []
    container = soup.find(attrs={"itemtype": re.compile("BreadcrumbList", re.I)})
    if not container:
        return items

    for element in container.find_all(attrs={"itemprop": "itemListElement"}):
        name_tag = element.find(attrs={"itemprop": "name"})
        name = clean_text(name_tag.get_text()) if name_tag else ""
        if not name:
            name = clean_text(element.get_text())
        link_tag = element.find("a", href=True)
        position = element.find(attrs={"itemprop": "position"})
        try:
            pos = int(clean_text(position.get_text())) if position else None
        except ValueError:
            pos = None
        if name:
            items.append(
                BreadcrumbItem(
                    position=pos,
                    name=name,
                    url=link_tag["href"] if link_tag else None,
                )
            )

    if not items:
        # Fall back to the visible breadcrumb links.
        for anchor in container.find_all("a", href=True):
            text = clean_text(anchor.get_text())
            if text:
                items.append(BreadcrumbItem(name=text, url=anchor["href"]))
    return items


def extract_og_meta(soup: BeautifulSoup) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(og, twitter)`` meta maps."""
    og: dict[str, str] = {}
    twitter: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        prop = tag.get("property") or ""
        name = tag.get("name") or ""
        content = tag.get("content")
        if not content:
            continue
        if prop.startswith("og:"):
            og[prop] = content
        elif name.startswith("twitter:"):
            twitter[name] = content
    return og, twitter
