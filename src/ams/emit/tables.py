"""Tabular output: CSVs plus a queryable SQLite database.

This is the deterministic half of the eventual chat system. Most high-value
questions about this site are *lookups*, not semantic search — price, MOQ,
delivery time, GST number, address, director names. Those are answered exactly
from these tables, with no chance of hallucination. Embeddings are then reserved
for what genuinely needs them: the About prose and the FAQs.

Numeric columns are stored as numbers (not text) so ``WHERE price < 100000``
works and can be indexed.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from ..config import Config
from ..models import PageDocument, SiteShared
from ..normalize import parse_lead_time_days, parse_quantity
from . import write_json

# name -> column definitions (SQLite types)
SCHEMA: dict[str, str] = {
    "products": """
        product_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        model TEXT,
        category TEXT,
        jsonld_category TEXT,
        url TEXT NOT NULL,
        price_amount INTEGER,
        price_currency TEXT,
        price_unit TEXT,
        price_display TEXT,
        price_spoken TEXT,
        price_is_list_price INTEGER,
        quote_required INTEGER,
        moq_value REAL,
        moq_unit TEXT,
        delivery_time TEXT,
        delivery_days INTEGER,
        supply_ability TEXT,
        sample_available TEXT,
        sample_policy TEXT,
        main_domestic_market TEXT,
        availability TEXT,
        condition TEXT,
        brand TEXT,
        about_chars INTEGER,
        faq_count INTEGER,
        spec_count INTEGER,
        meta_description TEXT,
        meta_keywords TEXT,
        source_url TEXT,
        content_sha256 TEXT,
        crawled_at TEXT
    """,
    "product_specs": """
        product_id TEXT,
        spec_group TEXT,
        spec_name TEXT,
        spec_value TEXT,
        value_min REAL,
        value_max REAL,
        unit TEXT,
        source_url TEXT
    """,
    "faqs": """
        product_id TEXT,
        product_name TEXT,
        question TEXT,
        answer TEXT,
        source TEXT,
        source_url TEXT
    """,
    "media": """
        product_id TEXT,
        url TEXT,
        role TEXT,
        variant TEXT,
        alt TEXT,
        source_url TEXT
    """,
    "people": """
        name TEXT,
        role TEXT,
        source_url TEXT
    """,
    "branches": """
        label TEXT,
        address TEXT,
        source_url TEXT
    """,
    "certifications": """
        kind TEXT,
        value TEXT,
        source_url TEXT
    """,
    "company_ids": """
        kind TEXT,
        value TEXT,
        source_url TEXT
    """,
    "categories": """
        name TEXT,
        url TEXT,
        declared_count INTEGER,
        listed_count INTEGER
    """,
    "related_products": """
        product_id TEXT,
        related_name TEXT,
        related_url TEXT
    """,
}


def _product_rows(doc: PageDocument) -> dict:
    offer = doc.offer
    return {
        "product_id": doc.product_id,
        "name": doc.product_name,
        "model": (doc.relations or {}).get("model"),
        "category": doc.category_name,
        "jsonld_category": doc.structured.category if doc.structured else None,
        "url": doc.url,
        "price_amount": int(offer.price) if offer and offer.price is not None else None,
        "price_currency": offer.currency if offer else None,
        "price_unit": offer.price_unit if offer else None,
        "price_display": _price_display(doc),
        "price_spoken": _price_spoken(doc),
        "price_is_list_price": 1,
        "quote_required": 1,
        "moq_value": offer.moq_value if offer else None,
        "moq_unit": offer.moq_unit if offer else None,
        "delivery_time": offer.delivery_lead_time if offer else None,
        "delivery_days": parse_lead_time_days(offer.delivery_lead_time if offer else None),
        "supply_ability": offer.supply_ability if offer else None,
        "sample_available": offer.sample_available if offer else None,
        "sample_policy": offer.sample_policy if offer else None,
        "main_domestic_market": offer.main_domestic_market if offer else None,
        "availability": offer.availability if offer else None,
        "condition": offer.item_condition if offer else None,
        "brand": doc.structured.brand if doc.structured else None,
        "about_chars": len(doc.section_text("about")),
        "faq_count": len(doc.faqs),
        "spec_count": len(doc.specs),
        "meta_description": doc.meta.description,
        "meta_keywords": ", ".join(doc.meta.keywords) or None,
        "source_url": doc.url,
        "content_sha256": doc.crawl.content_sha256,
        "crawled_at": doc.crawl.fetched_at.isoformat(),
    }


def _price_display(doc: PageDocument) -> str | None:
    from ..normalize import parse_price

    if not doc.offer or doc.offer.price is None:
        return None
    return parse_price(str(doc.offer.price), doc.offer.price_unit).display


def _price_spoken(doc: PageDocument) -> str | None:
    from ..normalize import parse_price

    if not doc.offer or doc.offer.price is None:
        return None
    return parse_price(str(doc.offer.price), doc.offer.price_unit).spoken


def build_rows(
    documents: list[PageDocument], shared: SiteShared
) -> dict[str, list[dict]]:
    """Build every table's rows from the canonical documents."""
    products = [d for d in documents if d.page_type == "product"]

    tables: dict[str, list[dict]] = {name: [] for name in SCHEMA}

    for doc in products:
        tables["products"].append(_product_rows(doc))

        for spec in doc.specs:
            qty = parse_quantity(spec.value)
            tables["product_specs"].append(
                {
                    "product_id": doc.product_id,
                    "spec_group": "specification",
                    "spec_name": spec.name,
                    "spec_value": spec.value,
                    "value_min": qty.value_min,
                    "value_max": qty.value_max,
                    "unit": qty.unit,
                    "source_url": doc.url,
                }
            )
        for line in doc.section_text("trade_information").splitlines():
            if ":" not in line:
                continue
            name, _, value = line.partition(":")
            qty = parse_quantity(value)
            tables["product_specs"].append(
                {
                    "product_id": doc.product_id,
                    "spec_group": "trade_information",
                    "spec_name": name.strip(),
                    "spec_value": value.strip(),
                    "value_min": qty.value_min,
                    "value_max": qty.value_max,
                    "unit": qty.unit,
                    "source_url": doc.url,
                }
            )

        for faq in doc.faqs:
            tables["faqs"].append(
                {
                    "product_id": doc.product_id,
                    "product_name": doc.product_name,
                    "question": faq.question,
                    "answer": faq.answer,
                    "source": faq.source,
                    "source_url": doc.url,
                }
            )

        for item in doc.media:
            tables["media"].append(
                {
                    "product_id": item.product_id,
                    "url": item.url,
                    "role": item.role,
                    "variant": ",".join(sorted(item.variants)) or None,
                    "alt": item.alt,
                    "source_url": doc.url,
                }
            )

        for rel in (doc.relations or {}).get("related_products", []) or []:
            tables["related_products"].append(
                {
                    "product_id": doc.product_id,
                    "related_name": rel.get("name"),
                    "related_url": rel.get("url"),
                }
            )

    for doc in documents:
        if doc.page_type == "company_profile":
            for person in doc.company_facts.get("directors", []) or []:
                tables["people"].append(
                    {"name": person.get("name"), "role": person.get("role"), "source_url": doc.url}
                )
            for label, value in (doc.company_facts.get("statutory", {}) or {}).items():
                if "office" in label.casefold() or "address" in label.casefold():
                    tables["branches"].append(
                        {"label": label, "address": value, "source_url": doc.url}
                    )
            for cert in doc.company_facts.get("iso", []) or []:
                tables["certifications"].append(
                    {"kind": "iso", "value": cert, "source_url": doc.url}
                )
            for cert in doc.company_facts.get("nsic_crisil", []) or []:
                tables["certifications"].append(
                    {"kind": "nsic_crisil", "value": cert, "source_url": doc.url}
                )
            for kind in ("gst", "pan", "iec", "drug_license", "registration"):
                for value in doc.company_facts.get(kind, []) or []:
                    tables["company_ids"].append(
                        {"kind": kind, "value": value, "source_url": doc.url}
                    )
        if doc.page_type == "contact":
            for person in doc.contact_facts.get("persons", []) or []:
                tables["people"].append(
                    {"name": person.get("name"), "role": person.get("role"), "source_url": doc.url}
                )
            for label, value in (doc.contact_facts.get("addresses", {}) or {}).items():
                tables["branches"].append(
                    {"label": label, "address": value, "source_url": doc.url}
                )
            for value in doc.contact_facts.get("gst_numbers", []) or []:
                tables["company_ids"].append(
                    {"kind": "gst", "value": value, "source_url": doc.url}
                )
        if doc.page_type == "category":
            tables["categories"].append(
                {
                    "name": doc.category_name,
                    "url": doc.url,
                    "declared_count": None,
                    "listed_count": len(doc.listed_products),
                }
            )
        if doc.page_type == "products_index":
            for group in doc.listed_products:
                tables["categories"].append(
                    {
                        "name": group.get("category"),
                        "url": doc.url,
                        "declared_count": group.get("declared_count"),
                        "listed_count": len(group.get("products", []) or []),
                    }
                )

    # Site-wide facts that are not tied to a single page.
    for value in shared.gst_numbers:
        tables["company_ids"].append(
            {"kind": "gst", "value": value, "source_url": shared.source_url}
        )
    # Phone numbers are identifiers in the same sense as the GSTIN and the PAN:
    # a single structured value for the company, not prose. They were extracted
    # into site_shared.json and reached no table, so "what is your phone
    # number?" was unanswerable while the number sat in the corpus.
    for value in shared.phone_numbers:
        tables["company_ids"].append(
            {"kind": "phone", "value": value, "source_url": shared.source_url}
        )
    for label, value in shared.addresses.items():
        tables["branches"].append({"label": label, "address": value, "source_url": shared.source_url})

    tables["products"] = _dedupe(tables["products"], "product_id")
    tables["company_ids"] = _dedupe(tables["company_ids"], "kind", "value")
    tables["people"] = _dedupe(tables["people"], "name", "role")
    tables["branches"] = _dedupe(tables["branches"], "label")
    tables["certifications"] = _dedupe(tables["certifications"], "kind", "value")
    tables["categories"] = _dedupe(tables["categories"], "name")
    return tables


def _dedupe(rows: list[dict], *keys: str) -> list[dict]:
    """Keep the first row for each key combination, preserving order."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for row in rows:
        key = tuple(row.get(k) for k in keys)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def write_csvs(config: Config, tables: dict[str, list[dict]], schemas: dict[str, str]) -> None:
    """Write one CSV per table."""
    out_dir = config.path("tables")
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        columns = _columns(schemas[name])
        path = out_dir / f"{name}.csv"
        # newline='' is required for correct CSV on Windows.
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({c: row.get(c) for c in columns})


def _columns(definition: str) -> list[str]:
    """Parse column names out of the SQLite schema string."""
    names: list[str] = []
    for raw_line in definition.strip().splitlines():
        line = raw_line.strip().rstrip(",")
        if not line:
            continue
        names.append(line.split()[0])
    return names


def write_sqlite(config: Config, tables: dict[str, list[dict]], schemas: dict[str, str]) -> Path:
    """Materialise every table into ``ams.sqlite`` with helpful indexes."""
    db_path = config.path("out") / "ams.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    connection = sqlite3.connect(db_path)
    try:
        for name, definition in schemas.items():
            connection.execute(f"CREATE TABLE {name} ({definition})")
            rows = tables.get(name, [])
            if not rows:
                continue
            columns = _columns(definition)
            placeholders = ", ".join("?" for _ in columns)
            connection.executemany(
                f"INSERT INTO {name} ({', '.join(columns)}) VALUES ({placeholders})",
                [[row.get(c) for c in columns] for row in rows],
            )
        # Indexes for the lookups the chat layer will actually run.
        connection.executescript(
            """
            CREATE INDEX idx_products_name ON products(name);
            CREATE INDEX idx_products_price ON products(price_amount);
            CREATE INDEX idx_products_category ON products(category);
            CREATE INDEX idx_specs_product ON product_specs(product_id);
            CREATE INDEX idx_specs_name ON product_specs(spec_name);
            CREATE INDEX idx_faqs_question ON faqs(question);
            CREATE INDEX idx_company_ids_kind ON company_ids(kind);
            """
        )
        connection.commit()
    finally:
        connection.close()
    return db_path


def emit(config: Config, documents: list[PageDocument], shared: SiteShared) -> dict[str, int]:
    """Write CSVs, the SQLite database, and a schema record."""
    tables = build_rows(documents, shared)
    write_csvs(config, tables, SCHEMA)
    db_path = write_sqlite(config, tables, SCHEMA)
    write_json(
        config.path("out") / "tables_schema.json",
        {"database": db_path.name, "tables": {k: v.strip() for k, v in SCHEMA.items()}},
    )
    return {name: len(rows) for name, rows in tables.items()}
