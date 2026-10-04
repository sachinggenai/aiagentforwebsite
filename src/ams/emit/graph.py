"""Graph RAG nodes and edges.

Built **only from explicit evidence** — breadcrumbs, category membership,
related-product links, the statutory table, and FAQ/media ownership. No
co-occurrence edges are inferred: on a 24-product corpus, inferred relations
would be mostly noise, and a graph RAG that traverses a wrong edge produces a
confidently wrong answer.

Node ids are namespaced (``product:270691``, ``category:x-ray-accessories``) so
a product and a category can never collide on a shared name.
"""

from __future__ import annotations

import re

from ..config import Config
from ..models import PageDocument, SiteShared
from . import write_jsonl


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").casefold()).strip("-")


def build_graph(
    documents: list[PageDocument], shared: SiteShared
) -> tuple[list[dict], list[dict]]:
    """Return ``(nodes, edges)``."""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    seen_edges: set[tuple] = set()

    def add_node(node_id: str, node_type: str, label: str, **props) -> None:
        if node_id in nodes:
            nodes[node_id].update({k: v for k, v in props.items() if v is not None})
            return
        nodes[node_id] = {
            "node_id": node_id,
            "node_type": node_type,
            "label": label,
            **{k: v for k, v in props.items() if v is not None},
        }

    def add_edge(source: str, relation: str, target: str, evidence: str, source_url: str | None = None) -> None:
        key = (source, relation, target)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edges.append(
            {
                "source": source,
                "relation": relation,
                "target": target,
                "evidence": evidence,
                "source_url": source_url,
            }
        )

    company_id = "company:anita-medical-systems"
    add_node(
        company_id,
        "Company",
        shared.company_name or "ANITA MEDICAL SYSTEMS PVT. LTD.",
        gst=shared.gst_numbers,
        phones=shared.phone_numbers,
    )

    # --- products, categories, brands ------------------------------------
    for doc in documents:
        if doc.page_type == "product":
            product_id = f"product:{doc.product_id}"
            add_node(
                product_id,
                "Product",
                doc.product_name or "",
                product_id=doc.product_id,
                url=doc.url,
                category=doc.category_name,
                price=doc.offer.price if doc.offer else None,
                price_currency=doc.offer.currency if doc.offer else None,
                moq=doc.offer.moq_value if doc.offer else None,
                delivery=doc.offer.delivery_lead_time if doc.offer else None,
            )
            add_edge(company_id, "SUPPLIES", product_id, "catalogue membership", doc.url)

            if doc.category_name:
                category_id = f"category:{_slug(doc.category_name)}"
                add_node(category_id, "Category", doc.category_name, url=None)
                add_edge(product_id, "IN_CATEGORY", category_id, "breadcrumb", doc.url)

            if doc.structured and doc.structured.brand:
                brand_id = f"brand:{_slug(doc.structured.brand)}"
                add_node(brand_id, "Brand", doc.structured.brand)
                add_edge(product_id, "SOLD_BY", brand_id, "jsonld.brand", doc.url)

            for index, faq in enumerate(doc.faqs):
                faq_id = f"faq:{doc.product_id}:{index}"
                add_node(faq_id, "FAQ", faq.question, answer=faq.answer)
                add_edge(product_id, "HAS_FAQ", faq_id, "page markup", doc.url)

            for item in doc.media:
                if item.role == "template":
                    continue
                media_id = f"media:{item.url}"
                add_node(media_id, "Media", item.url, role=item.role, product_id=item.product_id)
                add_edge(product_id, "HAS_IMAGE", media_id, f"role={item.role}", doc.url)

            for rel in (doc.relations or {}).get("related_products", []) or []:
                related_match = re.search(r"-(\d{5,7})\.html$", rel.get("url", ""))
                if related_match:
                    add_edge(
                        product_id,
                        "RELATED_TO",
                        f"product:{related_match.group(1)}",
                        "related-products widget",
                        doc.url,
                    )

    # --- company profile: people, certifications, branches, identifiers --
    for doc in documents:
        if doc.page_type == "company_profile":
            facts = doc.company_facts
            for person in facts.get("directors", []) or []:
                person_id = f"person:{_slug(person.get('name', ''))}"
                add_node(person_id, "Person", person.get("name", ""), role=person.get("role"))
                add_edge(company_id, "HAS_DIRECTOR", person_id, "statutory table", doc.url)

            for kind in ("iso", "nsic_crisil"):
                for value in facts.get(kind, []) or []:
                    cert_id = f"certification:{_slug(value)}"
                    add_node(cert_id, "Certification", value, kind=kind)
                    add_edge(company_id, "HAS_CERTIFICATION", cert_id, "statutory table", doc.url)

            for kind in ("gst", "pan", "iec", "drug_license"):
                for value in facts.get(kind, []) or []:
                    add_node(company_id, "Company", shared.company_name or "", **{kind: value})

            for label, value in (facts.get("statutory", {}) or {}).items():
                if "office" in label.casefold() or "address" in label.casefold():
                    location_id = f"location:{_slug(value[:40])}"
                    add_node(location_id, "Location", value)
                    add_edge(company_id, "HAS_BRANCH", location_id, f"statutory:{label}", doc.url)

        if doc.page_type == "contact":
            for person in doc.contact_facts.get("persons", []) or []:
                person_id = f"person:{_slug(person.get('name', ''))}"
                add_node(person_id, "Person", person.get("name", ""), role=person.get("role"))
                add_edge(company_id, "HAS_CONTACT", person_id, "contact page", doc.url)
            for label, value in (doc.contact_facts.get("addresses", {}) or {}).items():
                location_id = f"location:{_slug(label)}"
                add_node(location_id, "Location", value, location_key=label)
                add_edge(company_id, "HAS_BRANCH", location_id, "contact page", doc.url)

        if doc.page_type == "home":
            for brand in doc.company_facts.get("brands_represented", []) or []:
                brand_id = f"brand:{_slug(brand)}"
                add_node(brand_id, "Brand", brand)
                add_edge(company_id, "REPRESENTS", brand_id, "about ourselves", doc.url)

    # --- shared addresses and phones -------------------------------------
    for label, value in (shared.addresses or {}).items():
        location_id = f"location:{_slug(label)}"
        add_node(location_id, "Location", value, location_key=label)
        add_edge(company_id, "HAS_BRANCH", location_id, "site-wide footer", shared.source_url)

    return list(nodes.values()), edges


def emit(config: Config, documents: list[PageDocument], shared: SiteShared) -> dict[str, int]:
    """Write ``graph_nodes.jsonl`` and ``graph_edges.jsonl``."""
    out = config.path("out")
    nodes, edges = build_graph(documents, shared)
    node_count = write_jsonl(out / "graph_nodes.jsonl", nodes)
    edge_count = write_jsonl(out / "graph_edges.jsonl", edges)
    by_type: dict[str, int] = {}
    for node in nodes:
        by_type[node["node_type"]] = by_type.get(node["node_type"], 0) + 1
    relations: dict[str, int] = {}
    for edge in edges:
        relations[edge["relation"]] = relations.get(edge["relation"], 0) + 1
    return {"nodes": node_count, "edges": edge_count, **by_type, **relations}
