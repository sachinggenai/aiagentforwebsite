"""Media manifest.

**Manifest only** — no binaries are downloaded, per the agreed scope. The
manifest records every image reference, its role, its product, and the size
variants the CDN exposes, so a later multimodal phase can fetch exactly what it
needs without re-crawling.

Note that size variants genuinely differ per page (``b/3``, ``b/4`` and ``b/5``
all occur), so URLs are recorded as they appear in the markup and never
synthesised from a template.
"""

from __future__ import annotations

import re

from ..config import Config
from ..models import PageDocument, SiteShared
from . import write_json

IMAGE_RE = re.compile(
    r"https?://[^\"'\s)]+\.(?:jpg|jpeg|png|gif|webp|svg)", re.IGNORECASE
)


def _is_template(url: str) -> bool:
    markers = (
        "/catalogs/template",
        "/new_website1/",
        "/catalogs/js/",
        "/common-icons/",
        "/favicon/",
    )
    return any(marker in url for marker in markers)


def build_manifest(
    documents: list[PageDocument], shared: SiteShared, raw_html: dict[str, str]
) -> dict:
    """Build the media manifest from parsed documents and raw markup."""
    product_images: dict[str, dict] = {}
    template_images: dict[str, dict] = {}
    by_product: dict[str, list[str]] = {}

    for doc in documents:
        for item in doc.media:
            key = item.url
            if _is_template(item.url):
                template_images.setdefault(
                    key,
                    {"url": item.url, "role": "template", "found_on": []},
                )["found_on"].append(doc.url)
                continue

            entry = product_images.setdefault(
                key,
                {
                    "url": item.url,
                    "role": item.role,
                    "product_id": item.product_id,
                    "variants": {},
                    "alt": item.alt,
                    "found_on": [],
                },
            )
            entry["variants"].update(item.variants)
            if doc.url not in entry["found_on"]:
                entry["found_on"].append(doc.url)
            if item.role == "primary":
                entry["role"] = "primary"
            if doc.product_id:
                by_product.setdefault(doc.product_id, [])
                if item.url not in by_product[doc.product_id]:
                    by_product[doc.product_id].append(item.url)

    # Any image referenced in markup but not captured structurally.
    for url, html in raw_html.items():
        for match in IMAGE_RE.finditer(html):
            image_url = match.group(0)
            if _is_template(image_url) or image_url in product_images:
                continue
            product_images[image_url] = {
                "url": image_url,
                "role": "unclassified",
                "product_id": None,
                "variants": {},
                "alt": None,
                "found_on": [url],
            }

    unique_products = sorted({k for k in by_product if k})
    return {
        "note": (
            "Manifest only — no image binaries were downloaded. "
            "Set media.download: true in config/site.yaml to fetch them later."
        ),
        "counts": {
            "product_images": len(product_images),
            "template_images": len(template_images),
            "products_with_images": len(unique_products),
        },
        "size_variants_observed": sorted(
            {v for e in product_images.values() for v in e.get("variants", {})}
        ),
        "product_images": sorted(product_images.values(), key=lambda e: e["url"]),
        "template_images": sorted(template_images.values(), key=lambda e: e["url"]),
        "images_by_product": by_product,
    }


def emit(
    config: Config,
    documents: list[PageDocument],
    shared: SiteShared,
    raw_html: dict[str, str] | None = None,
) -> dict[str, int]:
    """Write ``media_manifest.json``."""
    manifest = build_manifest(documents, shared, raw_html or {})
    write_json(config.path("out") / "media_manifest.json", manifest)
    return {"product_images": manifest["counts"]["product_images"]}
