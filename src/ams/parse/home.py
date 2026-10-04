"""Homepage parser.

The homepage carries the richest company narrative on the site — the
"ABOUT OURSELVES" block covering company history (Anita Enterprises 1979, AMS
1984), the brands represented, office and warehouse locations, headcount and
installed-base figures. None of this appears on the company-profile page, so it
must be captured here.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..dom import drop_chrome_lines, find_heading, flat_text, section_body, smart_text
from ..models import Section
from .shared import extract_identifiers, parse_generic

BRAND_TOKENS = (
    "FUJI", "FUJIFILM", "Bayer", "MEDRAD", "Apollo RT", "Skanray",
    "Kiran", "Trivitron", "Zydus", "ESKAY", "Allied Photographic",
    "Jindal Photo", "Criticare",
)


def parse(soup: BeautifulSoup, url: str) -> PageDocument:
    """Parse the homepage."""
    content_selectors = (".about_ourselves", ".about", "#about", ".welcome", ".home_about")
    doc = parse_generic(soup, url, "home", content_selectors=content_selectors)

    # Locate the "ABOUT OURSELVES" block by its heading text. Here heading text
    # is a *label*, not a locator: if it is absent we simply skip the section
    # rather than falling back to something ambiguous.
    about_text = ""
    heading = find_heading(soup, "ABOUT OURSELVES", levels=("h1", "h2", "h3", "h4"))
    if heading is not None:
        about_text = section_body(heading, levels=("h1", "h2", "h3", "h4"))

    if not about_text:
        # The heading is often styled text rather than a real heading element.
        match = re.search(
            r"ABOUT OURSELVES(.*?)(?:Our Products|HOT PRODUCTS|Most Popular Products|Quick Links|$)",
            smart_text(soup),
            re.DOTALL | re.IGNORECASE,
        )
        if match:
            about_text = match.group(1).strip()

    about_text = drop_chrome_lines(about_text)
    if about_text:
        doc.sections.append(
            Section(
                key="about_ourselves",
                heading="ABOUT OURSELVES",
                text=about_text,
                char_count=len(about_text),
            )
        )

    all_text = "\n".join(s.text for s in doc.sections) + "\n" + about_text
    brands = sorted({b for b in BRAND_TOKENS if re.search(rf"\b{re.escape(b)}\b", all_text, re.I)})

    counts: dict[str, str] = {}
    for match in re.finditer(
        r"(?:nearly|about|approx\.?)?\s*([\d,]{3,})\s+([A-Z][A-Za-z ]{3,40})", all_text
    ):
        value, what = match.group(1), match.group(2).strip()
        if any(k in what.casefold() for k in ("fuji", "system", "printer", "personnel", "employee", "cr ", "dr ")):
            counts[what] = value

    doc.company_facts = {
        **doc.company_facts,
        **extract_identifiers(all_text),
        "brands_represented": brands,
        "installed_base_mentions": counts,
    }

    doc.headings = {k: v for k, v in doc.headings.items()}
    return doc
