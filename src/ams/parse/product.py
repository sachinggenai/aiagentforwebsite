"""Product detail page parser.

Container anchors below are **verified against the live pages**, not inferred:

===========================  ==========================================
``.product_title``           product ``<h1>``
``.product_price_div``       ``<h2>Price 2500 INR / Piece</h2>``
``.pro_det_other``           holds the Spec + Trade headings and blocks
``.pro_details-div``         one per block; each ``.pro_details_list`` is a
                             ``<ul>`` of exactly two ``<li>`` — label, value
``.pro_det_about``           About prose **and** the FAQ block, with the
                             inquiry modal excluded
``.container4 .expolor_div`` related products
===========================  ==========================================

Two traps this parser exists to avoid:

* **The spec block is optional.** Five of 24 products have no spec pairs, and
  two carry their real specifications in a "Technical Details" table instead.
  Missing specs must be recorded as absent, never silently dropped.
* **FAQ pairs can exist without an "FAQs of" heading.** Discovery therefore keys
  on ``<h3>`` elements whose text begins with ``Q:`` — never on the heading.
  Related-product names are also ``<h3>``, so keying on heading level alone
  invents roughly four fake FAQs per page.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

from ..dom import (
    flat_text,
    meta_content,
    sha256_text,
    smart_text,
    strip_noise,
)
from ..models import (
    CrawlMeta,
    FaqItem,
    MediaItem,
    MetaTags,
    Offer,
    PageDocument,
    Section,
    SpecItem,
)
from ..normalize import (
    clean_text,
    parse_price,
    parse_quantity,
    product_id_from_url,
    strip_qa_prefix,
    tidy_punctuation,
)
from .jsonld import (
    extract_breadcrumb,
    extract_og_meta,
    extract_product,
    looks_corrupted,
)

CPIMG_RE = re.compile(
    r"https://cpimg\.tistatic\.com/(?P<pid>\d{8})/(?P<variant>[a-z]/\d)/(?P<name>[^\"'\s)]+)"
)
TITLE_SUFFIXES = (
    " at Best Prices - Durable, Reliable & Versatile",
    " - Best Price in Mumbai",
    " - Best Price in India",
    " at Best Price in Mumbai",
)


def _clean_title(raw: str | None) -> str:
    """Strip the marketing suffix the site appends to product titles."""
    text = clean_text(raw or "")
    for suffix in TITLE_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text.strip(" -–|")


def _extract_spec_blocks(root: Tag) -> tuple[list[SpecItem], list[SpecItem]]:
    """Return ``(specification_pairs, trade_pairs)``.

    Each ``.pro_details_list`` is a two-item ``<ul>`` of label then value.
    Blocks are labelled from the nearest preceding ``<h2>``, so a template
    reorder cannot silently swap specifications for trade terms.
    """
    specs: list[SpecItem] = []
    trade: list[SpecItem] = []
    if root is None:
        return specs, trade

    for block in root.select(".pro_details-div"):
        heading = block.find_previous(["h2", "h3"])
        heading_text = flat_text(heading).casefold() if heading else ""
        target = trade if "trade" in heading_text else specs

        for group in block.select(".pro_details_list"):
            items = group.find_all("li", recursive=False) or group.find_all("li")
            texts = [clean_text(li.get_text(" ", strip=True)) for li in items]
            texts = [t for t in texts if t]
            if len(texts) >= 2:
                target.append(
                    SpecItem(name=texts[0], value=tidy_punctuation(texts[1]))
                )
            elif len(texts) == 1:
                # A lone item carries a "Label: value" pair.
                from ..normalize import split_label_value

                pair = split_label_value(texts[0])
                if pair:
                    target.append(SpecItem(name=pair[0], value=pair[1]))
    return specs, trade


def _extract_table_specs(container: Tag) -> list[SpecItem]:
    """Fallback: specifications held in a ``<table>``.

    Some products have an empty standard spec block and carry their real
    specifications in a "Technical Details" table (a Google-Sheets HTML export).
    Rows are read positionally as label/value.
    """
    specs: list[SpecItem] = []
    if container is None:
        return specs
    for table in container.find_all("table"):
        for row in table.find_all("tr"):
            cells = [
                clean_text(cell.get_text(" ", strip=True))
                for cell in row.find_all(["td", "th"])
            ]
            cells = [c for c in cells if c]
            if len(cells) >= 2 and len(cells[0]) < 60:
                specs.append(SpecItem(name=cells[0], value=cells[1]))
    return specs


def _extract_faqs(container: Tag, product_id: str | None, url: str) -> list[FaqItem]:
    """Extract FAQ pairs by structure: an ``<h3>`` starting with ``Q:`` followed
    by the answer, which the site places in the next sibling."""
    faqs: list[FaqItem] = []
    if container is None:
        return faqs

    for heading in container.find_all("h3"):
        question = flat_text(heading)
        if not question.upper().startswith("Q:"):
            continue
        answer_parts: list[str] = []
        for sibling in heading.next_siblings:
            if isinstance(sibling, Tag) and sibling.name in ("h3", "h2"):
                break
            chunk = smart_text(sibling) if isinstance(sibling, Tag) else str(sibling)
            if chunk:
                answer_parts.append(chunk)
        answer = " ".join(part.strip() for part in answer_parts if part.strip())
        question = strip_qa_prefix(question)
        answer = strip_qa_prefix(clean_text(answer))
        if question and answer:
            faqs.append(
                FaqItem(
                    question=question,
                    answer=answer,
                    source="site",
                    product_id=product_id,
                    url=url,
                )
            )
    return faqs


def _about_text(container: Tag) -> str:
    """Return the About prose with the FAQ block removed.

    The FAQ block is stripped from a copy so prose and Q&A never overlap — an
    About chunk containing questions would match FAQ queries twice.
    """
    if container is None:
        return ""

    clone = BeautifulSoup(str(container), "lxml").find(
        True, class_=container.get("class")
    ) or BeautifulSoup(str(container), "lxml")

    faq_heading = None
    for heading in clone.find_all(["h2", "h3"]):
        if "faqs of" in flat_text(heading).casefold():
            faq_heading = heading
            break

    if faq_heading is not None:
        for sibling in list(faq_heading.next_siblings):
            if isinstance(sibling, Tag):
                sibling.decompose()
            else:
                sibling.extract()
        faq_heading.decompose()
    else:
        for heading in clone.find_all("h3"):
            if flat_text(heading).upper().startswith("Q:"):
                for sibling in list(heading.next_siblings):
                    if isinstance(sibling, Tag) and sibling.name in ("h3", "h2"):
                        break
                    if isinstance(sibling, Tag):
                        sibling.decompose()
                    else:
                        sibling.extract()
                heading.decompose()

    text = smart_text(clone)
    # Drop the leading "X-Ray Cassette About" heading echo.
    text = re.sub(r"^.{0,80}?\bAbout\b\s*", "", text, count=1).strip()
    return text


def _extract_price(soup: BeautifulSoup) -> tuple[float | None, str | None, str | None]:
    """Read the price from ``.product_price_div``.

    The JSON-LD price is preferred elsewhere because it is already typed; this is
    the DOM fallback and the source of the human-readable unit.
    """
    block = soup.select_one(".product_price_div")
    if not block:
        return None, None, None
    text = flat_text(block)
    match = re.search(r"([\d][\d,]*)", text)
    amount = int(match.group(1).replace(",", "")) if match else None
    unit = None
    unit_match = re.search(r"INR\s*/\s*([A-Za-z]+)", text)
    if unit_match:
        unit = unit_match.group(1)
    return amount, text, unit


def _extract_media(soup: BeautifulSoup, product_id: str | None, jsonld_image: str | None) -> list[MediaItem]:
    """Collect image references as a manifest (no downloads).

    Variants genuinely differ per page (``b/3``, ``b/4``, ``b/5`` all occur), so
    URLs are read from the markup as-is and never synthesised from a template.
    """
    found: dict[str, MediaItem] = {}

    for match in CPIMG_RE.finditer(str(soup)):
        pid = match.group("pid")
        url = match.group(0)
        key = f"{pid}/{match.group('name')}"
        # Product ids on this site are zero-padded to 8 digits in image URLs.
        is_current = bool(product_id) and pid.lstrip("0") == product_id.lstrip("0")
        role = "primary" if is_current else "related"
        if key not in found:
            found[key] = MediaItem(url=url, role=role, product_id=pid)
        found[key].variants[match.group("variant")] = url

    if jsonld_image and jsonld_image not in {m.url for m in found.values()}:
        pid_match = CPIMG_RE.search(jsonld_image)
        found[f"jsonld/{jsonld_image}"] = MediaItem(
            url=jsonld_image,
            role="primary",
            product_id=pid_match.group("pid") if pid_match else product_id,
        )

    # Mark the JSON-LD image as primary and anything sharing its id as a gallery shot.
    for item in found.values():
        if jsonld_image and item.url == jsonld_image:
            item.role = "primary"

    return list(found.values())


def _extract_related(soup: BeautifulSoup) -> list[dict[str, str]]:
    """Related products from the "More Products in ... Category" widget."""
    related: list[dict[str, str]] = []
    seen: set[str] = set()
    # Scope strictly to the related-products widget. Falling back to the whole
    # document would sweep in navigation and footer links as "related".
    scope = soup.select_one(".container4") or soup.select_one(".expolore")
    if scope is None:
        return related
    for anchor in scope.select("a[href]"):
        href = anchor.get("href", "")
        if not re.search(r"-\d{5,7}\.html$", href):
            continue
        name = clean_text(anchor.get_text(" ", strip=True))
        if not name or href in seen:
            continue
        seen.add(href)
        related.append({"name": name, "url": href})
    return related


def parse(soup: BeautifulSoup, url: str) -> PageDocument:
    """Parse a product detail page."""
    # ORDER MATTERS: JSON-LD lives inside <script> tags, and strip_noise()
    # decomposes those. Extract structured data and microdata first, then strip.
    product_id = product_id_from_url(url)
    structured, warnings = extract_product(soup)
    og, twitter = extract_og_meta(soup)
    breadcrumb = extract_breadcrumb(soup)
    canonical_tag = soup.find("link", rel="canonical")

    # Collect links BEFORE stripping chrome, so the footer and "Our Products"
    # navigation still contributes its links even though its prose is removed.
    links_internal: list[str] = []
    links_external: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if href.startswith("/") or "anitamedicalsystems.com" in href:
            links_internal.append(href)
        elif href.startswith("http"):
            links_external.append(href)

    soup = strip_noise(soup)

    title_block = soup.select_one(".product_title")
    h1 = soup.find("h1")
    name = clean_text((title_block or h1).get_text(" ", strip=True)) if (title_block or h1) else ""
    if not name and structured:
        name = structured.name or ""
    if not name:
        name = _clean_title(meta_content(soup, name="title"))

    spec_root = soup.select_one(".pro_det_other")
    specs, trade = _extract_spec_blocks(spec_root)

    about_root = soup.select_one(".pro_det_about")
    about_text = _about_text(about_root)
    faqs = _extract_faqs(about_root, product_id, url)

    # Some products genuinely have no About prose on the site — the container
    # holds only its own heading. That is source data absence, not an extraction
    # failure, and recording it as such keeps the validation gate meaningful
    # (it must only fail when *we* dropped something).
    if not about_text:
        if about_root is None:
            warnings.append("section_absent_on_source:about")
        else:
            container_text = flat_text(about_root)
            if len(container_text) <= 80:
                warnings.append("section_absent_on_source:about")
    if not specs and not trade:
        warnings.append("section_absent_on_source:specs")

    # Fall back to a "Technical Details" table when the standard block is empty.
    if not specs and about_root is not None:
        table_specs = _extract_table_specs(about_root)
        if table_specs:
            specs = table_specs
            warnings.append(
                f"specs_from_table_fallback: standard spec block empty; "
                f"recovered {len(table_specs)} rows from a Technical Details table"
            )

    dom_price, price_text, price_unit = _extract_price(soup)
    offer: Offer | None = structured.offer if structured else None
    if offer is None and dom_price is not None:
        money = parse_price(str(dom_price), unit=price_unit)
        offer = Offer(
            price=money.amount_int,
            price_text=price_text,
            currency=money.currency,
            price_unit=price_unit,
        )
    elif offer is not None:
        if offer.price is None and dom_price is not None:
            offer.price = dom_price
        if offer.price_unit is None:
            offer.price_unit = price_unit

    # Cross-check the JSON-LD prose against the DOM: the DOM version is clean.
    if structured and structured.description and looks_corrupted(structured.description):
        warnings.append("jsonld_description_ignored_for_prose")

    media = _extract_media(soup, product_id, structured.image if structured else None)
    related = _extract_related(soup)

    # The trade block is the authoritative source for terms the JSON-LD omits.
    if offer is not None and trade:
        trade_by_name = {s.name.casefold(): s.value for s in trade}
        if offer.moq_value is None:
            raw_moq = trade_by_name.get("minimum order quantity")
            if raw_moq:
                qty = parse_quantity(raw_moq)
                offer.moq_value = qty.value if qty.value is not None else qty.value_min
                offer.moq_unit = offer.moq_unit or qty.unit
        offer.delivery_lead_time = offer.delivery_lead_time or trade_by_name.get("delivery time")
        offer.supply_ability = offer.supply_ability or trade_by_name.get("supply ability")
        offer.sample_available = offer.sample_available or trade_by_name.get("sample available")
        offer.sample_policy = offer.sample_policy or trade_by_name.get("sample policy")
        offer.main_domestic_market = offer.main_domestic_market or trade_by_name.get(
            "main domestic market"
        )

    category_name = None
    for crumb in breadcrumb:
        if crumb.url and "products.html" not in crumb.url and crumb.name.casefold() not in ("home", name.casefold()):
            category_name = crumb.name
    if category_name is None and structured and structured.category:
        category_name = structured.category

    keywords = meta_content(soup, name="keywords") or ""
    meta = MetaTags(
        title=meta_content(soup, name="title") or clean_text(soup.title.get_text() if soup.title else ""),
        description=meta_content(soup, name="description"),
        keywords=[k.strip() for k in keywords.split(",") if k.strip()],
        canonical=canonical_tag.get("href") if canonical_tag else None,
        robots=meta_content(soup, name="robots"),
        og=og,
        twitter=twitter,
    )

    sections: list[Section] = []
    if specs:
        text = "\n".join(f"{s.name}: {s.value}" for s in specs)
        sections.append(
            Section(
                key="specification",
                heading=f"{name} Specification",
                text=text,
                text_sha256=sha256_text(text),
                char_count=len(text),
            )
        )
    if trade:
        text = "\n".join(f"{s.name}: {s.value}" for s in trade)
        sections.append(
            Section(
                key="trade_information",
                heading=f"{name} Trade Information",
                text=text,
                text_sha256=sha256_text(text),
                char_count=len(text),
            )
        )
    if about_text:
        sections.append(
            Section(
                key="about",
                heading=f"{name} About",
                text=about_text,
                text_sha256=sha256_text(about_text),
                char_count=len(about_text),
            )
        )

    headings: dict[str, list[str]] = {}
    for level in ("h1", "h2", "h3"):
        values = [flat_text(h) for h in soup.find_all(level)]
        if values:
            headings[level] = [v for v in values if v]

    return PageDocument(
        doc_id=f"product:{product_id or url}",
        url=url,
        canonical_url=meta.canonical,
        page_type="product",
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
        breadcrumb=breadcrumb,
        headings=headings,
        sections=sections,
        product_id=product_id,
        product_name=name or None,
        structured=structured,
        specs=specs,
        offer=offer,
        faqs=faqs,
        media=media,
        category_name=category_name,
        links_internal=sorted(set(links_internal)),
        links_external=sorted(set(links_external)),
        relations={
            "category": category_name,
            "related_products": related,
            "brand": structured.brand if structured else None,
        },
        boilerplate_ref="site_shared:v1",
        parse_warnings=warnings,
    )
