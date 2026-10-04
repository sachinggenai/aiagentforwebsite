"""Site-wide shared blocks.

Navigation, footer, addresses, tax identifiers and the inquiry-modal field list
repeat on all 32 pages. Storing them 32 times would flood retrieval: a question
like "what is your address" would match 32 near-identical chunks and drown out
the real answer.

Two complementary strategies:

* **Frequency-derived chrome** — links present on nearly every page are, by
  definition, navigation. Deriving them from the corpus keeps this correct if
  the menu changes, instead of hard-coding a link list that silently rots.
* **Pattern-extracted identifiers** — GST, PAN, IEC and ISO numbers are
  extracted by their own well-formed shapes. The statutory table's final row
  packs 8 labels against 10 values, so positional alignment is genuinely
  ambiguous there; matching the identifiers' formats is exact where column
  alignment is not.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..dom import drop_chrome_lines, flat_text, make_soup, smart_text, strip_noise
from ..models import CrawlMeta, MetaTags, PageDocument, SiteShared

# Well-formed identifier shapes.
#
# NOTE ON WORD BOUNDARIES: these deliberately do NOT use ``\b``. The statutory
# table's packed final row concatenates its values with no separator at all:
#
#     "Regd. Sr.No. 32903 dt. 17.05.198427AAACA3337R1ZD07AAACA3337R1ZF0388056606"
#
# Because the values touch, ``\b`` fails on every one of them and the whole row
# would be silently lost. The formats themselves are specific enough to match
# without boundary assertions.
GSTIN_RE = re.compile(r"\d{2}[A-Z]{5}\d{4}[A-Z]\d[ZX][A-Z0-9]")
PAN_RE = re.compile(r"[A-Z]{5}\d{4}[A-Z]")
IEC_RE = re.compile(r"(?<!\d)\d{10}(?!\d)")
ISO_RE = re.compile(r"ISO\s*\d{4}\s*:\s*\d{4}", re.IGNORECASE)
DRUG_LICENSE_RE = re.compile(r"MUMA\d{5}[A-Z]")
NSIC_RE = re.compile(r"NSIC\s*[-–]\s*CRISIL[^\n]{0,90}", re.IGNORECASE)
# Indian numbers on this site are either an 11-digit landline with a leading
# trunk 0 (080 45477795) or a 10-digit mobile. The landline must be matched as
# 0 + 10 digits, or the trailing digit is silently truncated.
PHONE_RE = re.compile(r"(?:\+?91[-\s]?)?(?:0\d{10}|[6-9]\d{9})")
PINCODE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")
# Bounded so it stops at the date rather than running into the next value.
REGISTRATION_RE = re.compile(
    r"Regd\.\s*Sr\.No\.\s*\d+\s*dt\.\s*\d{2}[.\-/]\d{2}[.\-/]\d{4}", re.IGNORECASE
)


def extract_identifiers(text: str) -> dict[str, list[str]]:
    """Pull well-formed business identifiers out of a page's text.

    Extraction is sequential with masking: each identifier type is found and then
    blanked out before the next is searched. Without masking, the PAN pattern
    (``AAACA3337R``) matches *inside* the GSTIN ``27AAACA3337R1ZD`` and the same
    number is reported as two different identifiers.
    """
    working = text or ""

    gst = sorted(set(GSTIN_RE.findall(working)))
    working = GSTIN_RE.sub(" " * 15, working)

    pan = sorted(set(PAN_RE.findall(working)))
    working = PAN_RE.sub(" " * 10, working)

    pincodes = {m.group(0) for m in PINCODE_RE.finditer(text or "")}
    iec = sorted(
        {
            m.group(0)
            for m in IEC_RE.finditer(working)
            # Exclude 10-digit runs that are really phone numbers or pincodes.
            if m.group(0) not in pincodes and not m.group(0).startswith(("6", "7", "8", "9"))
        }
    )

    return {
        "gst": gst,
        "pan": pan,
        "iec": iec,
        "iso": sorted({m.group(0) for m in ISO_RE.finditer(text or "")}),
        "drug_license": sorted(set(DRUG_LICENSE_RE.findall(text or ""))),
        "nsic_crisil": sorted(
            {
                re.sub(r"\s+", " ", m.group(0)).strip(" -")
                for m in NSIC_RE.finditer(text or "")
            }
        ),
        "registration": sorted(
            {re.sub(r"\s+", " ", m.group(0)) for m in REGISTRATION_RE.finditer(text or "")}
        ),
        "phones": sorted(set(PHONE_RE.findall(text or ""))),
    }


def extract_addresses(text: str) -> dict[str, str]:
    """Find Mumbai and Delhi addresses by their landmark tokens."""
    addresses: dict[str, str] = {}
    patterns = {
        "mumbai": r"[^\n]*Goregaon[^\n]*(?:400\s?063|400063)[^\n]*",
        "delhi": r"[^\n]*Karol Bagh[^\n]*(?:110\s?005|110005)[^\n]*",
        "navi_mumbai": r"[^\n]{0,120}Navi Mumbai[^\n]{0,80}",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            value = re.sub(r"\s+", " ", match.group(0)).strip(" ,")
            addresses[key] = value
    return addresses


def extract_inquiry_form_fields(html: str) -> list[str]:
    """Names of every input/select/textarea inside the inquiry forms."""
    soup = make_soup(html)
    names: set[str] = set()
    for form in soup.find_all("form"):
        for field in form.find_all(["input", "select", "textarea"]):
            name = field.get("name")
            if name:
                names.add(str(name))
    return sorted(names)


def build_site_shared(documents: list[PageDocument], raw_html: dict[str, str]) -> SiteShared:
    """Synthesise the shared block from every parsed document.

    ``raw_html`` maps URL to the page's original HTML, used for form-field
    extraction (which needs the markup that ``strip_noise`` removes).
    """
    if not documents:
        return SiteShared()

    # --- Navigation: links that appear on nearly every page ---------------
    threshold = max(2, int(len(documents) * 0.8))
    counter: Counter[str] = Counter()
    labels: dict[str, str] = {}
    for doc in documents:
        for href in set(doc.links_internal):
            # Normalise absolute and relative forms to a single path, so the
            # same link is not counted twice under two spellings.
            normalized = _normalize_href(href)
            if normalized:
                counter[normalized] += 1
    for href, count in counter.items():
        if count >= threshold:
            labels.setdefault(href, _label_for(doc_set=documents, href=href))

    nav = [
        {"url": href, "label": labels.get(href, href)}
        for href, count in counter.most_common()
        if count >= threshold
    ]

    # --- Identifiers from the pages most likely to carry them -------------
    corpus_text = ""
    for doc in documents:
        if doc.page_type in ("company_profile", "contact", "home"):
            corpus_text += "\n" + "\n".join(s.text for s in doc.sections)
            corpus_text += "\n" + (doc.meta.description or "")
            corpus_text += "\n" + "\n".join(
                doc.company_facts.get("raw_statutory_lines", []) or []
            )

    identifiers = extract_identifiers(corpus_text)
    addresses = extract_addresses(corpus_text)

    form_fields: list[str] = []
    for url, html in raw_html.items():
        if any(k in url for k in ("company-profile", "contact-us", "products.html")) or url.endswith("/"):
            form_fields = sorted(set(form_fields) | set(extract_inquiry_form_fields(html)))
            if form_fields:
                break

    company_name = None
    for doc in documents:
        if doc.company_facts.get("organization_name"):
            company_name = doc.company_facts["organization_name"]
            break
    if not company_name:
        for doc in documents:
            match = re.search(r"ANITA MEDICAL SYSTEMS[^\n]*", doc.meta.title or "")
            if match:
                company_name = "ANITA MEDICAL SYSTEMS PVT. LTD."
                break

    return SiteShared(
        company_name=company_name,
        navigation=nav,
        product_nav=[n for n in nav if re.search(r"-\d{5,7}\.html$", n["url"])],
        quick_links=[n for n in nav if "sitemap" in n["url"] or "products.html" in n["url"]],
        addresses=addresses,
        phone_numbers=identifiers["phones"],
        gst_numbers=identifiers["gst"],
        inquiry_form_fields=form_fields,
        source_url=documents[0].url,
    )


def _normalize_href(href: str) -> str | None:
    """Reduce a link to a site-relative ``.html`` path, or ``None`` to skip it."""
    if not href:
        return None
    path = href
    if href.startswith("http"):
        parsed = urlparse(href)
        if "anitamedicalsystems.com" not in (parsed.netloc or ""):
            return None
        path = parsed.path or "/"
    if not path.startswith("/"):
        return None
    # External utility paths and non-page assets are not navigation.
    if path.startswith(("/utils/", "/component/", "/Exporters_Suppliers/")):
        return None
    if not path.endswith(".html") and path != "/":
        return None
    return path


def _label_for(doc_set: list[PageDocument], href: str) -> str:
    """Best-effort human label for a navigation href."""
    slug = href.rstrip("/").rsplit("/", 1)[-1]
    slug = slug[:-5] if slug.endswith(".html") else slug
    if not slug:
        return "Home Page"
    return slug.replace("-", " ").title()


# ---------------------------------------------------------------------------
# Generic page parsing for the remaining page types
# ---------------------------------------------------------------------------
def parse_generic(
    soup: BeautifulSoup,
    url: str,
    page_type: str,
    *,
    content_selectors: tuple[str, ...] = (),
) -> PageDocument:
    """Parse a page that has no product-specific structure.

    Used by the home, contact, sitemap and catalog-index parsers: extract meta,
    headings, breadcrumb and content sections, then stop.
    """
    canonical_tag = soup.find("link", rel="canonical")
    title = soup.title.get_text().strip() if soup.title else ""
    meta = MetaTags(
        title=title,
        description=(
            soup.find("meta", attrs={"name": "description"}) or {}
        ).get("content") if soup.find("meta", attrs={"name": "description"}) else None,
        keywords=[
            k.strip()
            for k in (
                (soup.find("meta", attrs={"name": "keywords"}) or {}).get("content", "")
                if soup.find("meta", attrs={"name": "keywords"})
                else ""
            ).split(",")
            if k.strip()
        ],
        canonical=canonical_tag.get("href") if canonical_tag else None,
    )

    # Collect links BEFORE stripping chrome. The footer and "Our Products"
    # blocks are removed as prose, but they carry the catalogue navigation —
    # stripping first would silently drop those links from the graph and from
    # the derived site navigation.
    links_internal = sorted(
        {
            a["href"]
            for a in soup.find_all("a", href=True)
            if a["href"].startswith("/") or "anitamedicalsystems.com" in a["href"]
        }
    )
    links_external = sorted(
        {
            a["href"]
            for a in soup.find_all("a", href=True)
            if a["href"].startswith("http") and "anitamedicalsystems.com" not in a["href"]
        }
    )

    soup = strip_noise(soup)

    sections = []
    for selector in content_selectors:
        for element in soup.select(selector):
            text = drop_chrome_lines(smart_text(element))
            if text:
                sections.append(
                    {
                        "key": selector.strip(".#"),
                        "text": text,
                    }
                )

    # Whole-page text as a fallback content section. The body still contains
    # scattered widget markup, so known chrome lines are removed explicitly.
    if not sections:
        body = soup.find("body") or soup
        text = drop_chrome_lines(smart_text(body))
        if text:
            sections.append({"key": "page_text", "text": text})

    from ..models import Section

    built_sections = [
        Section(
            key=s["key"],
            heading=s["key"],
            text=s["text"],
            char_count=len(s["text"]),
        )
        for s in sections
    ]

    headings: dict[str, list[str]] = {}
    for level in ("h1", "h2", "h3"):
        values = [flat_text(h) for h in soup.find_all(level)]
        if values:
            headings[level] = [v for v in values if v]

    full_text = "\n".join(s.text for s in built_sections)
    facts = extract_identifiers(full_text)

    return PageDocument(
        doc_id=f"{page_type}:{urlparse(url).path or '/'}",
        url=url,
        canonical_url=meta.canonical,
        page_type=page_type,  # type: ignore[arg-type]
        title=meta.title,
        crawl=CrawlMeta(
            url=url,
            final_url=url,
            status=200,
            fetched_at=datetime.now(timezone.utc),
            content_sha256="",
            bytes=0,
        ),
        meta=meta,
        headings=headings,
        sections=built_sections,
        company_facts=facts,
        links_internal=links_internal,
        links_external=links_external,
        boilerplate_ref="site_shared:v1",
    )
