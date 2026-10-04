"""DOM loading and text extraction.

The single most important function here is :func:`smart_text`. The site's rich
prose is Draft.js output in which **every word sits in its own ``<span>``**::

    <span>light</span><span>-</span><span>tight</span>

Stripping tags with a space separator corrupts this into ``"light - tight"``,
``"X - Ray"`` and ``"film ."`` — and that corruption is not cosmetic. It reaches
the JSON-LD ``description`` field too, so a naive extractor poisons the corpus
with broken product copy. Parsing a page this way and then embedding it produces
a RAG that answers in mangled English.

The rule that fixes it, verified against the live pages:

* **Inline** elements (span, strong, font, a, ...) concatenate with **no**
  separator — this is what repairs ``"X - Ray"`` into ``"X-Ray"``.
* **Block** elements (div, p, li, br, headings, ...) insert a single space —
  omitting this would merge separate sentences.

Verified on ``x-ray-cassette-270691`` (span-heavy) and ``ultrasound-gel-270694``
(clean): the walker repairs the first and leaves the second byte-identical.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

# Elements that must NOT break a word when adjacent. Everything else is treated
# as block-level and separated by a space.
INLINE_ELEMENTS: frozenset[str] = frozenset(
    {
        "a", "abbr", "b", "bdi", "bdo", "big", "br", "cite", "code", "data",
        "del", "dfn", "em", "font", "i", "ins", "kbd", "label", "mark", "q",
        "s", "samp", "small", "span", "strong", "sub", "sup", "time", "tt",
        "u", "var", "wbr",
    }
)

# Never contributes text.
DROPPED_ELEMENTS: tuple[str, ...] = ("script", "style", "noscript", "iframe", "svg", "template")

# Inquiry-modal / widget containers. These repeat on every page and sit
# *between* real content sections in document order, so they must be removed
# structurally — no amount of regex scoping makes them safe to ignore.
MODAL_SELECTORS: tuple[str, ...] = (
    "#sendInquiry_normal",
    "#sendInquiry",
    "#reqCallBack",
    "#reqCallBackOtp",
    "#callback_registration",
    "#quick_inquiry_first_form",
    "#inquiry_form_similar_product_list",
    # The "quick inquiry" question flow (OTP prompts, "Did not receive yet?").
    # It sits in the document flow on every page and is not page content.
    "#quick_inquiry_question",
    ".mcQuestion",
    # Footer and repeated navigation blocks. These are chrome on every page; the
    # links themselves are captured as link edges, so the prose is redundant and
    # would otherwise appear 32 times in the corpus.
    "div.footer_linkss",
    "div.ourProduct",
    ".terms-condition",
    # Contact-form widgets and their AJAX loading spinners ("Please Wait...",
    # "Don't leave just yet!"). Not page content.
    ".mcContactForm",
    ".loading-container",
    "div.modal",
    "div.popup",
)

# Strings that must never reach the corpus. Used by the validation gate to prove
# chrome did not leak into extracted text.
CHROME_MARKERS: tuple[str, ...] = (
    "Tell us about your requirement",
    "Did not receive yet?",
    "You're Done!",
    "You’re Done!",
    "Products You May Like",
    "Quick Links",
    "Request To Call Back",
    "Don't leave just yet!",
    "Please Wait...",
    "Send SMS",
    "Call Me Free",
)

_WS_RUN = re.compile(r"[ \t ​‌‍﻿]+")
_SPACE_AROUND_NL = re.compile(r" *\n *")
_MULTI_NL = re.compile(r"\n{2,}")


def make_soup(html: str) -> BeautifulSoup:
    """Parse HTML with the fast lxml backend.

    Decoding is the caller's job — the response has ``Content-Type: text/html``
    with no charset, so the bytes must be decoded as UTF-8 explicitly rather
    than guessed (a 919-byte block page would otherwise be sniffed as cp1252).
    """
    return BeautifulSoup(html, "lxml")


def strip_noise(soup: BeautifulSoup) -> BeautifulSoup:
    """Remove scripts, styles, comments and modal/widget containers in place.

    Called before any extraction. On these pages it deletes ~98% of the markup.
    """
    for tag in soup.find_all(list(DROPPED_ELEMENTS)):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for selector in MODAL_SELECTORS:
        for tag in soup.select(selector):
            tag.decompose()
    return soup


def _walk(node: Tag | NavigableString, out: list[str]) -> None:
    if isinstance(node, NavigableString):
        out.append(str(node))
        return
    if not isinstance(node, Tag):
        return
    if node.name in DROPPED_ELEMENTS:
        return

    is_block = node.name not in INLINE_ELEMENTS
    if is_block and out and not out[-1].endswith((" ", "\n")):
        out.append(" ")

    for child in node.children:
        _walk(child, out)

    if is_block and out and not out[-1].endswith((" ", "\n")):
        out.append(" ")


def smart_text(node: Tag | NavigableString | None) -> str:
    """Extract text, joining inline elements without a separator.

    This is the function that turns ``"An X - Ray cassette ... on a film ."``
    back into ``"An X-Ray cassette ... on a film."``
    """
    if node is None:
        return ""
    out: list[str] = []
    _walk(node, out)
    text = "".join(out).replace("\xa0", " ")
    text = _WS_RUN.sub(" ", text)
    text = _SPACE_AROUND_NL.sub("\n", text)
    text = _MULTI_NL.sub("\n", text)
    return text.strip()


def flat_text(node: Tag | NavigableString | None) -> str:
    """Like :func:`smart_text` but collapses newlines to single spaces."""
    return re.sub(r"\s+", " ", smart_text(node)).strip()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def text_sha(text: str) -> str:
    """Stable short hash used in chunk ids."""
    return sha256_text(text)[:12]


def dedupe_lines(text: str, *, min_length: int = 1) -> str:
    """Drop consecutive duplicate lines, preserving order.

    Navigation and footer blocks repeat themselves within a page; this collapses
    the repeats without touching genuinely distinct lines.
    """
    seen: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if len(line) < min_length:
            continue
        if not seen or seen[-1] != line:
            seen.append(line)
    return "\n".join(seen)


def contains_chrome(text: str) -> list[str]:
    """Return any chrome marker strings present in ``text``.

    An empty list means the text is clean. The validation gate asserts this is
    empty for every emitted chunk.
    """
    return [marker for marker in CHROME_MARKERS if marker in text]


def drop_chrome_lines(text: str) -> str:
    """Remove lines that contain a known chrome marker.

    Used by the whole-page fallback parser, where the extracted region is the
    entire body and therefore inevitably overlaps footer and widget markup.
    Removing the specific offending lines is more honest than widening the
    container blacklist until the check happens to pass.
    """
    kept = [
        line
        for line in (text or "").split("\n")
        if line.strip() and not contains_chrome(line)
    ]
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# Section walking
# ---------------------------------------------------------------------------
def iter_sections(root: Tag, levels: Iterable[str] = ("h2",)) -> Iterator[tuple[str, str, Tag]]:
    """Yield ``(heading_text, level, body)`` for each heading inside ``root``.

    ``body`` is a synthetic container holding every sibling up to the next
    heading of the same or higher level. Used to scope a section by structure
    rather than by brittle CSS classes.
    """
    heading_tags = root.find_all(list(levels))
    for heading in heading_tags:
        body_parts: list[str] = []
        for sibling in heading.next_siblings:
            if isinstance(sibling, Tag) and sibling.name in levels:
                break
            if isinstance(sibling, NavigableString):
                body_parts.append(str(sibling))
            elif isinstance(sibling, Tag):
                body_parts.append(str(sibling))
        body_soup = BeautifulSoup("".join(body_parts), "lxml")
        yield heading.get_text(" ", strip=True), heading.name, body_soup


def find_heading(soup: Tag, text: str, levels: Iterable[str] = ("h2", "h3")) -> Tag | None:
    """Find the first heading whose normalised text contains ``text``.

    Matching is on normalised text so that stray whitespace and inline markup in
    the heading do not defeat it.
    """
    needle = text.casefold().strip()
    for heading in soup.find_all(list(levels)):
        if needle in flat_text(heading).casefold():
            return heading
    return None


def section_body(heading: Tag, levels: Iterable[str] = ("h2", "h3")) -> str:
    """Return the text following ``heading`` up to the next heading at its level or above."""
    parts: list[str] = []
    for sibling in heading.next_siblings:
        if isinstance(sibling, Tag) and sibling.name in levels:
            break
        if isinstance(sibling, NavigableString):
            parts.append(str(sibling))
        elif isinstance(sibling, Tag):
            parts.append(str(sibling))
    return smart_text(BeautifulSoup("".join(parts), "lxml"))


def meta_content(soup: BeautifulSoup, *, name: str | None = None, prop: str | None = None) -> str | None:
    """Read a ``<meta>`` value by ``name=`` or ``property=``."""
    if name:
        tag = soup.find("meta", attrs={"name": name})
    elif prop:
        tag = soup.find("meta", attrs={"property": prop})
    else:
        return None
    if tag and tag.get("content"):
        return str(tag["content"]).strip()
    return None
