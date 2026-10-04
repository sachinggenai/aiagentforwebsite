"""Text and value normalisation.

Turns raw site strings into values a chat/voice/WhatsApp layer can use directly.
The money handling is deliberately Indian-convention: a text-to-speech engine
reading "1450000" aloud produces something no caller understands, whereas
"about 14.5 lakh rupees" is how the price would actually be said.

All functions here are pure and independently testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Prefix / whitespace cleanup
# ---------------------------------------------------------------------------
_QA_PREFIX = re.compile(r"^\s*([QA])\s*[:\-–]\s*", re.IGNORECASE)
_HTML_ENTITIES = {
    "&rsquo;": "’", "&lsquo;": "‘", "&amp;": "&", "&nbsp;": " ",
    "&#10005;": "✕", "&quot;": '"', "&#39;": "'", "&lt;": "<", "&gt;": ">",
}


def strip_qa_prefix(text: str) -> str:
    """Remove a leading literal ``Q:`` / ``A:`` from a FAQ string.

    The site stores these as content, not markup, so they survive extraction and
    must be removed from the value.
    """
    return _QA_PREFIX.sub("", text or "").strip()


def decode_entities(text: str) -> str:
    """Resolve the HTML entities that survive BeautifulSoup's parser."""
    for entity, char in _HTML_ENTITIES.items():
        text = text.replace(entity, char)
    return text


def clean_text(text: str) -> str:
    """Standard cleanup applied to every extracted string."""
    if not text:
        return ""
    text = decode_entities(text)
    text = text.replace("\xa0", " ").replace("​", "")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def collapse_lines(text: str) -> str:
    """Collapse runs of blank lines and trailing spaces."""
    lines = [line.strip() for line in (text or "").split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


_ORPHAN_COMMA = re.compile(r"\s*,\s*(?=[,\s]|$)")
_RUN_OF_COMMAS = re.compile(r"(?:\s*,\s*){2,}")


def tidy_punctuation(text: str) -> str:
    """Clean malformed separators that exist in the source markup.

    Some specification values are stored malformed on the site itself — the
    markup literally contains ``<li>1 , , Unit</li>``. Extraction is faithful;
    this repairs the display so an answer is not read out as "one comma comma
    unit".

    Only applied to short structured values, never to prose, so sentence
    punctuation is untouched.
    """
    if not text:
        return ""
    text = _RUN_OF_COMMAS.sub(", ", text)
    text = _ORPHAN_COMMA.sub("", text)
    text = re.sub(r"\s+([,;])", r"\1", text)
    text = re.sub(r"[,;]\s*$", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Money:
    """A price with display and speakable renderings."""

    amount_int: int | None
    currency: str = "INR"
    unit: str | None = None
    display: str = ""
    spoken: str = ""
    is_list_price: bool = True
    quote_required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "amount_int": self.amount_int,
            "currency": self.currency,
            "unit": self.unit,
            "display": self.display,
            "spoken": self.spoken,
            "is_list_price": self.is_list_price,
            "quote_required": self.quote_required,
        }


def indian_grouping(value: int) -> str:
    """Format an integer using the Indian digit grouping (2,2,3 from the right).

    ``1450000`` -> ``"14,50,000"``
    """
    digits = str(abs(int(value)))
    if len(digits) <= 3:
        return ("-" if value < 0 else "") + digits
    head, tail = digits[:-3], digits[-3:]
    parts: list[str] = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    grouped = ",".join(parts + [tail])
    return ("-" if value < 0 else "") + grouped


def spoken_amount(value: int, currency: str = "INR") -> str:
    """Render an amount the way it would be spoken in Indian English.

    Uses lakh (10^5) and crore (10^7), which is how these figures are actually
    said aloud — "14.5 lakh rupees", not "one million four hundred fifty thousand".
    """
    unit_name = {"INR": "rupees", "USD": "dollars", "EUR": "euros"}.get(currency, currency)
    if value >= 10_000_000:
        n = value / 10_000_000
        return f"about {_trim(n)} crore {unit_name}"
    if value >= 100_000:
        n = value / 100_000
        return f"about {_trim(n)} lakh {unit_name}"
    # Below a lakh, lakh/crore phrasing sounds stilted ("about 2.5 thousand").
    # The grouped digits read naturally through text-to-speech instead.
    if value == 1:
        return f"1 {unit_name.rstrip('s')}"
    return f"{indian_grouping(value)} {unit_name}"


def _trim(n: float) -> str:
    """Drop a trailing ``.0`` so we say "25 lakh", not "25.0 lakh"."""
    return f"{n:.2f}".rstrip("0").rstrip(".")


_CURRENCY_SYMBOL = {"INR": "₹", "USD": "$", "EUR": "€"}


def parse_price(raw: str | None, unit: str | None = None, currency: str = "INR") -> Money:
    """Parse a raw price string such as ``"2500"`` or ``"Price 2500 INR/ Piece"``."""
    if raw is None:
        return Money(None, currency, unit, "", "")
    text = str(raw)
    match = re.search(r"([\d][\d,]*(?:\.\d+)?)", text)
    if not match:
        return Money(None, currency, unit, text.strip(), "")

    digits = match.group(1).replace(",", "")
    try:
        amount = int(float(digits))
    except ValueError:
        return Money(None, currency, unit, text.strip(), "")

    symbol = _CURRENCY_SYMBOL.get(currency, "")
    display = f"{symbol}{indian_grouping(amount)}"
    if unit:
        display = f"{display}/{unit}"
    return Money(
        amount_int=amount,
        currency=currency,
        unit=unit,
        display=display,
        spoken=spoken_amount(amount, currency),
    )


# ---------------------------------------------------------------------------
# Quantities and units
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Quantity:
    """A parsed quantity, supporting both single values and ranges."""

    value: float | None = None
    value_min: float | None = None
    value_max: float | None = None
    unit: str | None = None
    raw: str = ""

    @property
    def is_range(self) -> bool:
        return self.value_min is not None and self.value_max is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "value_min": self.value_min,
            "value_max": self.value_max,
            "unit": self.unit,
            "raw": self.raw,
        }


# Units as they appear on this site, longest first so "Kilograms" wins over "kg".
_UNIT_PATTERN = (
    r"kilograms|kilogram|kgs|kg|grams|gram|g|"
    r"millilitres|millilitre|milliliters|milliliter|ml|litres|litre|liters|liter|l|"
    r"pieces|piece|pcs|pc|units|unit|"
    r"days|day|weeks|week|months|month|years|year|"
    r"per day|per month|psi|mmhg|kv|ma|mm|cm|m|%"
)
_RANGE_RE = re.compile(
    rf"(?P<lo>\d+(?:\.\d+)?)\s*(?:-|to|–)\s*(?P<hi>\d+(?:\.\d+)?)\s*(?P<unit>{_UNIT_PATTERN})?",
    re.IGNORECASE,
)
_SINGLE_RE = re.compile(rf"(?P<val>\d+(?:\.\d+)?)\s*(?P<unit>{_UNIT_PATTERN})?", re.IGNORECASE)


def parse_quantity(raw: str | None) -> Quantity:
    """Parse a quantity string, including ranges.

    ``"2-3.5 Kilograms (kg)"`` -> range 2.0–3.5, unit ``Kilograms``
    ``"10 Pieces Per Day"``   -> 10, unit ``Pieces``
    ``"1 Piece"``             -> 1, unit ``Piece``
    """
    if not raw:
        return Quantity(raw="")
    text = clean_text(str(raw))

    range_match = _RANGE_RE.search(text)
    if range_match:
        return Quantity(
            value_min=float(range_match.group("lo")),
            value_max=float(range_match.group("hi")),
            unit=(range_match.group("unit") or None),
            raw=text,
        )

    single = _SINGLE_RE.search(text)
    if single:
        return Quantity(
            value=float(single.group("val")),
            unit=(single.group("unit") or None),
            raw=text,
        )
    return Quantity(raw=text)


def parse_lead_time_days(raw: str | None) -> int | None:
    """Extract a day count from a delivery-time string. ``"7 Days"`` -> 7."""
    if not raw:
        return None
    match = re.search(r"(\d+)\s*(day|week|month)", str(raw), re.IGNORECASE)
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2).lower()
    return {"day": value, "week": value * 7, "month": value * 30}[unit]


# ---------------------------------------------------------------------------
# Label / value splitting
# ---------------------------------------------------------------------------
# Labels known to appear on this site's product and trade-information blocks.
# Used only as a fallback: on product pages the label and value are separate
# <li> elements (see parsers/product.py), so no text splitting is needed there.
# This matters for the free-text fallback path — e.g. the "Technical Details"
# <table> that appears on pages whose standard spec block is empty.
KNOWN_LABELS: tuple[str, ...] = (
    "Minimum Order Quantity", "Sample Policy", "Sample Available",
    "Supply Ability", "Delivery Time", "Main Domestic Market",
    "Payment Terms", "Packaging Details", "Power Source", "Operating Mode",
    "Wall Mounted", "Suitable For", "Color Code", "Model Number",
    "Condition", "Portable", "Usage", "Weight", "Material", "Color",
    "Application", "Capacity", "Accuracy", "Display Type", "Size",
    "Type", "Brand", "Model", "Voltage", "Frequency", "Warranty",
    "Flow Rate", "Volume", "Pressure Limit", "Injection Mode", "Scan Delay",
    "Rated Voltage", "Country of Origin", "Shape", "Finishing",
)


def split_label_value(text: str) -> tuple[str, str] | None:
    """Split a free-text spec row into ``(label, value)``.

    Preference order:

    1. An explicit ``"Label: Value"`` separator (unambiguous).
    2. The longest known label that prefixes the text — this is what correctly
       splits ``"Sample Policy Sample costs shipping..."`` on the value's first
       letter rather than on a number.
    3. A digit boundary, then a capitalisation boundary, as last resorts.
    """
    text = clean_text(text)
    if not text:
        return None

    if ":" in text:
        label, _, value = text.partition(":")
        if label.strip() and value.strip():
            return clean_text(label), clean_text(value)

    # Longest known-label prefix wins, so "Sample Available" beats "Sample".
    folded = text.casefold()
    best: str | None = None
    for label in KNOWN_LABELS:
        if folded.startswith(label.casefold()):
            if best is None or len(label) > len(best):
                best = label
    if best is not None:
        remainder = text[len(best):].lstrip(" :-–")
        if remainder:
            return best, clean_text(remainder)

    match = re.match(r"^([A-Za-z][A-Za-z0-9 /()\-\.]{1,40}?)\s+(\d.*)$", text)
    if match:
        return clean_text(match.group(1)), clean_text(match.group(2))

    words = text.split()
    for i in range(len(words) - 1, 0, -1):
        if any(ch.isdigit() for ch in " ".join(words[i:])):
            return " ".join(words[:i]), " ".join(words[i:])
    return None


_SLUG_ID_RE = re.compile(r"-(\d{5,7})\.html$")


def product_id_from_url(url: str) -> str | None:
    """Extract the numeric product id from a product URL slug."""
    match = _SLUG_ID_RE.search(url or "")
    return match.group(1) if match else None


def slug_from_url(url: str) -> str:
    """Return the final path segment (without extension) of a URL."""
    path = (url or "").rstrip("/").rsplit("/", 1)[-1]
    return path[:-5] if path.endswith(".html") else path
