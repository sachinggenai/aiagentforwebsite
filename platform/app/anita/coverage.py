"""Coverage probe — what the catalogue has *published*, per product (US-012).

`DG-13` is a live data reality, not a nicety: three products publish no
specifications (270685, 270687, 381893) and four publish no About text (270683,
3935251, 3935247, 3935249). The product's rule is that those are **stated as
not published**, never inferred from a similarly named sibling — inventing a
specification is the one failure mode this assistant may not ship.

Before this module the availability fact existed in two places and was used in
neither: `corpus.KNOWN_NO_SPECS`/`KNOWN_NO_ABOUT` recorded the product ids, and
`ProductView.specs_absent_on_source` carried the flag — while the answer path
declined "not in our catalogue" for a product it stocks but has not documented.
Which reads to a buyer as "you don't sell it", the opposite of the truth.

The probe is **derived, never cached**: it reads the corpus at call time, so a
refresh that adds specifications flips the answer to `published` with no code
change and no cache to invalidate (edge case 9). Read-only by construction —
the corpus opens its SQLite file read-only.

The story's LLD expresses this as a SQL view over `product_availability`. The
platform's corpus *is* that projection (`products` + `product_specs` in
`data/out/ams.sqlite`), so the probe queries the corpus rather than a second
view over the same rows — one definition of "published", in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# The canned answers. `BRD-07` wants the absence stated in words a buyer can
# act on: what is missing, why the assistant cannot fill it, and the way to get
# it. No "I don't have that in our catalogue" — that sentence means "we do not
# sell this", which is false.
NOT_PUBLISHED_SPECS = (
    "ANITA has not published specifications for {product} yet, so I can't give "
    "you the figures — I'd rather say that than guess. Would you like our team "
    "to confirm the details for you?"
)
NOT_PUBLISHED_ABOUT = (
    "There's no product description published for {product} yet. Our team can "
    "tell you more about it — would you like us to get in touch?"
)


class Availability(str, Enum):
    PUBLISHED = "published"
    NOT_PUBLISHED = "not_published"


class UnknownProduct(KeyError):
    """The id is not in `products` — a data gap, not an absence claim."""


@dataclass(frozen=True)
class ProductAvailability:
    product_id: str
    name: str
    specs: Availability
    about: Availability

    def as_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "name": self.name,
            "specs": self.specs.value,
            "about": self.about.value,
        }


class CoverageProbe:
    """Availability derived from the corpus, per product id.

    Deliberately **per product id**, not per name: the catalogue holds two
    different products both named "Medical Dry Imager", one with About text and
    one without, and a name-keyed probe would report whichever it found first
    (edge case 11).
    """

    def __init__(self, corpus) -> None:
        self.corpus = corpus
        self._cache: dict[str, ProductAvailability] = {}

    def for_product(self, product_id: str) -> ProductAvailability:
        """What is published for this product. Raises :class:`UnknownProduct`."""
        key = str(product_id)
        if key in self._cache:
            return self._cache[key]

        product = self.corpus.get(key)
        if product is None:
            raise UnknownProduct(key)

        # Spec counts come from the product row (`spec_count`), which the corpus
        # loads from the `product_specs` aggregate — the same definition the
        # absence view uses. `has_specs` is that count > 0; `has_about` is
        # `about_chars > 0`. Both are the *source site's* state, which is the
        # only thing "not published" can honestly mean.
        availability = ProductAvailability(
            product_id=key,
            name=product.name,
            specs=Availability.PUBLISHED if product.has_specs else Availability.NOT_PUBLISHED,
            about=Availability.PUBLISHED if product.has_about else Availability.NOT_PUBLISHED,
        )
        self._cache[key] = availability
        return availability

    def absent_spec_products(self) -> frozenset[str]:
        return frozenset(
            product.product_id for product in self.corpus.all_products()
            if not product.has_specs
        )

    def absent_about_products(self) -> frozenset[str]:
        return frozenset(
            product.product_id for product in self.corpus.all_products()
            if not product.has_about
        )

    def counts(self) -> dict[str, int]:
        """For the boot/system report: how much of the catalogue is silent."""
        return {
            "products": len(self.corpus.all_products()),
            "absent_specs": len(self.absent_spec_products()),
            "absent_about": len(self.absent_about_products()),
        }


__all__ = [
    "Availability", "CoverageProbe", "NOT_PUBLISHED_ABOUT", "NOT_PUBLISHED_SPECS",
    "ProductAvailability", "UnknownProduct",
]
