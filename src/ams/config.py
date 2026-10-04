"""Configuration loading and environment fixes.

Single source of truth for paths and site settings. All entry-point scripts
call :func:`force_utf8` first — see the note on Windows consoles below.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# src/ams/config.py -> src/ams -> src -> <project root>
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "site.yaml"


def force_utf8() -> None:
    """Force UTF-8 on stdout/stderr.

    Windows consoles default to cp1252, which raises UnicodeEncodeError on
    characters that occur in this site's markup (e.g. ``✕`` U+2715). Without
    this, a script can abort partway through printing a report.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            # Not a TextIOWrapper (already wrapped, or redirected) — nothing to do.
            pass


@dataclass(frozen=True)
class CrawlSettings:
    concurrency: int
    delay_seconds: float
    timeout_seconds: float
    max_retries: int
    backoff_seconds: list[float]
    circuit_breaker_consecutive_failures: int
    cache_first: bool


@dataclass(frozen=True)
class SiteSettings:
    name: str
    base_url: str
    user_agent: str
    sitemap_path: str
    robots_path: str


@dataclass(frozen=True)
class Config:
    """Parsed ``config/site.yaml`` plus resolved absolute paths."""

    site: SiteSettings
    crawl: CrawlSettings
    page_types: list[dict[str, Any]]
    content: dict[str, Any]
    media: dict[str, Any]
    raw_paths: dict[str, str] = field(default_factory=dict)

    # -- path helpers ------------------------------------------------------
    def path(self, key: str) -> Path:
        """Resolve a key from the ``paths:`` block to an absolute path."""
        if key not in self.raw_paths:
            raise KeyError(f"Unknown path key: {key!r}")
        return (PROJECT_ROOT / self.raw_paths[key]).resolve()

    def ensure_dirs(self) -> None:
        """Create every configured output directory."""
        for key in ("raw", "pages", "out", "tables", "reports"):
            self.path(key).mkdir(parents=True, exist_ok=True)

    @property
    def allowed_host(self) -> str:
        """Hostname that internal links must match to be crawled."""
        return self.site.base_url.split("://", 1)[-1].rstrip("/")


@lru_cache(maxsize=1)
def load_config(config_path: Path | None = None) -> Config:
    """Load and cache the site configuration."""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")

    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    site_raw = data["site"]
    crawl_raw = data["crawl"]

    return Config(
        site=SiteSettings(
            name=site_raw["name"],
            base_url=site_raw["base_url"].rstrip("/"),
            user_agent=site_raw["user_agent"],
            sitemap_path=site_raw["sitemap_path"],
            robots_path=site_raw["robots_path"],
        ),
        crawl=CrawlSettings(
            concurrency=int(crawl_raw["concurrency"]),
            delay_seconds=float(crawl_raw["delay_seconds"]),
            timeout_seconds=float(crawl_raw["timeout_seconds"]),
            max_retries=int(crawl_raw["max_retries"]),
            backoff_seconds=[float(x) for x in crawl_raw["backoff_seconds"]],
            circuit_breaker_consecutive_failures=int(
                crawl_raw["circuit_breaker_consecutive_failures"]
            ),
            cache_first=bool(crawl_raw["cache_first"]),
        ),
        page_types=data.get("page_types", []),
        content=data.get("content", {}),
        media=data.get("media", {}),
        raw_paths=data.get("paths", {}),
    )
