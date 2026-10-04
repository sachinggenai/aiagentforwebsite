"""Deterministic answer composition for commercial and structural facts (TRD-18, TRD-19).

This module exists because of one commercial fact: **a language model must never
be in the path of a price.** An incorrect figure, stated confidently to a buyer
who then acts on it, is a liability event rather than a bug — and it is the one
error class that no amount of grounding prompt can make impossible while a model
is generating the number.

So every fact here is read from SQLite and rendered by code. The consequences:

* **It cannot hallucinate.** There is no generation step.
* **It survives an LLM outage.** This is the one path that keeps answering when
  the model is down (`DG-03`) — the designed degradation, and the reason a
  backend failure degrades the product instead of silencing it.
* **It is exact.** "What is the MOQ on the ultrasound gel?" returns 10 Pieces
  because that is what the row says, not because a model recalled it.

The **caveat is structural, not cosmetic** (TRD-19). Prices are list prices and
require a quotation; the corpus records that per row, and
:func:`AnitaAnswers.commercial` appends the caveat in the same function that
produces the number. A caller cannot obtain the figure without it, because there
is no code path that produces one and not the other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .corpus import PRICE_CAVEAT, Corpus, Product, ProductView

# ---------------------------------------------------------------------------
# Product resolution (BRD-09)
# ---------------------------------------------------------------------------
class ResolutionKind(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class Resolution:
    """The outcome of matching a spoken or typed product reference."""

    kind: ResolutionKind
    product: Product | None = None
    candidates: list[Product] = field(default_factory=list)
    query: str = ""

    @property
    def ok(self) -> bool:
        return self.kind is ResolutionKind.RESOLVED

    def clarify(self) -> str:
        """A question that lets a buyer pick, without dumping ids on them."""
        if self.kind is not ResolutionKind.AMBIGUOUS:
            return ""
        options = "; ".join(
            f"{c.name} {c.disambiguator}" if c.disambiguator else c.name
            for c in self.candidates
        )
        return f"There is more than one match for “{self.query}”. Which one did you mean — {options}?"


_WORD_RE = re.compile(r"[a-z0-9]+")

# Words that carry no product identity. Kept deliberately short: a stopword
# list that grows becomes a list of "words we decided not to match", which is
# how a resolver quietly stops finding things.
_STOPWORDS = frozenset({
    "the", "and", "for", "you", "your", "yours", "what", "which", "who", "how",
    "much", "many", "are", "is", "was", "of", "in", "on", "at", "to", "do",
    "does", "did", "my", "me", "we", "our", "can", "could", "i", "a", "an",
    "it", "its", "this", "that", "these", "those", "with", "from", "give",
    "tell", "about", "please", "want", "need", "have", "has", "any", "some",
    "there", "here", "get", "know", "like", "would", "should", "will", "be",
    "price", "cost", "rate", "quote", "quotation", "moq", "delivery", "time",
    "buy", "sell", "sell", "order", "quantity", "minimum", "available", "your",
})


def _contains_phrase(haystack: str, needle: str) -> bool:
    """Whole-word containment, both strings already case-folded.

    Plain ``in`` would match "gel" inside "gelatin" and "APO 20" inside
    "APO 200", which is how a resolver confidently identifies the wrong product.
    Both boundaries must be non-alphanumeric.
    """
    start = 0
    while True:
        found = haystack.find(needle, start)
        if found < 0:
            return False
        before = haystack[found - 1] if found else " "
        end = found + len(needle)
        after = haystack[end] if end < len(haystack) else " "
        if not before.isalnum() and not after.isalnum():
            return True
        start = found + 1


# A part code: two or more letters, an optional separator, two or more digits.
# "APO 200", "RT-150", "APO100", "CR-30". Deliberately not a general
# tokeniser — a word like "X-Ray" or "CT" must not become a code, or every
# product mentioning "CT" collides with every other.
_PART_CODE_RE = re.compile(r"\b([A-Za-z]{2,})[\s-]?(\d{2,})\b")


def _part_codes(text: str) -> set[str]:
    """Normalised part codes mentioned in ``text``, e.g. ``{"APO200"}``."""
    return {
        f"{letters}{digits}".upper()
        for letters, digits in _PART_CODE_RE.findall(text or "")
        if len(letters) + len(digits) >= 5
    }


class ProductResolver:
    """Match a reference to exactly one catalogue product, or say why not.

    The catalogue contains **two different products both named "Medical Dry
    Imager"** at ₹5,50,000 and ₹2,00,000. Returning either one silently would
    mean quoting a price for the wrong machine — so an ambiguous match returns
    AMBIGUOUS and a clarifying question, never a best guess.
    """

    def __init__(self, corpus: Corpus) -> None:
        self.corpus = corpus
        self._products = corpus.all_products()
        self._by_id = {p.product_id: p for p in self._products}
        self._by_name: dict[str, list[Product]] = {}
        for product in self._products:
            self._by_name.setdefault(product.name.strip().casefold(), []).append(product)

    def by_id(self, product_id: str) -> Resolution:
        product = self._by_id.get(str(product_id))
        if product:
            return Resolution(ResolutionKind.RESOLVED, product=product, query=product_id)
        return Resolution(ResolutionKind.NOT_FOUND, query=product_id)

    def resolve(self, query: str) -> Resolution:
        """Resolve an id, an exact name, or a distinctive name fragment."""
        text = (query or "").strip()
        if not text:
            return Resolution(ResolutionKind.NOT_FOUND, query=text)

        # 1. Exact id — unambiguous by construction.
        if text in self._by_id:
            return Resolution(ResolutionKind.RESOLVED, product=self._by_id[text], query=text)

        # 2. A bare number that looks like a catalogue id.
        if text.isdigit():
            hit = self._by_id.get(text)
            if hit:
                return Resolution(ResolutionKind.RESOLVED, product=hit, query=text)

        folded = text.casefold()

        # 3. Exact name.
        exact = self._by_name.get(folded)
        if exact:
            if len(exact) == 1:
                return Resolution(ResolutionKind.RESOLVED, product=exact[0], query=text)
            # >1 product shares this name — refuse to choose.
            return Resolution(ResolutionKind.AMBIGUOUS, candidates=list(exact), query=text)

        # Ordered before the word rule below: a part code is the most
        # specific signal a buyer can give, and "injector" appears in three
        # product names — the word rule would report that as ambiguous and
        # never reach the code that settles it.
        # 4. A part code. Buyers ask for "the APO 200 injector", not for
        #    "Apollo RT CT Dual Head Pressure Injector APO 200", and the code is
        #    not in the model field — it is only inside the name. Codes are
        #    compared with spaces, hyphens and case removed, because the same
        #    part is written "APO 200", "APO-200" and "apo200" depending on who
        #    is typing and where they read it.
        wanted = _part_codes(text)
        if wanted:
            code_hits: list[Product] = []
            for product in self._products:
                if _part_codes(product.name) & wanted and product not in code_hits:
                    code_hits.append(product)
            if len(code_hits) == 1:
                return Resolution(ResolutionKind.RESOLVED, product=code_hits[0], query=text)
            if len(code_hits) > 1:
                # Two different machines share a code — say so rather than pick.
                return Resolution(ResolutionKind.AMBIGUOUS, candidates=code_hits, query=text)

        # 5. A catalogue id appearing inside a longer question. Checked before
        #     the name, because an id is the one reference that settles the
        #     ambiguity names cannot: "the price of the Medical Dry Imager
        #     270683" names a product whose name is shared, and the buyer has
        #     already told us which one they mean. Only ids that exist count,
        #     and only from five digits — a shorter run is more likely to be a
        #     quantity or a price than a catalogue id.
        mentioned = [
            token for token in re.findall(r"\b\d{5,}\b", text) if token in self._by_id
        ]
        if len(set(mentioned)) == 1:
            product = self._by_id[mentioned[0]]
            return Resolution(ResolutionKind.RESOLVED, product=product, query=text)
        # The distinctive words of the question, used by the last-resort rule
        # at the end of this method — after the full-name rules, because a
        # single shared word is the weakest signal available.
        #
        # Whole words, not substrings: `in` matched "hi" against the inside of
        # "High Pressure Syringe & Tubing", recording a buyer who said hello as
        # interested in a syringe. A length floor is the obvious repair and the
        # wrong one — it also rejects "gel".
        words = {
            word for word in _WORD_RE.findall(folded)
            if len(word) >= 3 and word not in _STOPWORDS
        }

        # 6. A product name appearing *inside* a longer question — the reverse
        #    direction from steps 3 and 4, and the case that actually occurs:
        #    buyers write "what is the price of the ultrasound gel?", not
        #    "Ultrasound Gel". Without this step no natural-language question
        #    ever resolves, so the deterministic engine is unreachable from
        #    chat no matter how it is wired.
        #
        #    Longest name wins, because "Medical Dry Imager" contains "Dry
        #    Imager" and the more specific name is the one being asked about.
        #    Where the longest match is still shared by several products, the
        #    answer is AMBIGUOUS — which is the whole point of this class.
        best_length = 0
        hits: list[Product] = []
        for product in self._products:
            for name in (product.name, product.model):
                if not name:
                    continue
                needle = str(name).strip().casefold()
                if len(needle) < 4:  # too short to be distinctive
                    continue
                if not _contains_phrase(folded, needle):
                    continue
                if len(needle) > best_length:
                    best_length, hits = len(needle), [product]
                elif len(needle) == best_length and product not in hits:
                    hits.append(product)

        if len(hits) == 1:
            return Resolution(ResolutionKind.RESOLVED, product=hits[0], query=text)
        if len(hits) > 1:
            return Resolution(ResolutionKind.AMBIGUOUS, candidates=hits, query=text)

        # 8. Last resort: a single distinctive word from the question. Reached
        #    only when no full product name appears, so "gel" finds the
        #    Ultrasound Gel but "medical dry imager" is settled by the whole
        #    name above rather than by the shared word "medical" — which alone
        #    matched five products.
        if words:
            matches = []
            for product in self._products:
                name_words = set(_WORD_RE.findall(product.name.casefold()))
                if product.model:
                    name_words |= set(_WORD_RE.findall(str(product.model).casefold()))
                if words & name_words and product not in matches:
                    matches.append(product)
            if len(matches) == 1:
                return Resolution(ResolutionKind.RESOLVED, product=matches[0], query=text)
            if len(matches) > 1:
                return Resolution(ResolutionKind.AMBIGUOUS, candidates=matches, query=text)

        return Resolution(ResolutionKind.NOT_FOUND, query=text)

    def resolve_pair(self, query: str) -> tuple[Product, Product] | None:
        """The two products a comparison question names, if it names two.

        The single-product rules cannot answer this: a question naming two
        products reports AMBIGUOUS, because from the resolver's point of view
        two matches is exactly the situation it exists to refuse. Here it is
        not ambiguity — it is a comparison, and the buyer told us both sides.

        Matches whole product names anywhere in the question, longest first, so
        "Medical Dry Imager" is not shadowed by "Dry Imager".
        """
        text = (query or "").casefold()

        # A part code counts as naming a product. "the difference between the
        # APO 200 and the APO 100" names two products by code and neither by
        # name, so a names-only pair matcher finds nothing and the question
        # falls back to the single-product path — which sees two matches and
        # asks the buyer to choose between the two things they just asked us to
        # compare.
        codes = _part_codes(query or "")
        coded: list[Product] = []
        if codes:
            for product in self._products:
                if _part_codes(product.name) & codes and product not in coded:
                    coded.append(product)
        if len(coded) == 2:
            return (coded[0], coded[1])

        found: list[tuple[int, Product]] = []
        for product in self._products:
            needle = product.name.strip().casefold()
            if len(needle) < 4 or not _contains_phrase(text, needle):
                continue
            found.append((len(needle), product))

        # Longest name wins where names nest; a product is counted once.
        found.sort(key=lambda pair: -pair[0])
        picked: list[Product] = []
        consumed: list[tuple[int, int]] = []
        for length, product in found:
            start = text.find(product.name.strip().casefold())
            if any(lo <= start < hi for lo, hi in consumed):
                continue
            consumed.append((start, start + length))
            if product not in picked:
                picked.append(product)
            if len(picked) == 2:
                return (picked[0], picked[1])
        return None

    def all_names(self) -> list[str]:
        return sorted(p.name for p in self._products)


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------
class FactKind(str, Enum):
    PRICE = "price"
    MOQ = "moq"
    DELIVERY = "delivery"
    SUPPLY_ABILITY = "supply_ability"
    SPECIFICATION = "specification"
    SPEC_LIST = "spec_list"
    SAMPLE = "sample"
    MARKET = "market"
    CATEGORY = "category"


@dataclass(frozen=True)
class Fact:
    """Declarative description of one answerable fact.

    ``needs_caveat`` is part of the declaration rather than something the caller
    remembers: a new commercial fact added without it would be a review-visible
    omission in this table, not a silently uncaveated price.
    """

    kind: FactKind
    label: str
    needs_caveat: bool = False
    is_commercial: bool = False


FACT_REGISTRY: dict[FactKind, Fact] = {
    FactKind.PRICE: Fact(FactKind.PRICE, "price", needs_caveat=True, is_commercial=True),
    FactKind.MOQ: Fact(FactKind.MOQ, "minimum order quantity", is_commercial=True),
    FactKind.DELIVERY: Fact(FactKind.DELIVERY, "delivery time", is_commercial=True),
    FactKind.SUPPLY_ABILITY: Fact(FactKind.SUPPLY_ABILITY, "supply ability", is_commercial=True),
    FactKind.SAMPLE: Fact(FactKind.SAMPLE, "sample availability", is_commercial=True),
    FactKind.MARKET: Fact(FactKind.MARKET, "main domestic market"),
    FactKind.SPECIFICATION: Fact(FactKind.SPECIFICATION, "specification"),
    FactKind.SPEC_LIST: Fact(FactKind.SPEC_LIST, "specifications"),
    FactKind.CATEGORY: Fact(FactKind.CATEGORY, "category"),
}


# Which fact a question is asking for. Ordered, because a question can mention
# two ("what's the price and the minimum order?") and the first match is the one
# being asked about — price is the commercial fact buyers lead with.
#
# This exists because the deterministic engine was unreachable without it: the
# intents were classified, the facts were registered, and nothing joined them,
# so "what is the price of the ultrasound gel?" was answered with whichever
# chunk happened to rank first — once, a FAQ about diagnostic suitability.
# A verbatim chunk is not a hallucination, but answering a price question with
# something else is still the wrong answer.
_FACT_PATTERNS: tuple[tuple[FactKind, re.Pattern[str]], ...] = (
    (FactKind.PRICE, re.compile(
        r"\b(price|prices|pricing|cost|costs|rate|how\s+much|"
        r"what.{0,12}\bcharge|quotation|quote|₹|rs\.?|inr)\b", re.I)),
    (FactKind.MOQ, re.compile(
        r"\b(moq|minimum\s+order|min\.?\s*order|minimum\s+quantity|"
        r"min\.?\s*quantity|order\s+quantity|how\s+many.{0,20}\border)\b", re.I)),
    (FactKind.DELIVERY, re.compile(
        r"\b(delivery|deliver|lead\s+time|dispatch|shipping|ship\s+time|"
        r"how\s+long.{0,20}\b(deliver|ship|take|arrive))\b", re.I)),
    (FactKind.SUPPLY_ABILITY, re.compile(
        r"\b(supply\s+ability|supply\s+capacity|monthly\s+capacity|"
        r"how\s+many.{0,20}\bsupply)\b", re.I)),
    (FactKind.SAMPLE, re.compile(r"\b(sample|samples|trial\s+unit)\b", re.I)),
    (FactKind.MARKET, re.compile(
        r"\b(which\s+market|markets\b|domestic\s+market|where.{0,16}\bsell)\b", re.I)),
    (FactKind.SPEC_LIST, re.compile(
        r"\b(specifications?|specs?|technical\s+details?|full\s+details?)\b", re.I)),
)


# Which company fact a question asks for. The kinds are the ones
# ``company_fact`` can answer from the corpus; anything else returns None and
# goes to retrieval, which may still find it in the company profile prose.
_COMPANY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("address", re.compile(
        r"\b(where are you|where is your|your (office|address|location|branch|"
        r"showroom|factory|warehouse)|address|located|head ?office|"
        r"branch(es)?|visit)\b", re.I)),
    ("contact", re.compile(
        r"\b(phone|telephone|contact (you|us|number|details)|call you|reach you|"
        r"mobile|email|whatsapp)\b", re.I)),
    ("gst", re.compile(r"\b(gst|gstin)\b", re.I)),
    ("pan", re.compile(r"\b(pan|permanent account)\b", re.I)),
    ("iec", re.compile(r"\b(iec|import export code)\b", re.I)),
    ("certifications", re.compile(
        r"\b(iso|nsic|crisil|certif\w*|licen[cs]e|accredit\w*|quality (standard|rating))\b",
        re.I)),
    ("directors", re.compile(
        r"\b(director|ceo|chairman|managing director|who (runs|owns|founded)|"
        r"promoter|proprietor|leadership|management)\b", re.I)),
)


def company_kind_for(question: str) -> str | None:
    """The company fact this question asks for, or ``None``."""
    for kind, pattern in _COMPANY_PATTERNS:
        if pattern.search(question or ""):
            return kind
    return None


def fact_kind_for(question: str) -> FactKind | None:
    """The commercial fact this question asks for, or ``None``.

    ``None`` is a real answer and it is the safe one: it sends the question down
    the retrieval path instead. Guessing a fact would produce a confident answer
    to a question nobody asked.
    """
    text = question or ""
    for kind, pattern in _FACT_PATTERNS:
        if pattern.search(text):
            return kind
    return None


@dataclass(frozen=True)
class Answer:
    """A grounded answer. ``grounded_on`` names the corpus field it came from."""

    text: str
    spoken: str
    grounded_on: str
    source_url: str | None = None
    product_id: str | None = None
    caveat: str | None = None
    found: bool = True

    def render(self, *, speak: bool = False) -> str:
        body = self.spoken if speak else self.text
        return f"{body} {self.caveat}" if self.caveat else body

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "spoken": self.spoken,
            "grounded_on": self.grounded_on,
            "source_url": self.source_url,
            "product_id": self.product_id,
            "caveat": self.caveat,
            "found": self.found,
        }


class AnitaAnswers:
    """The deterministic half of the answer engine."""

    def __init__(self, corpus: Corpus) -> None:
        self.corpus = corpus
        self.resolver = ProductResolver(corpus)

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _cited(product: Product, field_name: str) -> str:
        return f"products[{product.product_id}].{field_name}"

    def _not_published(self, product: Product, what: str) -> Answer:
        """Absence is a fact, not a failure — and it never gets guessed (DG-13).

        ``what`` is a noun phrase that reads after "We don't have … for", e.g.
        "a published price". Phrasing it that way avoids the doubled wording
        ("does not have a published specification list published") that arises
        when the caller's noun already contains "published".
        """
        return Answer(
            text=(
                f"We don't have {what} for {product.name} in our catalogue. "
                f"Our team can confirm it for you."
            ),
            spoken=f"That detail isn't published for {product.name}. Our team can confirm it.",
            grounded_on=f"products[{product.product_id}] (absent)",
            source_url=product.url,
            product_id=product.product_id,
            found=False,
        )

    # -- commercial facts --------------------------------------------------
    def commercial(self, kind: FactKind, product: Product) -> Answer:
        """Answer a commercial fact from SQL, with the caveat where required.

        The caveat is applied here — in the same function that produces the
        number — so there is no path that yields a price without it.
        """
        fact = FACT_REGISTRY[kind]
        caveat = PRICE_CAVEAT if fact.needs_caveat else None

        if kind is FactKind.PRICE:
            if product.price_amount is None:
                return self._not_published(product, "a price")
            display = product.price_display or f"{product.price_amount} {product.price_currency or 'INR'}"
            spoken = product.price_spoken or f"{product.price_amount} {product.price_currency or 'INR'}"
            # `price_display` already carries "/Piece" when a unit is known, so
            # appending it again produced "₹60/Piece per Piece". Only add the
            # unit when the rendered form does not already express it.
            if product.price_unit and f"/{product.price_unit}" not in display:
                display = f"{display} per {product.price_unit}"
            return Answer(
                text=f"{product.name} is listed at {display}.",
                spoken=f"{product.name} is listed at {spoken}.",
                grounded_on=self._cited(product, "price_amount"),
                source_url=product.url,
                product_id=product.product_id,
                caveat=caveat,
            )

        if kind is FactKind.MOQ:
            if product.moq_value is None:
                return self._not_published(product, "a minimum order quantity")
            unit = product.moq_unit or ""
            quantity = f"{product.moq_value:g} {unit}".strip()
            return Answer(
                text=f"The minimum order quantity for {product.name} is {quantity}.",
                spoken=f"The minimum order for {product.name} is {quantity}.",
                grounded_on=self._cited(product, "moq_value"),
                source_url=product.url,
                product_id=product.product_id,
                caveat=caveat,
            )

        if kind is FactKind.DELIVERY:
            if not product.delivery_time:
                return self._not_published(product, "a delivery time")
            spoken = (
                f"about {product.delivery_days} days"
                if product.delivery_days
                else product.delivery_time
            )
            return Answer(
                text=f"Delivery time for {product.name} is {product.delivery_time}.",
                spoken=f"Delivery for {product.name} takes {spoken}.",
                grounded_on=self._cited(product, "delivery_time"),
                source_url=product.url,
                product_id=product.product_id,
                caveat=caveat,
            )

        if kind is FactKind.SUPPLY_ABILITY:
            if not product.supply_ability:
                return self._not_published(product, "a supply ability")
            return Answer(
                text=f"Supply ability for {product.name} is {product.supply_ability}.",
                spoken=f"We can supply {product.supply_ability} for {product.name}.",
                grounded_on=self._cited(product, "supply_ability"),
                source_url=product.url,
                product_id=product.product_id,
                caveat=caveat,
            )

        if kind is FactKind.SAMPLE:
            if not product.sample_available:
                return self._not_published(product, "sample availability")
            if product.sample_available.strip().casefold() in {"yes", "true", "1"}:
                extra = f" {product.sample_policy}" if product.sample_policy else ""
                return Answer(
                    text=f"Samples of {product.name} are available.{extra}",
                    spoken=f"Samples of {product.name} are available.",
                    grounded_on=self._cited(product, "sample_available"),
                    source_url=product.url,
                    product_id=product.product_id,
                    caveat=caveat,
                )
            return Answer(
                text=f"Samples of {product.name} are not available.",
                spoken=f"Samples aren't available for {product.name}.",
                grounded_on=self._cited(product, "sample_available"),
                source_url=product.url,
                product_id=product.product_id,
                caveat=caveat,
            )

        if kind is FactKind.MARKET:
            if not product.main_domestic_market:
                return self._not_published(product, "a domestic market")
            return Answer(
                text=f"{product.name} is supplied to {product.main_domestic_market}.",
                spoken=f"We supply {product.name} across {product.main_domestic_market}.",
                grounded_on=self._cited(product, "main_domestic_market"),
                source_url=product.url,
                product_id=product.product_id,
            )

        if kind is FactKind.CATEGORY:
            if not product.category:
                return self._not_published(product, "a category")
            return Answer(
                text=f"{product.name} belongs to the {product.category} category.",
                spoken=f"{product.name} is in the {product.category} category.",
                grounded_on=self._cited(product, "category"),
                source_url=product.url,
                product_id=product.product_id,
            )

        raise ValueError(f"{kind} is not a commercial fact")

    # -- specifications ----------------------------------------------------
    def specification(self, product: Product, spec_name: str) -> Answer:
        """One named specification, distinguishing 'not published' from 'unknown'."""
        view = self.corpus.product_view(product.product_id)
        if view is None:
            return self._not_published(product, "specifications")

        value = view.spec(spec_name)
        if value:
            return Answer(
                text=f"{product.name} — {spec_name}: {value}.",
                spoken=f"{spec_name} is {value}.",
                grounded_on=f"product_specs[{product.product_id}].{spec_name}",
                source_url=product.url,
                product_id=product.product_id,
            )

        if not view.specs:
            # The source site publishes no specs for this product — say so
            # rather than implying the value is unknown (DG-13).
            return self._not_published(product, "a published specification list")
        return self._not_published(product, f"a published “{spec_name}” specification")

    def spec_list(self, product: Product) -> Answer:
        view = self.corpus.product_view(product.product_id)
        if view is None or not view.specs:
            return self._not_published(product, "a published specification list")
        pairs = "; ".join(f"{s.name}: {s.value}" for s in view.specs[:8])
        return Answer(
            text=f"{product.name} — {pairs}.",
            spoken=f"{product.name}: {pairs}.",
            grounded_on=f"product_specs[{product.product_id}]",
            source_url=product.url,
            product_id=product.product_id,
        )

    # -- comparison (UC-06) ------------------------------------------------
    def compare(self, left: Product, right: Product) -> Answer:
        """Compare on the specifications **both** products actually publish.

        A comparison that fills a gap from the other product's spec sheet would
        be a fabrication, so rows present on only one side are marked "not
        published" rather than omitted — the buyer needs to see that the data is
        missing, not have it quietly dropped.
        """
        left_view = self.corpus.product_view(left.product_id)
        right_view = self.corpus.product_view(right.product_id)
        left_specs = {s.name.casefold(): s for s in (left_view.specs if left_view else [])}
        right_specs = {s.name.casefold(): s for s in (right_view.specs if right_view else [])}

        shared = sorted(set(left_specs) & set(right_specs))
        if not shared:
            return Answer(
                text=(
                    f"{left.name} and {right.name} have no published specifications "
                    f"in common, so I can't compare them from the catalogue. "
                    f"Our team can help with a detailed comparison."
                ),
                spoken=(
                    f"I don't have comparable published specifications for "
                    f"{left.name} and {right.name}. Our team can help."
                ),
                grounded_on="product_specs (no intersection)",
                source_url=left.url,
                found=False,
            )

        rows = []
        for key in shared:
            a, b = left_specs[key], right_specs[key]
            marker = "" if a.value == b.value else "  ← differs"
            rows.append(f"{a.name}: {left.name} — {a.value} | {right.name} — {b.value}{marker}")

        return Answer(
            text=(
                f"Comparing {left.name} and {right.name} on the "
                f"{len(shared)} published specification(s) they share:\n"
                + "\n".join(rows)
            ),
            spoken=f"They share {len(shared)} published specifications. {rows[0]}",
            grounded_on="product_specs intersection",
            source_url=left.url,
        )

    # -- company facts -----------------------------------------------------
    _GENERIC_LABELS = {"address", "branch offices", "branch office"}
    _LABEL_ORDER = ("mumbai", "delhi", "navi_mumbai")

    def _clean_branches(self) -> list[dict[str, str]]:
        """Normalise the branch rows into speakable address lines.

        The corpus stores branches from two sources: the statutory table
        ("Address", "Branch Offices") and the scraper's city keys
        ("mumbai", "delhi"). They describe the same offices, so a naive
        concatenation repeats Mumbai twice under two labels, embeds newlines,
        and prefixes every line with a redundant label — which is exactly what
        the first run of this method produced.

        Deduplication is by **pincode**, because the two sources word the same
        address differently ("Pandurangwadi, 4th Cross Road" vs "4th Lane,
        Pandurang Wadi") while agreeing on the one genuinely identifying token.
        """
        import re

        # Deduplicate by key while *preferring* the better row, rather than
        # first-come-first-served.
        #
        # The first version deduped before choosing, so the statutory row
        # labelled "Address" claimed Mumbai's pincode and then blocked the
        # city-labelled "mumbai" row that described the same office better —
        # leaving only the warehouse. Selection must happen inside the dedupe.
        best: dict[str, dict[str, str]] = {}

        for row in self.corpus.branches():
            raw = re.sub(r"\s+", " ", (row.get("address") or "")).strip()
            raw = re.sub(r"^Additional Address\s*:\s*", "", raw, flags=re.IGNORECASE)
            if not raw:
                continue

            label = (row.get("label") or "").strip()
            is_generic = label.casefold() in self._GENERIC_LABELS

            # Pincodes are written two ways across the two sources — "400063"
            # in the city rows and "400 063" in the statutory table. Normalising
            # the space is what makes the two descriptions of the same office
            # collapse into one.
            pincode = re.search(r"\b(\d{3})\s?(\d{3})\b", raw)
            key = pincode.group(1) + pincode.group(2) if pincode else raw[:40].casefold()

            friendlier = label.replace("_", " ").title() if label else "Head"
            entry = {"label": friendlier, "address": raw, "generic": str(is_generic)}

            incumbent = best.get(key)
            if incumbent is None:
                best[key] = entry
                continue
            # A named row beats a generic one; between equals, the fuller
            # address wins because it carries more of the detail a buyer needs.
            incumbent_rank = (incumbent["generic"] == "True", -len(incumbent["address"]))
            challenger_rank = (entry["generic"] == "True", -len(entry["address"]))
            if challenger_rank < incumbent_rank:
                best[key] = entry

        source = list(best.values())
        source.sort(
            key=lambda b: self._LABEL_ORDER.index(b["label"].casefold())
            if b["label"].casefold() in self._LABEL_ORDER
            else len(self._LABEL_ORDER)
        )
        # A warehouse is not an office — say what it is.
        for entry in source:
            if entry["label"].casefold() == "navi mumbai" and "storage" in entry["address"].casefold():
                entry["label"] = "Navi Mumbai storage"
        return source

    def company_fact(self, kind: str) -> Answer:
        """Statutory and contact facts, read from the corpus (BRD-08)."""
        if kind == "contact":
            numbers = self.corpus.company_ids().get("phone", [])
            if not numbers:
                return Answer(
                    text="I don't have our phone number to hand.",
                    spoken="I don't have that number available.",
                    grounded_on="company_ids[phone]",
                    found=False,
                )
            # The first number is the one the site publishes first; the rest are
            # the same number in another format, so repeating them all would
            # read as several lines rather than one.
            primary = numbers[0]
            spoken = " ".join(primary.lstrip("+"))
            return Answer(
                text=f"You can reach us on {primary}.",
                spoken=f"You can reach us on {spoken}.",
                grounded_on="company_ids[phone]",
            )

        if kind in {"gst", "pan", "iec"}:
            values = self.corpus.company_ids().get(kind, [])
            if not values:
                return Answer(
                    text=f"I don't have our {kind.upper()} number to hand.",
                    spoken=f"I don't have that number available.",
                    grounded_on=f"company_ids[{kind}]",
                    found=False,
                )
            # Spelled out for voice: a GSTIN or PAN read as a word is useless.
            spelled = ", ".join(" ".join(v) for v in values)
            label = {"gst": "GST numbers", "pan": "PAN", "iec": "import export code"}[kind]
            plural = len(values) > 1
            return Answer(
                text=f"Our {label} {'are' if plural else 'is'} " + ", ".join(values) + ".",
                spoken=f"Our {label} {'are' if plural else 'is'} {spelled}.",
                grounded_on=f"company_ids[{kind}]",
            )

        if kind == "address":
            branches = self._clean_branches()
            if not branches:
                return Answer(
                    text="I don't have our address details available.",
                    spoken="I don't have that to hand.",
                    grounded_on="branches",
                    found=False,
                )
            # A place is either an office or a facility. Storage is described by
            # its own address ("Air-conditioned storage in Navi Mumbai"), so
            # appending "office at <address>" to it produced nonsense like
            # "our office Navi Mumbai is at air-conditioned storage in Navi Mumbai".
            def is_facility(branch: dict[str, str]) -> bool:
                return "storage" in branch["label"].casefold()

            def office_name(branch: dict[str, str]) -> str:
                return branch["label"].replace(" storage", "").strip()

            primary = branches[0]
            text = f"Our {office_name(primary)} office is at {primary['address']}."

            for branch in branches[1:]:
                if is_facility(branch):
                    text += f" We also have {branch['address'].lower()[0].upper()}{branch['address'][1:]}."
                else:
                    text += f" We also have a {office_name(branch)} office at {branch['address']}."

            spoken = f"Our {office_name(primary)} office is at {primary['address']}."
            return Answer(text=text, spoken=spoken, grounded_on="branches")

        if kind == "directors":
            people = self.corpus.people()
            if not people:
                return Answer(
                    text="I don't have the leadership details available.",
                    spoken="I don't have that to hand.",
                    grounded_on="people",
                    found=False,
                )
            listing = "; ".join(f"{p['name']} ({p['role']})" for p in people)
            return Answer(
                text=f"Our leadership: {listing}.",
                spoken="Our leadership team is listed on our company profile.",
                grounded_on="people",
            )

        if kind == "certifications":
            certs = self.corpus.certifications()
            if not certs:
                return Answer(
                    text="I don't have our certifications listed.",
                    spoken="I don't have that to hand.",
                    grounded_on="certifications",
                    found=False,
                )
            values = "; ".join(c["value"] for c in certs)
            return Answer(
                text=f"Our certifications: {values}.",
                spoken=f"We hold {values}.",
                grounded_on="certifications",
            )

        raise ValueError(f"unknown company fact {kind!r}")

    # -- catalogue ---------------------------------------------------------
    def product_count(self) -> Answer:
        products = self.corpus.all_products()
        return Answer(
            text=f"Our catalogue lists {len(products)} products.",
            spoken=f"We offer {len(products)} products.",
            grounded_on="products",
        )
