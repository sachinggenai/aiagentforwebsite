"""Vector / naive RAG chunks.

Deliberately **not** a fixed-size splitter. The whole corpus is only ~50–70K
tokens, so the property that matters is *self-containment*, not uniformity — a
fixed window would slice a specification table in half and separate an FAQ
answer from its question.

Granularity:

* one chunk per product section (identity, specs, trade terms, about)
* **one chunk per FAQ pair** — the highest-precision retrieval unit available,
  and the site supplies these verbatim so they need no generation
* one chunk per non-product page section

Every chunk carries a ``context_prefix`` naming the product, its **numeric id**
and its category. The id matters: two different products on this site are both
named "Medical Dry Imager" at different prices, so a bare product name in a
voice answer would be ambiguous. The prefix is a separate field so the chat
layer can display clean text while the embedder still sees the context.
"""

from __future__ import annotations

from ..config import Config
from ..dom import sha256_text, text_sha
from ..models import PageDocument, SiteShared
from . import write_jsonl

# Rough characters-per-token for English. Good enough to route a chunk between
# "speak it" and "summarise it" without pulling in a tokenizer.
CHARS_PER_TOKEN = 4

ABOUT_SPLIT_THRESHOLD = 1800


def _processable(text: str) -> bool:
    return bool(text) and len(text.strip()) >= 40


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def _base_chunk(
    doc: PageDocument,
    *,
    chunk_type: str,
    section: str,
    text: str,
    context_prefix: str,
    speakable: bool,
) -> dict:
    return {
        "chunk_id": f"{doc.doc_id}:{section}:{text_sha(text)}",
        "doc_id": doc.doc_id,
        "chunk_type": chunk_type,
        "section": section,
        "context_prefix": context_prefix,
        "text": text,
        "embed_text": f"{context_prefix}\n\n{text}" if context_prefix else text,
        "source_url": doc.url,
        "page_type": doc.page_type,
        "product_id": doc.product_id,
        "product_name": doc.product_name,
        "category": doc.category_name,
        "price_amount": doc.offer.price if doc.offer else None,
        "price_display": (
            _price_display(doc) if doc.offer and doc.offer.price is not None else None
        ),
        "speakable": speakable,
        "lang": "en-IN",
        "token_estimate": _estimate_tokens(text),
        "char_count": len(text),
        "content_hash": sha256_text(text),
        "crawled_at": doc.crawl.fetched_at.isoformat(),
    }


def _price_display(doc: PageDocument) -> str | None:
    from ..normalize import parse_price

    if not doc.offer or doc.offer.price is None:
        return None
    return parse_price(str(doc.offer.price), doc.offer.price_unit).display


def _product_prefix(doc: PageDocument) -> str:
    """Context prefix that disambiguates same-named products."""
    parts = [doc.product_name or "Product"]
    if doc.product_id:
        parts.append(f"[{doc.product_id}]")
    if doc.category_name:
        parts.append(f"| Category: {doc.category_name}")
    if doc.offer and doc.offer.price is not None:
        display = _price_display(doc)
        if display:
            parts.append(f"| Price: {display}")
    if doc.meta.description:
        parts.append(f"| {doc.meta.description[:180]}")
    return " ".join(parts)


def _split_about(text: str) -> list[str]:
    """Split long About prose on paragraph boundaries only."""
    if len(text) <= ABOUT_SPLIT_THRESHOLD:
        return [text]
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 1 > ABOUT_SPLIT_THRESHOLD and current:
            chunks.append(current.strip())
            current = paragraph
        else:
            current = f"{current}\n{paragraph}" if current else paragraph
    if current.strip():
        chunks.append(current.strip())
    return chunks


def build_chunks(documents: list[PageDocument], shared: SiteShared) -> list[dict]:
    """Build every chunk from the canonical documents."""
    chunks: list[dict] = []

    for doc in documents:
        if doc.page_type == "product":
            prefix = _product_prefix(doc)
            name = doc.product_name or "Product"

            # --- identity: the fact sheet a price/MOQ question needs ------
            identity_lines = [f"Product: {name}"]
            if doc.product_id:
                identity_lines.append(f"Product ID: {doc.product_id}")
            if doc.category_name:
                identity_lines.append(f"Category: {doc.category_name}")
            if doc.offer:
                display = _price_display(doc)
                if display:
                    identity_lines.append(f"Price: {display} (list price, excluding taxes)")
                    identity_lines.append("Price is indicative — a formal quotation is required.")
                if doc.offer.moq_value is not None:
                    identity_lines.append(f"Minimum Order Quantity: {doc.offer.moq_value} {doc.offer.moq_unit or ''}".strip())
                if doc.offer.delivery_lead_time:
                    identity_lines.append(f"Delivery Time: {doc.offer.delivery_lead_time}")
                if doc.offer.supply_ability:
                    identity_lines.append(f"Supply Ability: {doc.offer.supply_ability}")
                if doc.offer.main_domestic_market:
                    identity_lines.append(f"Main Domestic Market: {doc.offer.main_domestic_market}")
            identity_text = "\n".join(identity_lines)
            if _processable(identity_text):
                chunks.append(
                    _base_chunk(
                        doc,
                        chunk_type="product_identity",
                        section="identity",
                        text=identity_text,
                        context_prefix="",
                        speakable=True,
                    )
                )

            # --- specs ---------------------------------------------------
            # Built from `doc.specs` — the same canonical list the `product_specs`
            # table is built from — rather than from the page's Specification
            # section.
            #
            # Those two disagreed, and the disagreement was invisible: 32
            # specification rows were in the database and absent from the RAG,
            # because three products carry their specifications only in the
            # structured data (their page has no Specification *section* at all)
            # and two more have a section of 12 and 16 characters, which the
            # 40-character prose floor silently discarded.
            #
            # A floor is right for prose and wrong for a specification, where
            # "Yes" and "Manual" are complete answers. Rendering the structured
            # list sidesteps the question: the chunk is as long as the data is.
            spec_text = "\n".join(
                f"{spec.name}: {spec.value}".strip(": ")
                for spec in doc.specs
                if spec.name and spec.value
            )
            if spec_text:
                chunks.append(
                    _base_chunk(
                        doc,
                        chunk_type="product_specs",
                        section="specification",
                        text=spec_text,
                        context_prefix=prefix,
                        speakable=False,
                    )
                )

            trade_text = doc.section_text("trade_information")
            if _processable(trade_text):
                chunks.append(
                    _base_chunk(
                        doc,
                        chunk_type="product_trade",
                        section="trade_information",
                        text=trade_text,
                        context_prefix=prefix,
                        speakable=True,
                    )
                )

            # --- about ---------------------------------------------------
            about = doc.section_text("about")
            if _processable(about):
                for index, part in enumerate(_split_about(about)):
                    section = "about" if index == 0 else f"about_{index + 1}"
                    chunks.append(
                        _base_chunk(
                            doc,
                            chunk_type="product_about",
                            section=section,
                            text=part,
                            context_prefix=prefix,
                            speakable=False,
                        )
                    )

            # --- FAQ: one chunk per pair --------------------------------
            for index, faq in enumerate(doc.faqs):
                # The question and its answer read as prose, with no "Q:" / "A:"
                # labels. Three consumers take this text and none of them wants
                # the labels: they are noise tokens to the embedding, a TTS
                # engine reads "Q colon" aloud, and the chat surface returns the
                # chunk verbatim to a buyer, who would see them in the bubble.
                # The question already ends in "?" on 36 of 36 pairs; the period
                # is for the ones that do not.
                question = faq.question.rstrip()
                if not question.endswith(("?", ".", "!")):
                    question += "."
                text = f"{question} {faq.answer}"
                chunks.append(
                    _base_chunk(
                        doc,
                        chunk_type="product_faq",
                        section=f"faq_{index + 1}",
                        text=text,
                        context_prefix=prefix,
                        speakable=True,
                    )
                )

        elif doc.page_type == "category":
            intro = doc.section_text("category_listing")
            if _processable(intro):
                chunks.append(
                    _base_chunk(
                        doc,
                        chunk_type="category_listing",
                        section="category_listing",
                        text=intro,
                        context_prefix=f"Category: {doc.category_name or doc.title}\n\n",
                        speakable=False,
                    )
                )

        elif doc.page_type in ("company_profile", "contact", "home", "products_index", "sitemap_page"):
            for section in doc.sections:
                if not _processable(section.text):
                    continue
                chunks.append(
                    _base_chunk(
                        doc,
                        chunk_type=f"{doc.page_type}_{section.key}",
                        section=section.key,
                        text=section.text,
                        context_prefix=f"{shared.company_name or 'Company'} — {doc.page_type.replace('_', ' ').title()}",
                        speakable=doc.page_type in ("company_profile", "contact"),
                    )
                )

    # --- site-wide shared block: one chunk, not 32 -----------------------
    shared_lines: list[str] = []
    if shared.company_name:
        shared_lines.append(f"Company: {shared.company_name}")
    for label, value in (shared.addresses or {}).items():
        shared_lines.append(f"{label.replace('_', ' ').title()} address: {value}")
    if shared.phone_numbers:
        shared_lines.append(f"Phone: {', '.join(shared.phone_numbers)}")
    if shared.gst_numbers:
        shared_lines.append(f"GST numbers: {', '.join(shared.gst_numbers)}")
    if shared_lines:
        text = "\n".join(shared_lines)
        chunks.append(
            {
                "chunk_id": f"{shared.doc_id}:contact:0",
                "doc_id": shared.doc_id,
                "chunk_type": "site_contact",
                "section": "contact",
                "context_prefix": "",
                "text": text,
                "embed_text": text,
                "source_url": shared.source_url,
                "page_type": "shared",
                "product_id": None,
                "product_name": None,
                "category": None,
                "price_amount": None,
                "price_display": None,
                "speakable": True,
                "lang": "en-IN",
                "token_estimate": max(1, len(text) // CHARS_PER_TOKEN),
                "char_count": len(text),
                "content_hash": sha256_text(text),
                "crawled_at": None,
            }
        )

    return chunks


def emit(config: Config, documents: list[PageDocument], shared: SiteShared) -> dict[str, int]:
    """Write ``chunks.jsonl``."""
    chunks = build_chunks(documents, shared)
    count = write_jsonl(config.path("out") / "chunks.jsonl", chunks)
    by_type: dict[str, int] = {}
    for chunk in chunks:
        by_type[chunk["chunk_type"]] = by_type.get(chunk["chunk_type"], 0) + 1
    return {"chunks": count, **by_type}
