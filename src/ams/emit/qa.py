"""Question/answer pairs and speakable short answers.

Three sources, in order of trust:

1. **Verbatim FAQ pairs** from the site — highest quality, no generation, and
   tagged ``source: "site"`` so provenance is unambiguous.
2. **Derived fact answers** built from the structured records (price, MOQ,
   delivery, contact, certifications). These are lookups, so they are exact.
3. **Voice short answers** — a separate ``voice_short_answers.jsonl`` sized for
   Twilio voice and WhatsApp.

Why a separate speakable file: a voice bot cannot read a paragraph aloud, and
WhatsApp messages get truncated. Short answers for money must also use *spoken*
numerals — a TTS engine reading "1450000" is unintelligible, whereas "about
14.5 lakh rupees" is how the number would actually be said.

``gaps.jsonl`` records what the site **cannot** answer (stock levels, warranty
terms, clinical advice). Without it, a chatbot has no honest fallback and will
invent an answer; with it, it can hand off to a human.
"""

from __future__ import annotations

from ..config import Config
from ..models import PageDocument, SiteShared
from ..normalize import parse_lead_time_days, parse_price
from . import write_jsonl

# Every price quoted by a bot must carry this caveat: the site shows list prices
# and requires a quotation for an actual order.
PRICE_CAVEAT = "This is a list price, excluding taxes; a formal quotation is required."


def _money(doc: PageDocument) -> tuple[str, str] | None:
    """Return ``(display, spoken)`` for a product's price."""
    if not doc.offer or doc.offer.price is None:
        return None
    money = parse_price(str(doc.offer.price), doc.offer.price_unit)
    return money.display, money.spoken


def _qa(
    *,
    question: str,
    long_answer: str,
    speakable_answer: str,
    answer_type: str,
    grounded_on: str,
    source_url: str | None,
    product_id: str | None = None,
    product_name: str | None = None,
    source: str = "derived",
) -> dict:
    return {
        "question": question,
        "long_answer": long_answer,
        "speakable_answer": speakable_answer,
        "answer_type": answer_type,  # verbatim | derived_fact | derived_contact
        "grounded_on": grounded_on,  # field path the answer came from
        "source": source,  # site | derived
        "product_id": product_id,
        "product_name": product_name,
        "source_url": source_url,
        "lang": "en-IN",
        "disclaimer": PRICE_CAVEAT if answer_type == "derived_fact" and "price" in grounded_on else None,
    }


def build_qa(documents: list[PageDocument], shared: SiteShared) -> list[dict]:
    """Build the full Q&A set."""
    rows: list[dict] = []
    products = [d for d in documents if d.page_type == "product"]

    # --- 1. Verbatim site FAQs -------------------------------------------
    for doc in products:
        for faq in doc.faqs:
            rows.append(
                _qa(
                    question=faq.question,
                    long_answer=faq.answer,
                    speakable_answer=_shorten(faq.answer),
                    answer_type="verbatim",
                    grounded_on="faqs",
                    source_url=doc.url,
                    product_id=doc.product_id,
                    product_name=doc.product_name,
                    source="site",
                )
            )

    # --- 2. Derived per-product facts ------------------------------------
    for doc in products:
        name = doc.product_name or "this product"
        money = _money(doc)
        if money:
            display, spoken = money
            rows.append(
                _qa(
                    question=f"What is the price of {name}?",
                    long_answer=(
                        f"{name} is listed at {display} per {doc.offer.price_unit or 'unit'}. "
                        f"{PRICE_CAVEAT}"
                    ),
                    speakable_answer=f"{name} is listed at {spoken} per {doc.offer.price_unit or 'unit'}.",
                    answer_type="derived_fact",
                    grounded_on="offer.price",
                    source_url=doc.url,
                    product_id=doc.product_id,
                    product_name=name,
                )
            )

        if doc.offer and doc.offer.moq_value is not None:
            moq = f"{doc.offer.moq_value:g} {doc.offer.moq_unit or ''}".strip()
            rows.append(
                _qa(
                    question=f"What is the minimum order quantity for {name}?",
                    long_answer=f"The minimum order quantity for {name} is {moq}.",
                    speakable_answer=f"The minimum order for {name} is {moq}.",
                    answer_type="derived_fact",
                    grounded_on="offer.moq_value",
                    source_url=doc.url,
                    product_id=doc.product_id,
                    product_name=name,
                )
            )

        if doc.offer and doc.offer.delivery_lead_time:
            days = parse_lead_time_days(doc.offer.delivery_lead_time)
            spoken_days = f"about {days} days" if days else doc.offer.delivery_lead_time
            rows.append(
                _qa(
                    question=f"What is the delivery time for {name}?",
                    long_answer=f"Delivery time for {name} is {doc.offer.delivery_lead_time}.",
                    speakable_answer=f"Delivery for {name} takes {spoken_days}.",
                    answer_type="derived_fact",
                    grounded_on="offer.delivery_lead_time",
                    source_url=doc.url,
                    product_id=doc.product_id,
                    product_name=name,
                )
            )

        if doc.specs:
            spec_text = "; ".join(f"{s.name}: {s.value}" for s in doc.specs[:8])
            rows.append(
                _qa(
                    question=f"What are the specifications of {name}?",
                    long_answer=f"{name} specifications — {spec_text}.",
                    speakable_answer=f"{name} — {_shorten(spec_text, 30)}",
                    answer_type="derived_fact",
                    grounded_on="specs",
                    source_url=doc.url,
                    product_id=doc.product_id,
                    product_name=name,
                )
            )

        if doc.category_name:
            rows.append(
                _qa(
                    question=f"Which category does {name} belong to?",
                    long_answer=f"{name} belongs to the {doc.category_name} category.",
                    speakable_answer=f"{name} is in the {doc.category_name} category.",
                    answer_type="derived_fact",
                    grounded_on="category_name",
                    source_url=doc.url,
                    product_id=doc.product_id,
                    product_name=name,
                )
            )

    # --- 3. Company-level facts ------------------------------------------
    rows.extend(_company_qa(documents, shared))
    return rows


def _company_qa(documents: list[PageDocument], shared: SiteShared) -> list[dict]:
    """Company, contact, certification and catalogue answers."""
    rows: list[dict] = []
    company = next((d for d in documents if d.page_type == "company_profile"), None)
    contact = next((d for d in documents if d.page_type == "contact"), None)
    home = next((d for d in documents if d.page_type == "home"), None)
    name = shared.company_name or "ANITA MEDICAL SYSTEMS PVT. LTD."

    addresses = dict(shared.addresses or {})
    if contact:
        addresses.update(contact.contact_facts.get("addresses", {}) or {})

    if addresses.get("mumbai"):
        rows.append(
            _qa(
                question="What is the address of the company?",
                long_answer=f"{name} — Mumbai office: {addresses['mumbai']}"
                + (f" Delhi branch: {addresses['delhi']}" if addresses.get("delhi") else ""),
                speakable_answer=f"Our Mumbai office is at {_shorten(addresses['mumbai'], 32)}.",
                answer_type="derived_contact",
                grounded_on="addresses.mumbai",
                source_url=contact.url if contact else shared.source_url,
            )
        )
    if addresses.get("delhi"):
        rows.append(
            _qa(
                question="Do you have a branch office in Delhi?",
                long_answer=f"Yes. The Delhi branch is at {addresses['delhi']}.",
                speakable_answer=f"Yes, our Delhi branch is at {_shorten(addresses['delhi'], 32)}.",
                answer_type="derived_contact",
                grounded_on="addresses.delhi",
                source_url=contact.url if contact else shared.source_url,
            )
        )

    if shared.phone_numbers:
        phone = shared.phone_numbers[0]
        spoken_phone = _speak_phone(phone)
        rows.append(
            _qa(
                question="What is your phone number?",
                long_answer=f"You can reach us on {phone}.",
                speakable_answer=f"You can call us on {spoken_phone}.",
                answer_type="derived_contact",
                grounded_on="phone_numbers",
                source_url=shared.source_url,
            )
        )

    if shared.gst_numbers:
        rows.append(
            _qa(
                question="What is your GST number?",
                long_answer="Our GST numbers are " + ", ".join(shared.gst_numbers) + ".",
                speakable_answer="Our GST numbers are " + ", ".join(_speak_gst(g) for g in shared.gst_numbers) + ".",
                answer_type="derived_contact",
                grounded_on="gst_numbers",
                source_url=shared.source_url,
            )
        )

    if company:
        directors = company.company_facts.get("directors", []) or []
        if directors:
            listing = "; ".join(f"{p['name']} ({p['role']})" for p in directors)
            rows.append(
                _qa(
                    question="Who are the directors of the company?",
                    long_answer=f"The directors and officers are: {listing}.",
                    speakable_answer="The company is led by " + _shorten(listing, 34) + ".",
                    answer_type="derived_contact",
                    grounded_on="company_facts.directors",
                    source_url=company.url,
                )
            )
        certs = (company.company_facts.get("iso", []) or []) + (
            company.company_facts.get("nsic_crisil", []) or []
        )
        if certs:
            rows.append(
                _qa(
                    question="What certifications does the company hold?",
                    long_answer="Certifications: " + "; ".join(certs) + ".",
                    speakable_answer="We hold " + _shorten("; ".join(certs), 30) + ".",
                    answer_type="derived_contact",
                    grounded_on="company_facts.iso",
                    source_url=company.url,
                )
            )
        if company.company_facts.get("pan"):
            rows.append(
                _qa(
                    question="What is your PAN number?",
                    long_answer=f"Our PAN is {company.company_facts['pan'][0]}.",
                    speakable_answer=f"Our PAN is {_speak_spelled(company.company_facts['pan'][0])}.",
                    answer_type="derived_contact",
                    grounded_on="company_facts.pan",
                    source_url=company.url,
                )
            )
        if company.company_facts.get("iec"):
            rows.append(
                _qa(
                    question="What is your Importer-Exporter Code?",
                    long_answer=f"Our Importer-Exporter Code is {company.company_facts['iec'][0]}.",
                    speakable_answer=f"Our import export code is {_speak_spelled(company.company_facts['iec'][0])}.",
                    answer_type="derived_contact",
                    grounded_on="company_facts.iec",
                    source_url=company.url,
                )
            )

    if home:
        brands = home.company_facts.get("brands_represented", []) or []
        if brands:
            rows.append(
                _qa(
                    question="Which brands do you deal in?",
                    long_answer="Brands represented: " + ", ".join(brands) + ".",
                    speakable_answer="We deal in " + _shorten(", ".join(brands), 30) + ".",
                    answer_type="derived_contact",
                    grounded_on="company_facts.brands_represented",
                    source_url=home.url,
                )
            )

    products = [d for d in documents if d.page_type == "product"]
    if products:
        listing = ", ".join(sorted({d.product_name for d in products if d.product_name}))
        rows.append(
            _qa(
                question="What products do you sell?",
                long_answer=f"We supply {len(products)} products, including: {listing}.",
                speakable_answer=(
                    f"We supply {len(products)} products including CR systems, X-ray "
                    f"accessories, pressure injectors and ultrasound gel."
                ),
                answer_type="derived_fact",
                grounded_on="products",
                source_url=products[0].url,
            )
        )
        rows.append(
            _qa(
                question="How many products do you offer?",
                long_answer=f"The catalogue lists {len(products)} products.",
                speakable_answer=f"We offer {len(products)} products.",
                answer_type="derived_fact",
                grounded_on="products",
                source_url=products[0].url,
            )
        )
    return rows


def build_gaps(documents: list[PageDocument]) -> list[dict]:
    """Questions the site does not answer, so the bot can hand off honestly."""
    return [
        {
            "topic": "stock_availability",
            "reason": "The site lists no live stock or inventory figures.",
            "suggested_response": "I don't have live stock information. Please call us to confirm availability.",
        },
        {
            "topic": "warranty_terms",
            "reason": "No warranty or guarantee terms appear anywhere on the site.",
            "suggested_response": "Warranty terms aren't listed online. Our team can confirm them for you.",
        },
        {
            "topic": "service_contracts_amc",
            "reason": "No AMC or service-contract pricing is published.",
            "suggested_response": "Service contract details aren't published online — please contact us directly.",
        },
        {
            "topic": "final_price_and_taxes",
            "reason": "Only list prices are shown; discounts, taxes and freight are not published.",
            "suggested_response": "The price shown is a list price. Please request a quotation for an exact figure.",
        },
        {
            "topic": "clinical_advice",
            "reason": "The site sells equipment and does not provide medical guidance.",
            "suggested_response": "I can help with product and order questions, but not medical advice. Please consult a clinician.",
        },
        {
            "topic": "opening_hours",
            "reason": "No business hours are published.",
            "suggested_response": "Business hours aren't listed online. Please call to check.",
        },
    ]


def _shorten(text: str, max_words: int = 40) -> str:
    """Trim to a speakable length at a word boundary."""
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(",;:") + "…"


def _speak_phone(phone: str) -> str:
    """Render a phone number digit-by-digit for text-to-speech."""
    digits = [c for c in phone if c.isdigit()]
    if digits and digits[0] == "9" and len(digits) > 10:
        digits = digits[1:]
    return " ".join(digits)


def _speak_gst(gst: str) -> str:
    """Spell a GSTIN character by character — reading it as a word is useless."""
    return " ".join(gst)


def _speak_spelled(value: str) -> str:
    return " ".join(value)


def emit(config: Config, documents: list[PageDocument], shared: SiteShared) -> dict[str, int]:
    """Write ``qa_pairs.jsonl``, ``voice_short_answers.jsonl`` and ``gaps.jsonl``."""
    out = config.path("out")
    qa = build_qa(documents, shared)
    count = write_jsonl(out / "qa_pairs.jsonl", qa)

    voice = [
        {
            "question": row["question"],
            "answer": row["speakable_answer"],
            "answer_type": row["answer_type"],
            "product_id": row["product_id"],
            "product_name": row["product_name"],
            "source_url": row["source_url"],
            "lang": "en-IN",
        }
        for row in qa
        if row.get("speakable_answer")
    ]
    voice_count = write_jsonl(out / "voice_short_answers.jsonl", voice)

    gaps = build_gaps(documents)
    gap_count = write_jsonl(out / "gaps.jsonl", gaps)

    by_type: dict[str, int] = {}
    for row in qa:
        by_type[row["answer_type"]] = by_type.get(row["answer_type"], 0) + 1

    return {"qa_pairs": count, "voice_short_answers": voice_count, "gaps": gap_count, **by_type}
