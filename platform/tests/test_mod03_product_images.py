"""The product photograph on an answer — where it comes from, and when it must not.

Asserted against the **real corpus**, like the rest of MOD-03: the facts that
matter here are properties of the collected data (one primary image per
product, ids padded in one table and not the other), and a fixture I invented
would prove only that the code works on data I invented.

The second half of this file is the more important half. Showing a *wrong*
picture is worse than showing none — a photograph reads as evidence — so most
of these tests are about the cases that must render nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.answers import AnitaAnswers  # noqa: E402
from anita.corpus import Corpus  # noqa: E402
from anita.answer_engine import AnswerEngine  # noqa: E402
from anita.orchestrator import Intent  # noqa: E402
from anita.surfaces import product_card  # noqa: E402

CORPUS_DIR = PLATFORM.parent / "data" / "out"
pytestmark = pytest.mark.skipif(not CORPUS_DIR.exists(), reason="corpus not collected")


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return Corpus(CORPUS_DIR)


# ---------------------------------------------------------------------------
# The join
# ---------------------------------------------------------------------------
class TestTheProductImageJoin:
    def test_every_catalogue_product_has_exactly_one_primary_image(self, corpus):
        """The property the whole feature rests on."""
        missing, doubled = [], []
        for product in corpus.all_products():
            primaries = [m for m in corpus.media_for(product.product_id) if m.is_primary]
            if not primaries:
                missing.append(product.product_id)
            elif len(primaries) > 1:
                doubled.append(product.product_id)

        assert not missing, f"products with no primary image: {missing}"
        assert not doubled, f"products with more than one: {doubled}"

    def test_the_id_padding_difference_is_handled(self, corpus):
        """`media` stores `00270694`; `products` and every citation say `270694`.

        A plain equality join returns nothing here, and the symptom is "this
        product has no photograph" — a missing picture, not an error. So it is
        asserted rather than assumed.
        """
        product = corpus.get("270694")
        assert product is not None, "the fixture product is gone from the catalogue"
        assert product.product_id == "270694", "products stopped being unpadded"

        image = corpus.primary_image("270694")
        assert image is not None, "the padded/unpadded join returned nothing"

    def test_it_returns_the_primary_and_not_the_related(self, corpus):
        """`related` rows are pictures of *other* products on this product's page."""
        images = corpus.media_for("270694")
        primary = corpus.primary_image("270694")

        assert primary is not None
        assert primary.is_primary
        assert all(m.url != primary.url or m.is_primary for m in images)

    def test_a_product_with_no_image_returns_none_rather_than_a_guess(self, corpus):
        assert corpus.primary_image("999999999") is None

    def test_the_image_carries_the_page_it_was_found_on(self, corpus):
        """Provenance for the picture, which is not always the product's own page."""
        image = corpus.primary_image("270694")
        assert image is not None
        assert image.source_url.startswith("https://")


# ---------------------------------------------------------------------------
# The citation
# ---------------------------------------------------------------------------
class TestTheCitationCarriesTheImage:
    def test_a_product_answer_cites_the_image_and_the_page(self, corpus):
        engine = AnswerEngine(answers=AnitaAnswers(corpus))

        # COMMERCIAL is the price path — the deterministic engine, not retrieval.
        reply = engine.answer(
            Intent.COMMERCIAL,
            "What is the price of the ultrasound gel?",
            _conversation(),
        )

        citations = [c for c in reply.grounded_on if c.get("chunk_type") == "product_record"]
        assert citations, "no product citation to carry an image"
        assert citations[0]["image_url"], "the citation lost its image"
        assert citations[0]["image_url"].startswith("https://")
        assert citations[0]["source_url"].endswith("ultrasound-gel-270694.html")

    def test_an_engine_with_no_answers_still_composes_a_citation(self):
        """A missing corpus must not cost the answer — only the picture."""
        engine = AnswerEngine(answers=None)

        class P:
            product_id = "1"
            name = "Thing"
            url = "https://example.com/p"

        citation = engine._product_citation(P())

        assert citation["product_id"] == "1"
        assert "image_url" not in citation

    def test_a_corpus_that_cannot_be_read_does_not_raise(self, corpus):
        """A picture must never cost an answer that is already composed."""
        engine = AnswerEngine(answers=AnitaAnswers(corpus))

        class Broken:
            def primary_image(self, product_id):
                raise RuntimeError("database is gone")

        engine.answers.corpus = Broken()

        class P:
            product_id = "1"
            name = "Thing"
            url = "https://example.com/p"

        citation = engine._product_citation(P())

        assert citation["product_id"] == "1"
        assert "image_url" not in citation


# ---------------------------------------------------------------------------
# The card, and the cases that must show nothing
# ---------------------------------------------------------------------------
def _record(product_id="270686", name="Microbar Enema", **extra):
    citation = {
        "chunk_id": f"product:{product_id}", "chunk_type": "product_record",
        "product_id": product_id, "product_name": name,
        "source_url": f"https://www.anitamedicalsystems.com/x-{product_id}.html",
        "content_hash": "",
    }
    citation.update(extra)
    return citation


class TestWhichProductAnAnswerIsAbout:
    def test_the_product_record_citation_is_the_one_used(self):
        """Not the first citation, and not the first that mentions a product.

        The live reply to "tell me about the Microbar Enema" cited the Enema,
        the Paste and the Powder. The card is about the Enema.
        """
        body = {"grounded_on": [
            {"chunk_type": "product_identity", "product_id": "270686",
             "product_name": "Microbar Enema", "source_url": "https://x/1"},
            {"chunk_type": "product_about", "product_id": "270685",
             "product_name": "Microbar Paste", "source_url": "https://x/2"},
            _record(image_url="https://img/enema.jpg"),
        ]}

        card = product_card(body)

        assert card["product_id"] == "270686"
        assert card["name"] == "Microbar Enema"
        assert card["image_url"] == "https://img/enema.jpg"

    @pytest.mark.parametrize("citations", [
        [],                                                   # nothing grounded
        [{"chunk_type": "category_listing", "product_id": None}],   # "what do you sell"
        [{"chunk_type": "company_record", "product_id": None}],     # statutory facts
        [{"chunk_type": "product_about", "product_id": "270685",
          "product_name": "Microbar Paste", "source_url": "https://x/2"}],
    ])
    def test_answers_that_are_not_about_a_product_show_no_card(self, citations):
        """A greeting, a decline, a listing and a company fact are all here.

        A picture under any of them would imply the assistant had identified a
        product, which is the one thing it did not do.
        """
        assert product_card({"grounded_on": citations}) is None

    def test_a_product_with_no_image_still_gets_a_card(self):
        """The link is the more valuable half; the picture is the decoration."""
        card = product_card({"grounded_on": [_record()]})

        assert card is not None
        assert card["image_url"] is None
        assert card["source_url"].endswith("x-270686.html")

    def test_a_citation_with_no_source_page_is_not_a_card(self):
        """Provenance without a source is not provenance."""
        broken = _record()
        broken["source_url"] = None
        assert product_card({"grounded_on": [broken]}) is None

    def test_a_malformed_payload_does_not_raise(self):
        assert product_card({}) is None
        assert product_card({"grounded_on": None}) is None
        assert product_card({"grounded_on": ["not-a-dict"]}) is None


def _conversation():
    """A conversation stand-in — the engine only needs it to exist."""
    import types

    return types.SimpleNamespace(
        conversation_id="c1", key="s1", turns=[], product_id=None,
        state=None, summary=None,
    )
