"""Pipeline orchestration: parse cached pages into canonical documents.

Reads the raw cache, dispatches each page to its parser, attaches real crawl
provenance, and writes one JSON document per page plus the site-wide shared
block. Emitters read only from ``data/pages/`` — they never touch HTML.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .dom import make_soup
from .fetch import iter_cached_pages
from .models import CrawlMeta, PageDocument, SiteShared
from .parse import classify
from .parse import parse_page as dispatch_parse
from .parse.shared import build_site_shared


@dataclass
class ParseReport:
    """Outcome of a parse run."""

    parsed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    by_type: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.parsed) + len(self.failed)


def parse_all(config: Config, *, limit: int | None = None) -> tuple[list[PageDocument], ParseReport]:
    """Parse every page in the raw cache.

    Returns the documents and a report. A page that fails to parse is recorded
    with its error rather than aborting the run — a single malformed page must
    not cost the whole corpus, and the failure belongs in the validation report.
    """
    config.ensure_dirs()
    pages = [
        (url, html)
        for url, html in iter_cached_pages(config)
        # robots.txt and sitemap.xml are support files, not content pages. They
        # are read by discovery and the API inventory; parsing them as pages
        # would add meaningless entries to the corpus.
        if not url.rstrip("/").endswith(("robots.txt", "sitemap.xml"))
    ]
    if limit:
        pages = pages[:limit]

    meta_by_url: dict[str, dict] = {}
    raw_html: dict[str, str] = {}
    for url, html in pages:
        raw_html[url] = html

    # Recover the stored CrawlMeta for provenance.
    raw_dir = config.path("raw")
    for meta_path in raw_dir.glob("*.meta.json"):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        meta_by_url[data.get("url", "")] = data

    documents: list[PageDocument] = []
    report = ParseReport()

    for url, html in pages:
        try:
            doc = dispatch_parse(make_soup(html), url)
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            report.failed[url] = f"{type(exc).__name__}: {exc}"
            continue

        stored = meta_by_url.get(url, {})
        doc.crawl = CrawlMeta(
            url=url,
            final_url=stored.get("final_url", url),
            status=int(stored.get("status", 200)),
            fetched_at=_parse_dt(stored.get("fetched_at")),
            content_sha256=stored.get("content_sha256", ""),
            bytes=int(stored.get("bytes", len(html.encode("utf-8", "replace")))),
            etag=stored.get("etag"),
            last_modified=stored.get("last_modified"),
            from_cache=True,
            attempt=int(stored.get("attempt", 1)),
        )
        doc.page_type = classify(url, config)  # type: ignore[assignment]

        documents.append(doc)
        report.parsed.append(url)
        report.by_type[doc.page_type] = report.by_type.get(doc.page_type, 0) + 1

    _write_documents(config, documents)
    if documents:
        _write_shared(config, build_site_shared(documents, raw_html))

    return documents, report


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _write_documents(config: Config, documents: list[PageDocument]) -> None:
    pages_dir = config.path("pages")
    pages_dir.mkdir(parents=True, exist_ok=True)
    for doc in documents:
        name = doc.doc_id.replace(":", "__").replace("/", "_")
        (pages_dir / f"{name}.json").write_text(
            doc.model_dump_json(indent=2), encoding="utf-8"
        )


def _write_shared(config: Config, shared: SiteShared) -> None:
    path = config.path("site_shared")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(shared.model_dump_json(indent=2), encoding="utf-8")


def load_documents(config: Config) -> list[PageDocument]:
    """Load every parsed page document."""
    pages_dir = config.path("pages")
    documents: list[PageDocument] = []
    for path in sorted(pages_dir.glob("*.json")):
        try:
            documents.append(PageDocument.model_validate_json(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return documents


def load_shared(config: Config) -> SiteShared:
    """Load the shared site block, or an empty one if absent."""
    path = config.path("site_shared")
    if path.exists():
        try:
            return SiteShared.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return SiteShared()
