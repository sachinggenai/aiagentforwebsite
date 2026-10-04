"""Read-only access to the collected catalogue corpus (MOD-03, DAT-01/02/03/04).

The corpus is produced by the scraper in `src/ams/` and lands in `data/out/`:
`chunks.jsonl` for retrieval and `ams.sqlite` for exact lookups. This module is
the **only** place that touches those files — everything downstream goes through
it, so the shapes of the source artifacts are known in exactly one place.

Two properties matter here:

**Read-only, always.** The corpus is an input, not state. Connection is opened
in read-only mode so a bug here cannot corrupt the catalogue, and so the
platform can run while the scraper is rewriting it.

**Absence is data.** Three products have no specifications and four have no
About text *on the source site*. Those are recorded facts, not gaps to be filled
by inference (DG-13) — so :meth:`Corpus.product_view` reports them explicitly
rather than returning empty lists that a caller might misread as "no data yet".
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Products whose specification block is empty on the source site.
# Asserted by US-012 so a scraper change that silently drops specs is caught.
KNOWN_NO_SPECS: frozenset[str] = frozenset({"270685", "270687", "381893"})

# Products whose About section is empty on the source site.
KNOWN_NO_ABOUT: frozenset[str] = frozenset({"270683", "3935251", "3935247", "3935249"})

# The mandatory commercial caveat (BRD-06 / R-1). The corpus records
# price_is_list_price=1 and quote_required=1 for every product, which is what
# makes this a fact rather than a policy preference.
PRICE_CAVEAT = (
    "This is the published list price, excluding taxes; "
    "a formal quotation is required for an order."
)


@dataclass(frozen=True)
class Product:
    """One catalogue product, as stored."""

    product_id: str
    name: str
    url: str
    category: str | None = None
    model: str | None = None
    price_amount: int | None = None
    price_currency: str | None = None
    price_unit: str | None = None
    price_display: str | None = None
    price_spoken: str | None = None
    price_is_list_price: bool = True
    quote_required: bool = True
    moq_value: float | None = None
    moq_unit: str | None = None
    delivery_time: str | None = None
    delivery_days: int | None = None
    supply_ability: str | None = None
    sample_available: str | None = None
    sample_policy: str | None = None
    main_domestic_market: str | None = None
    brand: str | None = None
    spec_count: int = 0
    faq_count: int = 0
    about_chars: int = 0

    @property
    def has_specs(self) -> bool:
        return self.spec_count > 0

    @property
    def has_about(self) -> bool:
        return self.about_chars > 0

    @property
    def disambiguator(self) -> str:
        """A short phrase that tells two same-named products apart.

        The catalogue contains **two different products both named "Medical Dry
        Imager"** at different prices. A voice answer that says only the name is
        ambiguous in the worst possible way — it could quote either price — so
        any answer touching a duplicated name carries this suffix (BRD-09).
        """
        parts = []
        if self.model:
            parts.append(f"model {self.model}")
        if self.price_display:
            parts.append(self.price_display)
        return f" (product {self.product_id}" + (", " + ", ".join(parts) if parts else "") + ")"

    def label(self, *, with_id: bool = False) -> str:
        return f"{self.name}{self.disambiguator}" if with_id else self.name


@dataclass(frozen=True)
class Spec:
    name: str
    value: str
    group: str = "specification"


@dataclass(frozen=True)
class Faq:
    question: str
    answer: str
    product_id: str | None = None


@dataclass
class ProductView:
    """A product plus everything needed to answer about it, with absence explicit.

    ``specs`` and ``trade`` are kept **separate** because the corpus stores them
    as two groups in one table, and conflating them is not a cosmetic error: a
    product with no specification block but a populated trade block (there are
    three) would appear to have specifications, and a comparison would then
    "compare" MOQ against a competitor's weight. The questions they answer are
    different, so the fields are different.
    """

    product: Product
    specs: list[Spec] = field(default_factory=list)   # group == "specification"
    trade: list[Spec] = field(default_factory=list)   # group == "trade_information"
    faqs: list[Faq] = field(default_factory=list)
    # True when the *source site* has no such data. Distinct from an empty list,
    # which could mean "not loaded" — a caller must be able to tell them apart.
    specs_absent_on_source: bool = False
    about_absent_on_source: bool = False

    def spec(self, name: str) -> str | None:
        """Case-insensitive specification lookup, or None when not published.

        Deliberately searches specifications only — a trade term is not a
        product specification, and returning one for the other would put an MOQ
        where a buyer expected a weight.
        """
        wanted = name.strip().casefold()
        for item in self.specs:
            if item.name.strip().casefold() == wanted:
                return item.value
        return None

    def trade_term(self, name: str) -> str | None:
        wanted = name.strip().casefold()
        for item in self.trade:
            if item.name.strip().casefold() == wanted:
                return item.value
        return None


class Corpus:
    """Read-only handle on the catalogue corpus."""

    def __init__(self, out_dir: Path | str) -> None:
        self.out_dir = Path(out_dir)
        self.sqlite_path = self.out_dir / "ams.sqlite"
        self.chunks_path = self.out_dir / "chunks.jsonl"
        if not self.sqlite_path.exists():
            raise FileNotFoundError(
                f"corpus not found at {self.sqlite_path} — run the scraper "
                f"(src/ams) or check corpus.out_dir in config/anita.yaml"
            )

    # -- connection --------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        # Read-only URI: a bug here cannot corrupt the catalogue, and the
        # platform can run while the scraper rewrites it.
        connection = sqlite3.connect(f"file:{self.sqlite_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    # -- products ----------------------------------------------------------
    def all_products(self) -> list[Product]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM products ORDER BY name").fetchall()
        return [self._to_product(row) for row in rows]

    def get(self, product_id: str) -> Product | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM products WHERE product_id = ?", (str(product_id),)
            ).fetchone()
        return self._to_product(row) if row else None

    def specs_for(self, product_id: str) -> list[Spec]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT spec_name, spec_value, spec_group FROM product_specs "
                "WHERE product_id = ? ORDER BY spec_group, rowid",
                (str(product_id),),
            ).fetchall()
        return [Spec(name=r["spec_name"], value=r["spec_value"], group=r["spec_group"]) for r in rows]

    def faqs_for(self, product_id: str) -> list[Faq]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT question, answer, product_id FROM faqs WHERE product_id = ?",
                (str(product_id),),
            ).fetchall()
        return [
            Faq(question=r["question"], answer=r["answer"], product_id=r["product_id"])
            for r in rows
        ]

    def product_view(self, product_id: str) -> ProductView | None:
        """Everything known about one product, with absence stated explicitly."""
        product = self.get(product_id)
        if product is None:
            return None
        rows = self.specs_for(product_id)
        specs = [s for s in rows if s.group != "trade_information"]
        trade = [s for s in rows if s.group == "trade_information"]
        return ProductView(
            product=product,
            specs=specs,
            trade=trade,
            faqs=self.faqs_for(product_id),
            specs_absent_on_source=not specs and product_id in KNOWN_NO_SPECS,
            about_absent_on_source=(not product.has_about) and product_id in KNOWN_NO_ABOUT,
        )

    def _to_product(self, row: sqlite3.Row) -> Product:
        def value(key: str) -> Any:
            return row[key] if key in row.keys() else None

        return Product(
            product_id=str(row["product_id"]),
            name=row["name"] or "",
            url=row["url"] or "",
            category=value("category"),
            model=value("model"),
            price_amount=value("price_amount"),
            price_currency=value("price_currency"),
            price_unit=value("price_unit"),
            price_display=value("price_display"),
            price_spoken=value("price_spoken"),
            price_is_list_price=bool(value("price_is_list_price") or 0),
            quote_required=bool(value("quote_required") or 0),
            moq_value=value("moq_value"),
            moq_unit=value("moq_unit"),
            delivery_time=value("delivery_time"),
            delivery_days=value("delivery_days"),
            supply_ability=value("supply_ability"),
            sample_available=value("sample_available"),
            sample_policy=value("sample_policy"),
            main_domestic_market=value("main_domestic_market"),
            brand=value("brand"),
            spec_count=value("spec_count") or 0,
            faq_count=value("faq_count") or 0,
            about_chars=value("about_chars") or 0,
        )

    # -- company facts -----------------------------------------------------
    def company_ids(self) -> dict[str, list[str]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT kind, value FROM company_ids").fetchall()
        grouped: dict[str, list[str]] = {}
        for row in rows:
            grouped.setdefault(row["kind"], []).append(row["value"])
        return grouped

    def people(self) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT name, role FROM people").fetchall()
        return [{"name": r["name"], "role": r["role"]} for r in rows]

    def branches(self) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT label, address FROM branches").fetchall()
        return [{"label": r["label"], "address": r["address"]} for r in rows]

    def certifications(self) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT kind, value FROM certifications").fetchall()
        return [{"kind": r["kind"], "value": r["value"]} for r in rows]

    # -- integrity ---------------------------------------------------------
    def caveat_violations(self) -> list[str]:
        """Product ids whose price is not flagged as list-price + quote-required.

        This is a **boot guard** (TRD-19), not a report. Every price this system
        quotes must carry the caveat; if the corpus ever says otherwise, the
        platform must refuse to serve prices rather than quote one as final.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT product_id FROM products "
                "WHERE price_is_list_price <> 1 OR quote_required <> 1"
            ).fetchall()
        return [str(r["product_id"]) for r in rows]

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            products = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            specs = connection.execute("SELECT COUNT(*) FROM product_specs").fetchone()[0]
            faqs = connection.execute("SELECT COUNT(*) FROM faqs").fetchone()[0]
        return {"products": products, "specs": specs, "faqs": faqs}

    # -- chunks (retrieval input, MOD-03 proper) ---------------------------
    def iter_chunks(self) -> Iterable[dict[str, Any]]:
        """Yield corpus chunks. Used by the index builder (US-010)."""
        if not self.chunks_path.exists():
            return
        with self.chunks_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)
