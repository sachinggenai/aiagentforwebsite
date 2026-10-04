"""Validation gates.

Turns silent corruption into a visible failure. Each gate is a property the
corpus must satisfy; a failure is reported with the offending records named, not
just a count.

The gates that matter most here:

* **Chrome gate** — the inquiry modal and footer repeat on all 32 pages. If any
  of that text reaches a chunk, retrieval degrades. Checked against a literal
  marker list.
* **Corruption gate** — catches Draft.js space damage (``"cass ettes"``,
  ``"X - Ray"``, ``"film ."``) reappearing. This is the defect the whole text
  pipeline exists to prevent, so it is asserted directly rather than assumed.
* **Budget gate** — compares raw markup bytes to extracted characters per
  section. A section that extracts far more or less than expected means the
  scoping slipped and captured a neighbouring block (the exact bug that once
  captured 57 KB of modal boilerplate).
* **Graph gate** — every product must reach its category; an orphan node means
  a broken relation, and a graph RAG traversing it would answer wrongly.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .dom import CHROME_MARKERS
from .models import PageDocument, SiteShared

# Patterns that indicate Draft.js space-splitting survived into the corpus.
#
# Deliberately high-precision. An earlier, looser version flagged legitimate
# text — `14" x 17" , 14" x 14"` (a real dimension list) and `1 , , Unit`
# (malformed *in the source markup*, not by us) — which would have made the gate
# cry wolf and taught everyone to ignore it.
CORRUPTION_PATTERNS = (
    # "film .": a space before sentence punctuation after a word. Never
    # legitimate in this corpus.
    re.compile(r"\b[a-z]{2,} [.,](?:\s|$)"),
)

# Explicit regression probes. These are the exact strings the Draft.js
# space-splitting produced before it was fixed, so a reappearance is
# unambiguous.
#
# A generic "word - word" hyphen pattern was tried and removed: it fired on
# legitimate spaced hyphens that occur in the source, such as the company name
# "NSIC - CRISIL" and the specification "Ceiling - Option". Probe-based
# checking catches the real defect without crying wolf on real content.
CORRUPTION_PROBES = (
    "cass ettes",
    "X - Ray",
    "light - tight",
    "radi ography",
    "fluor osc opy",
    "computer ized",
    "film .",
)

EXPECTED = {"pages": 32, "products": 24, "categories": 3}


@dataclass
class Gate:
    """One validation check."""

    name: str
    passed: bool
    detail: str
    failures: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "PASS" if self.passed else "FAIL"


def _check_coverage(documents: list[PageDocument], manifest: dict | None) -> Gate:
    by_type: dict[str, int] = {}
    for doc in documents:
        by_type[doc.page_type] = by_type.get(doc.page_type, 0) + 1

    problems: list[str] = []
    if len(documents) != EXPECTED["pages"]:
        problems.append(f"parsed {len(documents)} pages, expected {EXPECTED['pages']}")
    if by_type.get("product", 0) != EXPECTED["products"]:
        problems.append(
            f"found {by_type.get('product', 0)} products, expected {EXPECTED['products']}"
        )
    if by_type.get("category", 0) != EXPECTED["categories"]:
        problems.append(
            f"found {by_type.get('category', 0)} categories, expected {EXPECTED['categories']}"
        )

    detail = ", ".join(f"{k}={v}" for k, v in sorted(by_type.items()))
    return Gate("Coverage", not problems, detail, problems)


def _check_products(documents: list[PageDocument]) -> Gate:
    products = [d for d in documents if d.page_type == "product"]
    problems: list[str] = []
    for doc in products:
        name = doc.product_name
        if not name:
            problems.append(f"{doc.product_id}: missing product name")
        if doc.offer is None or doc.offer.price is None:
            problems.append(f"{doc.product_id}: missing price")
        source_absent = {
            w.split(":", 1)[1]
            for w in doc.parse_warnings
            if w.startswith("section_absent_on_source:")
        }
        if not doc.specs and not doc.section_text("trade_information") and "specs" not in source_absent:
            problems.append(f"{doc.product_id}: no specs and no trade information")
        # Only a failure when we lost content that exists. Four products
        # genuinely have an empty About container on the site, which is data
        # absence and is recorded as such rather than reported as our bug.
        if not doc.section_text("about") and "about" not in source_absent:
            problems.append(f"{doc.product_id}: missing About text")
        if doc.offer and doc.offer.moq_value is None:
            problems.append(f"{doc.product_id}: MOQ not parsed")

    with_specs = sum(1 for d in products if d.specs)
    detail = (
        f"{len(products)} products; {with_specs} with specs; "
        f"{sum(1 for d in products if d.faqs)} with FAQs"
    )
    return Gate("Product completeness", not problems, detail, problems)


def _check_chrome(documents: list[PageDocument], config: Config) -> Gate:
    problems: list[str] = []
    chunks_path = config.path("out") / "chunks.jsonl"
    total = 0
    if chunks_path.exists():
        for line in chunks_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            total += 1
            chunk = json.loads(line)
            for marker in CHROME_MARKERS:
                if marker in chunk.get("text", "") or marker in chunk.get("context_prefix", ""):
                    problems.append(f"{chunk['chunk_id']}: contains chrome marker {marker!r}")
                    break

    # Also check the parsed About sections, which feed the chunks.
    for doc in documents:
        about = doc.section_text("about")
        for marker in CHROME_MARKERS:
            if marker in about:
                problems.append(f"{doc.doc_id}: About contains {marker!r}")

    return Gate("Chrome leakage", not problems, f"{total} chunks scanned", problems[:20])


def _check_corruption(documents: list[PageDocument], config: Config) -> Gate:
    problems: list[str] = []
    chunks_path = config.path("out") / "chunks.jsonl"
    if chunks_path.exists():
        for line in chunks_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            chunk = json.loads(line)
            text = chunk.get("text", "")
            for pattern in CORRUPTION_PATTERNS:
                for match in pattern.finditer(text):
                    snippet = match.group(0).strip()
                    problems.append(
                        f"{chunk['chunk_id']}: possible corruption {snippet!r} "
                        f"in {text[max(0, match.start()-30):match.end()+30]!r}"
                    )
                    break

    # Explicit high-signal probes across both parsed sections and chunks.
    for doc in documents:
        for probe in CORRUPTION_PROBES:
            for section in doc.sections:
                if probe in section.text:
                    problems.append(f"{doc.doc_id}/{section.key}: contains {probe!r}")
            for faq in doc.faqs:
                if probe in faq.answer or probe in faq.question:
                    problems.append(f"{doc.doc_id}/faq: contains {probe!r}")

    return Gate("Text corruption", not problems, "Draft.js space damage", problems[:20])


def _check_budget(documents: list[PageDocument], config: Config) -> Gate:
    """Raw markup bytes per section vs extracted characters.

    Catches a scoping slip: if a section grabs the wrong block it will extract
    an implausible amount of text for its markup.
    """
    problems: list[str] = []
    ratios: list[str] = []
    for doc in documents:
        if doc.page_type != "product":
            continue
        raw_bytes = doc.crawl.bytes or 0
        extracted = sum(s.char_count for s in doc.sections) + sum(len(f.answer) for f in doc.faqs)
        if raw_bytes <= 0:
            continue
        # Content is ~1-3% of a 540KB page dominated by inline CSS and modals.
        ratio = extracted / raw_bytes
        ratios.append(f"{doc.product_id}:{ratio:.3%}")
        if extracted > raw_bytes * 0.5:
            problems.append(
                f"{doc.product_id}: extracted {extracted} chars from {raw_bytes} bytes "
                f"({ratio:.1%}) — section scoping has probably over-captured"
            )
        if extracted < 200:
            problems.append(f"{doc.product_id}: only {extracted} chars extracted")

    detail = f"ratios {min(ratios) if ratios else 'n/a'}–{max(ratios) if ratios else 'n/a'}"
    return Gate("Extraction budget", not problems, detail, problems)


def _check_graph(config: Config) -> Gate:
    problems: list[str] = []
    nodes_path = config.path("out") / "graph_nodes.jsonl"
    edges_path = config.path("out") / "graph_edges.jsonl"
    if not (nodes_path.exists() and edges_path.exists()):
        return Gate("Graph integrity", False, "graph files missing", ["run 04_emit.py"])

    nodes = [json.loads(l) for l in nodes_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    edges = [json.loads(l) for l in edges_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    node_ids = {n["node_id"] for n in nodes}
    product_ids = {n["node_id"] for n in nodes if n["node_type"] == "Product"}

    dangling = sorted({e["source"] for e in edges} | {e["target"] for e in edges} - node_ids)
    dangling = [d for d in dangling if d not in node_ids]
    if dangling:
        problems.append(f"{len(dangling)} edge endpoint(s) reference missing nodes: {dangling[:5]}")

    has_category = {e["source"] for e in edges if e["relation"] == "IN_CATEGORY"}
    orphans = sorted(product_ids - has_category)
    if orphans:
        problems.append(f"{len(orphans)} product(s) have no category edge: {orphans[:5]}")

    detail = f"{len(nodes)} nodes, {len(edges)} edges, {len(product_ids)} products linked"
    return Gate("Graph integrity", not problems, detail, problems)


def _check_api_inventory(config: Config) -> Gate:
    path = config.path("out") / "api_inventory.json"
    if not path.exists():
        return Gate("API inventory", False, "api_inventory.json missing", [])
    inventory = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    if inventory["totals"]["endpoints"] == 0:
        problems.append("no endpoints discovered")
    for entry in inventory["endpoints"]:
        if not entry.get("kinds"):
            problems.append(f"{entry['path']}: no kind recorded")
    detail = (
        f"{inventory['totals']['endpoints']} endpoints "
        f"({inventory['totals']['allowed']} allowed, "
        f"{inventory['totals']['disallowed']} robots-disallowed)"
    )
    return Gate("API inventory", not problems, detail, problems[:10])


def _check_sqlite(config: Config) -> Gate:
    db_path = config.path("out") / "ams.sqlite"
    if not db_path.exists():
        return Gate("SQLite lookups", False, "ams.sqlite missing", [])
    problems: list[str] = []
    connection = sqlite3.connect(db_path)
    try:
        count = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        if count != EXPECTED["products"]:
            problems.append(f"products table has {count} rows, expected {EXPECTED['products']}")
        null_prices = connection.execute(
            "SELECT COUNT(*) FROM products WHERE price_amount IS NULL"
        ).fetchone()[0]
        if null_prices:
            problems.append(f"{null_prices} product(s) have a NULL price")
        # The lookup the chat layer will actually run.
        rows = connection.execute(
            "SELECT name, price_amount FROM products WHERE price_amount < 1000 ORDER BY price_amount"
        ).fetchall()
        if not rows:
            problems.append("numeric price lookup returned nothing")
        detail = f"{count} products, {len(rows)} under ₹1000"
    finally:
        connection.close()
    return Gate("SQLite lookups", not problems, detail, problems)


def _check_shared(shared: SiteShared) -> Gate:
    problems: list[str] = []
    if not shared.company_name:
        problems.append("company name not resolved")
    if not shared.addresses:
        problems.append("no addresses extracted")
    if not shared.gst_numbers:
        problems.append("no GST numbers extracted")
    if not shared.phone_numbers:
        problems.append("no phone numbers extracted")
    detail = (
        f"nav={len(shared.navigation)} addresses={len(shared.addresses)} "
        f"gst={len(shared.gst_numbers)} phones={len(shared.phone_numbers)}"
    )
    return Gate("Shared site block", not problems, detail, problems)


def _check_determinism(config: Config, documents: list[PageDocument]) -> Gate:
    """Re-running the emitters must produce byte-identical chunks."""
    chunks_path = config.path("out") / "chunks.jsonl"
    if not chunks_path.exists():
        return Gate("Determinism", False, "chunks.jsonl missing", [])
    import hashlib

    from .emit import chunks as chunks_emitter
    from .pipeline import load_shared

    first = hashlib.sha256(chunks_path.read_bytes()).hexdigest()
    shared = load_shared(config)
    regenerated = chunks_emitter.build_chunks(documents, shared)
    payload = "\n".join(
        json.dumps(row, ensure_ascii=False) for row in regenerated
    ) + "\n"
    second = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    problems = [] if first == second else ["regenerated chunks differ from the emitted file"]
    return Gate("Determinism", not problems, f"sha256 {first[:16]}…", problems)


@dataclass
class ValidationReport:
    gates: list[Gate]
    generated_at: datetime

    @property
    def passed(self) -> bool:
        return all(gate.passed for gate in self.gates)


def validate(config: Config) -> ValidationReport:
    """Run every gate and return the report."""
    from .pipeline import load_documents, load_shared

    documents = load_documents(config)
    shared = load_shared(config)

    manifest = None
    manifest_path = config.path("url_manifest")
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    gates = [
        _check_coverage(documents, manifest),
        _check_products(documents),
        _check_shared(shared),
        _check_chrome(documents, config),
        _check_corruption(documents, config),
        _check_budget(documents, config),
        _check_graph(config),
        _check_api_inventory(config),
        _check_sqlite(config),
        _check_determinism(config, documents),
    ]
    return ValidationReport(gates=gates, generated_at=datetime.now(timezone.utc))


def render_markdown(report: ValidationReport, config: Config) -> str:
    """Render the report as markdown."""
    lines = [
        "# Validation Report",
        "",
        f"Generated: {report.generated_at.isoformat()}",
        "",
        f"**Overall: {'PASS' if report.passed else 'FAIL'}**",
        "",
        "| Gate | Status | Detail |",
        "| --- | --- | --- |",
    ]
    for gate in report.gates:
        lines.append(f"| {gate.name} | {'✅ PASS' if gate.passed else '❌ FAIL'} | {gate.detail} |")

    failed = [g for g in report.gates if not g.passed]
    if failed:
        lines += ["", "## Failures", ""]
        for gate in failed:
            lines.append(f"### {gate.name}")
            lines.append("")
            for item in gate.failures:
                lines.append(f"- {item}")
            lines.append("")

    lines += [
        "",
        "## What each gate protects against",
        "",
        "- **Coverage** — a page silently dropped by discovery or the parser.",
        "- **Product completeness** — a product reaching RAG with no price, specs or prose.",
        "- **Chrome leakage** — inquiry-modal and footer text flooding retrieval.",
        "- **Text corruption** — Draft.js space damage (`cass ettes`, `X - Ray`) in the corpus.",
        "- **Extraction budget** — a section scoping slip capturing a neighbouring block.",
        "- **Graph integrity** — dangling edges or products with no category.",
        "- **API inventory** — endpoints referenced but not documented.",
        "- **SQLite lookups** — the deterministic answer path returning nothing.",
        "- **Determinism** — re-running the emitters producing different output.",
        "",
    ]
    return "\n".join(lines)


def emit_report(config: Config, report: ValidationReport) -> Path:
    """Write ``reports/validation.md``."""
    path = config.path("reports") / "validation.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(report, config), encoding="utf-8")
    return path
