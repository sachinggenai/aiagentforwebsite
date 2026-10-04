"""Canonical data model.

Every parser produces a :class:`PageDocument`; every emitter reads them. Keeping
one schema in the middle means adding an output format never touches parsing, and
fixing a parser never touches an output format.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

PageType = Literal[
    "product",
    "category",
    "products_index",
    "company_profile",
    "contact",
    "sitemap_page",
    "home",
    "other",
]

MediaRole = Literal[
    "primary",      # the product's own image (from JSON-LD)
    "gallery",      # additional images for the same product
    "related",      # a different product's image shown on this page
    "logo",
    "favicon",
    "template",     # TradeIndia chrome — recorded but not content
]


# ---------------------------------------------------------------------------
# Crawl / page metadata
# ---------------------------------------------------------------------------
class CrawlMeta(BaseModel):
    """Provenance for one fetched page."""

    url: str
    final_url: str
    status: int
    fetched_at: datetime
    content_sha256: str
    bytes: int
    etag: str | None = None
    last_modified: str | None = None
    from_cache: bool = False
    attempt: int = 1


class MetaTags(BaseModel):
    title: str | None = None
    description: str | None = None
    keywords: list[str] = Field(default_factory=list)
    canonical: str | None = None
    robots: str | None = None
    og: dict[str, str] = Field(default_factory=dict)
    twitter: dict[str, str] = Field(default_factory=dict)


class BreadcrumbItem(BaseModel):
    position: int | None = None
    name: str
    url: str | None = None


# ---------------------------------------------------------------------------
# Structured content
# ---------------------------------------------------------------------------
class SpecItem(BaseModel):
    """One specification row (long format — spec names vary per product)."""

    name: str
    value: str


class Offer(BaseModel):
    """Pricing / trade terms, normalised from the JSON-LD ``offers`` block."""

    price: float | None = None
    price_text: str | None = None
    currency: str | None = None
    price_unit: str | None = None          # e.g. "Piece"
    availability: str | None = None        # "InStock"
    item_condition: str | None = None      # "New"
    delivery_lead_time: str | None = None  # "7 Days"
    moq_value: float | None = None
    moq_unit: str | None = None
    supply_ability: str | None = None
    sample_available: str | None = None
    sample_policy: str | None = None
    main_domestic_market: str | None = None


class FaqItem(BaseModel):
    question: str
    answer: str
    source: Literal["site", "derived"] = "site"
    product_id: str | None = None
    url: str | None = None


class MediaItem(BaseModel):
    url: str
    role: MediaRole = "template"
    alt: str | None = None
    product_id: str | None = None
    # Verified size variants, e.g. {"b/3": "<url>", "s/3": "<url>"} — manifest only.
    variants: dict[str, str] = Field(default_factory=dict)


class Section(BaseModel):
    """A named content region, already HTML-stripped and normalised."""

    key: str
    heading: str
    text: str
    text_sha256: str | None = None
    char_count: int = 0


class ProductStructured(BaseModel):
    """The page's schema.org Product JSON-LD, unpacked."""

    name: str | None = None
    description: str | None = None
    image: str | None = None
    url: str | None = None
    brand: str | None = None
    category: str | None = None
    sku: str | None = None
    specs: list[SpecItem] = Field(default_factory=list)
    offer: Offer | None = None


# ---------------------------------------------------------------------------
# The page document
# ---------------------------------------------------------------------------
class PageDocument(BaseModel):
    """Everything extracted from one page."""

    doc_id: str
    url: str
    canonical_url: str | None = None
    page_type: PageType = "other"
    title: str | None = None

    crawl: CrawlMeta
    meta: MetaTags = Field(default_factory=MetaTags)

    breadcrumb: list[BreadcrumbItem] = Field(default_factory=list)
    headings: dict[str, list[str]] = Field(default_factory=dict)
    sections: list[Section] = Field(default_factory=list)

    # Product-specific
    product_id: str | None = None
    product_name: str | None = None
    structured: ProductStructured | None = None
    specs: list[SpecItem] = Field(default_factory=list)
    offer: Offer | None = None
    faqs: list[FaqItem] = Field(default_factory=list)
    media: list[MediaItem] = Field(default_factory=list)

    # Category / index / company
    category_name: str | None = None
    listed_products: list[dict[str, Any]] = Field(default_factory=list)
    company_facts: dict[str, Any] = Field(default_factory=dict)
    contact_facts: dict[str, Any] = Field(default_factory=dict)

    links_internal: list[str] = Field(default_factory=list)
    links_external: list[str] = Field(default_factory=list)
    relations: dict[str, Any] = Field(default_factory=dict)

    # Set when site-wide blocks (nav/footer/address/inquiry modal) were stripped,
    # so consumers know to resolve them from site_shared.json.
    boilerplate_ref: str | None = None
    parse_warnings: list[str] = Field(default_factory=list)

    def section_text(self, key: str) -> str:
        """Return the text of a named section, or empty string."""
        for section in self.sections:
            if section.key == key:
                return section.text
        return ""


# ---------------------------------------------------------------------------
# Site-wide shared blocks (extracted ONCE — see plan hazard 2)
# ---------------------------------------------------------------------------
class SiteShared(BaseModel):
    """Blocks that repeat on every page, stored once to avoid retrieval pollution."""

    doc_id: str = "site_shared:v1"
    company_name: str | None = None
    navigation: list[dict[str, str]] = Field(default_factory=list)
    product_nav: list[dict[str, str]] = Field(default_factory=list)
    quick_links: list[dict[str, str]] = Field(default_factory=list)
    addresses: dict[str, str] = Field(default_factory=dict)
    phone_numbers: list[str] = Field(default_factory=list)
    gst_numbers: list[str] = Field(default_factory=list)
    footer_text: str | None = None
    inquiry_form_fields: list[str] = Field(default_factory=list)
    source_url: str | None = None


# ---------------------------------------------------------------------------
# Discovery / endpoints
# ---------------------------------------------------------------------------
class UrlEntry(BaseModel):
    url: str
    path: str
    page_type: PageType = "other"
    source: Literal["sitemap", "link", "both"] = "sitemap"
    priority: float | None = None


class UrlManifest(BaseModel):
    generated_at: datetime
    base_url: str
    total: int
    by_type: dict[str, int] = Field(default_factory=dict)
    entries: list[UrlEntry] = Field(default_factory=list)
    sitemap_only: list[str] = Field(default_factory=list)
    link_only: list[str] = Field(default_factory=list)


class ApiEndpoint(BaseModel):
    """A form/AJAX endpoint discovered by static analysis. Never invoked."""

    path: str
    kind: Literal["form_action", "ajax", "popup", "link", "robots_disallow"]
    method: str | None = None
    parameters: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)
    found_on: list[str] = Field(default_factory=list)
    robots_allowed: bool = True
    purpose: str | None = None
    notes: str | None = None
