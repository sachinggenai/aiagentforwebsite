"""Company profile parser.

The statutory block is the single richest structured source on the site:
organisation name, addresses, directors, certifications, GST, PAN, IEC and drug
licences.

Its final table row packs **8 labels against 10 values** with no delimiter, so
positional label/value alignment is genuinely ambiguous. Clean rows are read as
pairs; the identifiers in the packed row are recovered by their own formats
(see :mod:`ams.parse.shared`).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from ..dom import flat_text, smart_text
from ..models import (
    CrawlMeta,
    MetaTags,
    PageDocument,
    Section,
)
from .shared import extract_identifiers, parse_generic

# Labels that introduce the packed final row.
PACKED_LABELS = (
    "Certificate of Incorporation",
    "GST Registration No. Mumbai",
    "GST Registration No. Delhi",
    "Importer - Exporter Code",
    "PAN No",
    "TDS No",
    "Drug License no. Mumbai",
    "Drug License no. Delhi",
)

DIRECTOR_ROLE_RE = re.compile(
    r"(Mr\.|Ms\.|Mrs\.)\s*([A-Z][A-Za-z.\s]{2,40}?)\s*[-–]\s*"
    r"(Chairman|Managing Director|Director|CEO)",
)


def find_statutory_table(soup: BeautifulSoup) -> Tag | None:
    """Locate the statutory table by its content, never by index.

    There are 33 tables on a typical page and ~32 of them are chrome; indexing
    into them would be a coin flip.
    """
    for table in soup.find_all("table"):
        first_text = flat_text(table).casefold()
        if "name of the organization" in first_text:
            return table
    return None


def read_statutory_pairs(table: Tag) -> dict[str, str]:
    """Read the clean two-column rows of the statutory table."""
    pairs: dict[str, str] = {}
    for row in table.find_all("tr"):
        cells = [smart_text(c) for c in row.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if len(cells) != 2:
            continue
        label, value = cells
        # Skip the packed row and the outer wrapper row.
        if len(label) > 120 or "Name of the Organization" in label and len(value) > 200:
            continue
        if label.casefold() in {p.casefold() for p in PACKED_LABELS}:
            continue
        pairs[label] = value
    return pairs


def parse_directors(text: str) -> list[dict[str, str]]:
    """Pull ``(name, role)`` pairs out of the directors cell.

    Whitespace is flattened first: the cell wraps mid-role
    (``"Mr. R. M. Chikramane - Managing\\nDirector"``), so the role only matches
    once the newline is collapsed. Without this, that director is silently lost.
    """
    flattened = re.sub(r"\s+", " ", text or "")
    people: list[dict[str, str]] = []
    for honorific, name, role in DIRECTOR_ROLE_RE.findall(flattened):
        people.append(
            {
                "name": f"{honorific} {name}".strip(),
                "role": role,
            }
        )
    return people


def parse(soup: BeautifulSoup, url: str) -> PageDocument:
    """Parse the company profile page."""
    canonical_tag = soup.find("link", rel="canonical")
    title = soup.title.get_text().strip() if soup.title else ""

    table = find_statutory_table(soup)
    pairs: dict[str, str] = {}
    directors: list[dict[str, str]] = []
    raw_text = ""

    if table is not None:
        raw_text = smart_text(table)
        pairs = read_statutory_pairs(table)
        for key, value in pairs.items():
            if "director" in key.casefold() or "owners" in key.casefold():
                directors = parse_directors(value)

    identifiers = extract_identifiers(f"{raw_text}\n{smart_text(soup)}")

    # Directors also appear in the page's contact cards; the table is preferred,
    # so only fall back when it yielded nothing.
    if not directors:
        directors = parse_directors(smart_text(soup))

    doc = parse_generic(soup, url, "company_profile")
    doc.meta.title = title or doc.meta.title
    doc.meta.canonical = canonical_tag.get("href") if canonical_tag else doc.meta.canonical

    if raw_text:
        doc.sections.append(
            Section(
                key="statutory_details",
                heading="STATUTORY DETAILS",
                text=raw_text,
                char_count=len(raw_text),
            )
        )

    doc.company_facts = {
        **identifiers,
        "statutory": pairs,
        "directors": directors,
        "organization_name": pairs.get("Name of the Organization"),
        "nature_of_organization": pairs.get("Nature of organization"),
        "address": pairs.get("Address"),
        "branch_offices": pairs.get("Branch Offices"),
        "associated_concerns": pairs.get("List of the Associated Concerns"),
        "nature_of_business": pairs.get("Nature of Business"),
        "raw_statutory_lines": raw_text.splitlines()[:60],
    }
    return doc
