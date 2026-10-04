"""Contact page parser."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..dom import flat_text, smart_text
from ..normalize import clean_text
from .shared import extract_identifiers, parse_generic

# The honorific appears both with and without a period on this site
# ("Mr Rajesh Mohan Chikramane" on the contact page vs "Mr. ..." on the
# company profile), so the period must be optional.
CONTACT_PERSON_RE = re.compile(
    r"\b(Mr|Ms|Mrs)\.?\s+([A-Z][A-Za-z.]*(?:\s+[A-Z][A-Za-z.]*){1,3})\s*\n?\s*"
    r"(Chairman|Managing Director|Director|CEO|Manager|Sales)",
    re.MULTILINE,
)


def parse(soup: BeautifulSoup, url: str) -> PageDocument:
    """Parse the contact page."""
    doc = parse_generic(soup, url, "contact")

    body_text = "\n".join(s.text for s in doc.sections)
    identifiers = extract_identifiers(body_text)

    # Flatten whitespace so a role wrapped across lines ("Managing\nDirector")
    # still matches.
    flattened = re.sub(r"\s+", " ", body_text)
    contact_persons: list[dict[str, str]] = []
    seen_people: set[str] = set()
    for honorific, name, role in CONTACT_PERSON_RE.findall(flattened):
        full_name = f"{honorific}. {name}".strip()
        key = full_name.casefold()
        if key in seen_people:
            continue
        seen_people.add(key)
        contact_persons.append({"name": full_name, "role": role})

    addresses: dict[str, str] = {}
    for key, pattern in {
        "mumbai": r"[^\n]*Goregaon[^\n]*(?:400\s?063|400063)[^\n]*",
        "delhi": r"[^\n]*Karol Bagh[^\n]*(?:110\s?005|110005)[^\n]*",
    }.items():
        match = re.search(pattern, body_text)
        if match:
            addresses[key] = clean_text(match.group(0))

    # Contact details also live in the repeated site-wide footer block; those are
    # captured once in site_shared.json rather than duplicated per page.
    doc.contact_facts = {
        "persons": contact_persons,
        "addresses": addresses,
        "phones": identifiers["phones"],
        "gst_numbers": identifiers["gst"],
    }
    return doc
