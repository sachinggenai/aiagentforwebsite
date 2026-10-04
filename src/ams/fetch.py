"""HTTP fetching: cache-first, polite, and block-aware.

The host returns HTTP 403 under sustained crawling — observed live during
reconnaissance, and it persisted through repeated retries. Two consequences
shape this module:

1. **Every 2xx response is cached to disk before anything else happens.** A block
   that starts mid-crawl can therefore never cost us pages we already have; a
   later `--resume` picks up exactly where it stopped.
2. **Failures back off and then abort the run**, rather than hammering the host.
   Burning a longer block to fetch nothing is strictly worse than stopping.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import Config
from .models import CrawlMeta

# Statuses that mean "slow down / stop", as opposed to "this page is broken".
TRANSIENT_STATUSES = {403, 429, 500, 502, 503, 504}


class CircuitBreakerTripped(RuntimeError):
    """Raised when too many consecutive failures suggest we are being blocked."""


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    text: str
    meta: CrawlMeta
    from_cache: bool


def safe_filename(url: str) -> str:
    """Map a URL to a stable, filesystem-safe basename.

    ``https://host/`` -> ``index.html``; ``/a/b.html`` -> ``a__b.html``.
    """
    path = urlparse(url).path.strip("/")
    if not path:
        return "index.html"
    name = path.replace("/", "__")
    # Strip characters Windows forbids in filenames.
    name = re.sub(r'[<>:"\\|?*]', "_", name)
    if not name.endswith(".html"):
        name += ".html"
    return name


class RobotsPolicy:
    """Minimal robots.txt matcher — enough for this site's rule shape."""

    def __init__(self, disallow: list[str], allow: list[str] | None = None) -> None:
        self.disallow = [d for d in disallow if d]
        self.allow = allow or []

    @classmethod
    def parse(cls, text: str, user_agent: str = "*") -> RobotsPolicy:
        """Parse robots.txt, collecting rules from **every** user-agent group.

        This site's robots.txt is mis-authored: the ``Disallow`` block follows
        ``User-agent: Mediapartners-Google`` with no intervening ``User-agent``
        line, so a strict group-semantics reading applies those rules *only* to
        Mediapartners-Google and leaves ``*`` unrestricted.

        We deliberately read past that. The list names the inquiry, OTP,
        callback and login machinery — treating the site's clear intent as
        binding is the conservative choice, and the cost is merely not crawling
        pages we have no need for.
        """
        disallow: list[str] = []
        allow: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key == "disallow" and value:
                disallow.append(value)
            elif key == "allow" and value:
                allow.append(value)
        return cls(disallow, allow)

    def is_allowed(self, path: str) -> bool:
        """True when ``path`` may be fetched.

        Longest-match wins, so a specific ``Allow`` can override a broad
        ``Disallow`` — consistent with how search engines interpret robots.txt.
        """
        best_len = -1
        decision = True
        for rule in self.disallow:
            if rule != "/" and path.startswith(rule) or rule == "/":
                if len(rule) > best_len:
                    best_len, decision = len(rule), False
        for rule in self.allow:
            if path.startswith(rule) and len(rule) > best_len:
                best_len, decision = len(rule), True
        return decision


class Fetcher:
    """Cache-first HTTP client with a politeness delay and a circuit breaker."""

    def __init__(self, config: Config, force: bool = False) -> None:
        self.config = config
        self.force = force
        self.raw_dir = config.path("raw")
        self.raw_dir.mkdir(parents=True, exist_ok=True)

        self._client = httpx.Client(
            headers={
                "User-Agent": config.site.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-IN,en;q=0.9",
            },
            timeout=config.crawl.timeout_seconds,
            follow_redirects=True,
        )
        self._last_request_at: float | None = None
        self.consecutive_failures = 0
        self.stats = {
            "cache_hits": 0,
            "live_fetches": 0,
            "failures": 0,
            "robots_blocked": 0,
        }
        self.robots: RobotsPolicy | None = None

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- cache -------------------------------------------------------------
    def _paths_for(self, url: str) -> tuple[Path, Path]:
        name = safe_filename(url)
        return self.raw_dir / name, self.raw_dir / f"{name}.meta.json"

    def _read_cache(self, url: str) -> FetchResult | None:
        html_path, meta_path = self._paths_for(url)
        if not (html_path.exists() and meta_path.exists()):
            return None
        try:
            meta = CrawlMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
            # Only a stored success is a usable cache entry.
            if not (200 <= meta.status < 300):
                return None
            text = html_path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None
        meta.from_cache = True
        return FetchResult(
            url=url,
            final_url=meta.final_url,
            status=meta.status,
            text=text,
            meta=meta,
            from_cache=True,
        )

    def _write_cache(self, result: FetchResult) -> None:
        html_path, meta_path = self._paths_for(result.url)
        html_path.write_text(result.text, encoding="utf-8")
        meta = result.meta.model_copy()
        meta.from_cache = False
        meta_path.write_text(
            meta.model_dump_json(indent=2), encoding="utf-8"
        )

    # -- politeness --------------------------------------------------------
    def _wait(self) -> None:
        """Sleep so consecutive live requests stay at least delay_seconds apart."""
        delay = self.config.crawl.delay_seconds
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < delay:
                time.sleep(delay - elapsed)

    # -- fetching ----------------------------------------------------------
    def get(self, url: str, *, is_robots: bool = False) -> FetchResult:
        """Fetch ``url``, preferring the on-disk cache.

        Raises :class:`CircuitBreakerTripped` once too many consecutive pages have
        failed — the caller should stop the run rather than continue.
        """
        if self.config.crawl.cache_first and not self.force:
            cached = self._read_cache(url)
            if cached is not None:
                self.stats["cache_hits"] += 1
                return cached

        # Respect robots.txt for anything we would actually crawl.
        if not is_robots and self.robots is not None:
            if not self.robots.is_allowed(urlparse(url).path or "/"):
                self.stats["robots_blocked"] += 1
                raise PermissionError(f"robots.txt disallows {url}")

        backoffs = self.config.crawl.backoff_seconds
        last_error: Exception | None = None

        for attempt in range(1, self.config.crawl.max_retries + 1):
            self._wait()
            try:
                response = self._client.get(url)
                self._last_request_at = time.monotonic()
                self.stats["live_fetches"] += 1

                if response.status_code in TRANSIENT_STATUSES:
                    last_error = httpx.HTTPStatusError(
                        f"HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                    if attempt < self.config.crawl.max_retries:
                        time.sleep(backoffs[min(attempt - 1, len(backoffs) - 1)])
                        continue
                else:
                    body = response.text
                    meta = CrawlMeta(
                        url=url,
                        final_url=str(response.url),
                        status=response.status_code,
                        fetched_at=datetime.now(timezone.utc),
                        content_sha256=hashlib.sha256(
                            body.encode("utf-8", "replace")
                        ).hexdigest(),
                        bytes=len(response.content),
                        etag=response.headers.get("etag"),
                        last_modified=response.headers.get("last-modified"),
                        from_cache=False,
                        attempt=attempt,
                    )
                    result = FetchResult(
                        url=url,
                        final_url=str(response.url),
                        status=response.status_code,
                        text=body,
                        meta=meta,
                        from_cache=False,
                    )
                    # Cache only successes; a stored 404 would be misleading.
                    if 200 <= response.status_code < 300:
                        self._write_cache(result)
                        self.consecutive_failures = 0
                    else:
                        self.stats["failures"] += 1
                        self.consecutive_failures += 1
                    return result
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                self._last_request_at = time.monotonic()
                if attempt < self.config.crawl.max_retries:
                    time.sleep(backoffs[min(attempt - 1, len(backoffs) - 1)])

        # All retries exhausted.
        self.stats["failures"] += 1
        self.consecutive_failures += 1
        threshold = self.config.crawl.circuit_breaker_consecutive_failures
        if self.consecutive_failures >= threshold:
            raise CircuitBreakerTripped(
                f"{self.consecutive_failures} consecutive failures (last: {url}) — "
                f"host appears to be blocking; stopping rather than hammering it. "
                f"Progress is cached; re-run with --resume later."
            ) from last_error
        raise RuntimeError(f"Failed to fetch {url}: {last_error}") from last_error

    def load_robots(self) -> RobotsPolicy:
        """Fetch and cache the robots.txt policy."""
        url = f"{self.config.site.base_url}{self.config.site.robots_path}"
        result = self.get(url, is_robots=True)
        self.robots = RobotsPolicy.parse(result.text, user_agent="*")
        return self.robots

    def load_sitemap(self) -> str:
        """Fetch the raw sitemap XML."""
        url = f"{self.config.site.base_url}{self.config.site.sitemap_path}"
        return self.get(url, is_robots=True).text


def load_cached_html(config: Config, url: str) -> str | None:
    """Read cached HTML for ``url`` without any network access."""
    html_path, _ = Fetcher(config)._paths_for(url)  # noqa: SLF001 - cheap helper
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return None


def iter_cached_pages(config: Config) -> list[tuple[str, str]]:
    """Yield ``(url, html)`` for every page currently in the raw cache."""
    raw_dir = config.path("raw")
    pages: list[tuple[str, str]] = []
    for meta_path in sorted(raw_dir.glob("*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        html_path = meta_path.with_name(meta_path.name.replace(".meta.json", ""))
        if html_path.exists() and 200 <= int(meta.get("status", 0)) < 300:
            pages.append((meta["url"], html_path.read_text(encoding="utf-8")))
    return pages
