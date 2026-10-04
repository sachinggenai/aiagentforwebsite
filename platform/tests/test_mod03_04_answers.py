"""MOD-03/04 tests — corpus access and the deterministic answer path.

These run against the **real collected corpus** (`../data/out/`), not fixtures.
That is deliberate: a fixture would prove the code works on data I invented,
whereas the corpus proves it works on the catalogue the product actually ships
— including its two same-named products and its four products with no About
text on the source site.

Run:  .venv\\Scripts\\python -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.answers import (  # noqa: E402
    FACT_REGISTRY,
    AnitaAnswers,
    FactKind,
    ProductResolver,
    ResolutionKind,
)
from anita.corpus import (  # noqa: E402
    KNOWN_NO_ABOUT,
    KNOWN_NO_SPECS,
    PRICE_CAVEAT,
    Corpus,
)

CORPUS_DIR = PLATFORM.parent / "data" / "out"
pytestmark = pytest.mark.skipif(
    not (CORPUS_DIR / "ams.sqlite").exists(),
    reason="corpus not built — run the scraper in src/ams first",
)


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return Corpus(CORPUS_DIR)


@pytest.fixture(scope="module")
def answers(corpus: Corpus) -> AnitaAnswers:
    return AnitaAnswers(corpus)


# ---------------------------------------------------------------------------
# Corpus access
# ---------------------------------------------------------------------------
class TestCorpus:
    def test_loads_the_real_catalogue(self, corpus):
        counts = corpus.counts()
        assert counts["products"] == 24
        assert counts["specs"] > 300
        assert counts["faqs"] == 36

    def test_caveat_guard_passes(self, corpus):
        """Every price must be flagged list-price + quote-required (TRD-19).

        This is the boot guard, not a report: if it ever fails, the platform
        must refuse to serve prices rather than quote one as final.
        """
        assert corpus.caveat_violations() == []

    def test_known_absent_products_are_still_absent(self, corpus):
        """A scraper change that silently drops specs must be caught.

        US-012 asserts these exact sets, so the day the source site gains the
        data — or the scraper loses it — this fails loudly rather than the
        product quietly answering "not published" for the wrong reason.
        """
        with_specs = {p.product_id for p in corpus.all_products() if p.has_specs}
        for pid in KNOWN_NO_SPECS:
            assert pid not in with_specs, f"{pid} unexpectedly has specs now"

        for pid in KNOWN_NO_ABOUT:
            product = corpus.get(pid)
            assert product is not None
            assert not product.has_about, f"{pid} unexpectedly has About text now"

    def test_read_only_connection_cannot_write(self, corpus):
        import sqlite3

        with pytest.raises(sqlite3.OperationalError):
            with corpus._connect() as connection:  # noqa: SLF001
                connection.execute("DELETE FROM products")

    def test_chunks_are_iterable(self, corpus):
        chunks = list(corpus.iter_chunks())
        assert len(chunks) > 100
        assert {"chunk_id", "text", "context_prefix"} <= set(chunks[0])


# ---------------------------------------------------------------------------
# Product resolution (BRD-09)
# ---------------------------------------------------------------------------
class TestProductResolver:
    def test_resolves_by_id(self, corpus):
        result = ProductResolver(corpus).resolve("270694")
        assert result.kind is ResolutionKind.RESOLVED
        assert result.product.name == "Ultrasound Gel"

    def test_resolves_by_exact_name(self, corpus):
        result = ProductResolver(corpus).resolve("X-Ray Cassette")
        assert result.kind is ResolutionKind.RESOLVED
        assert result.product.product_id == "270691"

    def test_duplicate_name_is_ambiguous_not_a_guess(self, corpus):
        """The catalogue has TWO products named 'Medical Dry Imager' at
        different prices. Returning either one silently would quote a price for
        the wrong machine — the worst available failure."""
        result = ProductResolver(corpus).resolve("Medical Dry Imager")
        assert result.kind is ResolutionKind.AMBIGUOUS
        assert len(result.candidates) == 2
        prices = {c.price_amount for c in result.candidates}
        assert len(prices) == 2, "the two products should have different prices"

    def test_ambiguous_resolution_asks_a_usable_question(self, corpus):
        result = ProductResolver(corpus).resolve("Medical Dry Imager")
        question = result.clarify()
        assert question
        # Both options must be distinguishable in the question itself.
        for candidate in result.candidates:
            assert candidate.product_id in question

    def test_unknown_product_is_not_found(self, corpus):
        result = ProductResolver(corpus).resolve("Titanium Spleen Polisher")
        assert result.kind is ResolutionKind.NOT_FOUND

    def test_empty_query_is_not_found(self, corpus):
        assert ProductResolver(corpus).resolve("").kind is ResolutionKind.NOT_FOUND

    def test_partial_name_resolves_when_unique(self, corpus):
        result = ProductResolver(corpus).resolve("Ultrasound")
        assert result.kind is ResolutionKind.RESOLVED
        assert "Ultrasound" in result.product.name

    def test_model_number_resolution_is_a_known_corpus_gap(self, corpus):
        """Model numbers are NOT resolvable today, and this records why.

        The category pages carry model tags ('DRYPIX 2000', '04') on each product
        card and the scraper parses them — but it does not propagate them into
        `products.model`, which is NULL for all 24 rows. So this resolver cannot
        match on model, and the test asserts the current reality rather than a
        capability the product does not have.

        Impact today: none. Disambiguation falls back to product id + price,
        which is sufficient for BRD-09. Closing the gap is a scraper change
        (write the parsed model into `products.model`) and would make
        disambiguation friendlier — it is recorded here so it is a known gap
        rather than a silent absence.
        """
        assert all(p.model is None for p in corpus.all_products()), (
            "models are now populated — the corpus gap is closed and this test "
            "should be replaced with a real resolution assertion"
        )
        assert ProductResolver(corpus).resolve("DRYPIX 2000").kind is ResolutionKind.NOT_FOUND


# ---------------------------------------------------------------------------
# Commercial answers — the caveat contract
# ---------------------------------------------------------------------------
class TestCommercialAnswers:
    def test_price_carries_the_caveat(self, answers, corpus):
        product = corpus.get("270694")  # Ultrasound Gel, ₹60
        answer = answers.commercial(FactKind.PRICE, product)
        assert answer.found
        assert "60" in answer.text
        assert answer.caveat == PRICE_CAVEAT
        assert PRICE_CAVEAT in answer.render()           # text form
        assert PRICE_CAVEAT in answer.render(speak=True)  # spoken form

    def test_every_price_has_a_caveat_across_the_catalogue(self, answers, corpus):
        """Structural, not spot-checked: no product can yield an uncaveated price."""
        for product in corpus.all_products():
            if product.price_amount is None:
                continue
            answer = answers.commercial(FactKind.PRICE, product)
            assert answer.caveat, f"{product.product_id} produced a price with no caveat"

    def test_price_renders_indian_conventions(self, answers, corpus):
        """₹14,50,000 is said 'about 14.5 lakh rupees' — a TTS engine reading
        the raw digits is unintelligible."""
        product = corpus.get("6835567")  # Apollo RT Cath Lab, ₹14,50,000
        answer = answers.commercial(FactKind.PRICE, product)
        assert "14,50,000" in answer.text
        assert "lakh" in answer.spoken

    def test_small_price_is_spoken_as_digits_not_lakh(self, answers, corpus):
        product = corpus.get("270694")  # ₹60
        assert "lakh" not in answers.commercial(FactKind.PRICE, product).spoken

    def test_moq_is_exact(self, answers, corpus):
        product = corpus.get("270694")  # MOQ 10 Pieces
        answer = answers.commercial(FactKind.MOQ, product)
        assert answer.found
        assert "10" in answer.text

    def test_delivery_time(self, answers, corpus):
        answer = answers.commercial(FactKind.DELIVERY, corpus.get("270691"))
        assert answer.found
        assert "7" in answer.text

    def test_moq_does_not_carry_the_price_caveat(self, answers, corpus):
        """The caveat is for prices. Attaching it to every commercial answer
        would train buyers to ignore it."""
        answer = answers.commercial(FactKind.MOQ, corpus.get("270694"))
        assert answer.caveat is None

    def test_fact_registry_marks_only_price_as_caveated(self):
        caveated = [k for k, f in FACT_REGISTRY.items() if f.needs_caveat]
        assert caveated == [FactKind.PRICE]

    def test_every_answer_cites_its_source(self, answers, corpus):
        for kind in (FactKind.PRICE, FactKind.MOQ, FactKind.DELIVERY):
            answer = answers.commercial(kind, corpus.get("270691"))
            assert answer.grounded_on.startswith("products[270691]")
            assert answer.source_url


# ---------------------------------------------------------------------------
# Honest absence (DG-13)
# ---------------------------------------------------------------------------
class TestHonestAbsence:
    def test_product_without_specs_says_so(self, answers, corpus):
        """3 products have no specification block on the source site."""
        product = corpus.get("381893")
        assert product is not None
        answer = answers.spec_list(product)
        assert not answer.found
        assert "don't have" in answer.text.lower()
        # It must decline rather than state a value, and must not double the
        # word "published" (a bug the first version shipped with).
        assert "published specification list" in answer.text
        assert "published specification list published" not in answer.text

    def test_absent_spec_is_distinguished_from_missing_load(self, corpus):
        view = corpus.product_view("381893")
        assert view is not None
        assert view.specs == []
        assert view.specs_absent_on_source is True

    def test_absent_about_is_recorded(self, corpus):
        view = corpus.product_view("270683")
        assert view is not None
        assert view.about_absent_on_source is True

    def test_unknown_spec_on_a_product_that_has_specs(self, answers, corpus):
        answer = answers.specification(corpus.get("270691"), "Warp Core Output")
        assert not answer.found


# ---------------------------------------------------------------------------
# Comparison (UC-06)
# ---------------------------------------------------------------------------
class TestComparison:
    def test_comparison_uses_the_shared_specification_intersection(self, answers, corpus):
        left = corpus.get("270691")   # X-Ray Cassette
        right = corpus.get("270692")  # Xray Intensifying Screen
        answer = answers.compare(left, right)
        assert answer.found
        assert left.name in answer.text and right.name in answer.text

    def test_comparison_never_invents_a_value(self, answers, corpus):
        """Every 'name: value' pair in the comparison must exist on BOTH sides."""
        left, right = corpus.get("270691"), corpus.get("270692")
        left_specs = {s.name.casefold() for s in corpus.specs_for(left.product_id)}
        right_specs = {s.name.casefold() for s in corpus.specs_for(right.product_id)}
        shared = left_specs & right_specs
        answer = answers.compare(left, right)
        for line in answer.text.splitlines()[1:]:
            spec_name = line.split(":", 1)[0].strip().casefold()
            assert spec_name in shared

    def test_comparison_of_a_product_with_no_specs_declines(self, answers, corpus):
        answer = answers.compare(corpus.get("381893"), corpus.get("270691"))
        assert not answer.found


# ---------------------------------------------------------------------------
# Company facts (BRD-08)
# ---------------------------------------------------------------------------
class TestCompanyFacts:
    @pytest.mark.parametrize("kind", ["gst", "pan", "iec", "address", "directors", "certifications"])
    def test_company_facts_are_answerable(self, answers, kind):
        answer = answers.company_fact(kind)
        assert answer.found, f"{kind} not answerable"
        assert answer.text

    def test_gst_numbers_match_the_catalogue(self, answers, corpus):
        answer = answers.company_fact("gst")
        for value in corpus.company_ids().get("gst", []):
            assert value in answer.text

    def test_gst_is_spelled_for_voice(self, answers):
        """Reading a GSTIN as a word is useless; it must be read out character
        by character."""
        answer = answers.company_fact("gst")
        assert "7" in answer.spoken and "Z" in answer.spoken


# ---------------------------------------------------------------------------
# The outage property (DG-03)
# ---------------------------------------------------------------------------
class TestLLMIndependence:
    def test_no_answer_path_imports_an_llm(self):
        """The deterministic path must survive an LLM outage.

        Asserted by inspection: `answers.py` and `corpus.py` must not import a
        model client. If someone later wires generation into this module, this
        test fails and the degradation guarantee is visibly broken rather than
        silently lost.
        """
        import ast

        for name in ("answers.py", "corpus.py"):
            tree = ast.parse((PLATFORM / "app" / "anita" / name).read_text(encoding="utf-8"))
            imported: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported += [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imported.append(node.module or "")
            forbidden = [m for m in imported if any(
                token in m.lower() for token in ("ollama", "openai", "llm", "mlx", "transformers")
            )]
            assert not forbidden, f"{name} imports {forbidden} — the deterministic path must not depend on a model"


# ---------------------------------------------------------------------------
# Resolving a product from a *question*
#
# Found by asking the live instance "what is the price of the ultrasound gel?"
# and being told about diagnostic suitability instead. The resolver matched
# references ("Ultrasound Gel") but never a name inside a question, so no
# natural-language question could reach the deterministic engine at all.
# ---------------------------------------------------------------------------
class TestResolutionFromAQuestion:
    @pytest.fixture()
    def answers(self):
        from anita.answers import AnitaAnswers
        from anita.corpus import Corpus

        return AnitaAnswers(Corpus(CORPUS_DIR))

    def test_a_product_named_inside_a_question_resolves(self, answers):
        resolution = answers.resolver.resolve("what is the price of the ultrasound gel?")
        assert resolution.ok
        assert resolution.product.name == "Ultrasound Gel"

    def test_the_full_name_inside_a_question_resolves(self, answers):
        resolution = answers.resolver.resolve("give me the X-Ray Cassette specification")
        assert resolution.ok
        assert resolution.product.name == "X-Ray Cassette"

    def test_a_part_code_resolves_however_it_is_written(self, answers):
        """The same part is written "APO 200", "APO-200" and "apo200" depending
        on who is typing and where they read it. The code is not in the model
        field — it is only inside the name."""
        for spelling in ("APO 200", "APO-200", "apo200"):
            resolution = answers.resolver.resolve(f"what is the price of the {spelling} injector?")
            assert resolution.ok, spelling
            assert "APO 200" in resolution.product.name

    def test_letters_that_merely_appear_in_a_name_are_not_a_code(self, answers):
        """"CT scanner" must not resolve — no product is called that, and "CT"
        appears in several names."""
        assert not answers.resolver.resolve("what is the price of a CT scanner?").ok

    def test_a_word_containing_a_product_name_is_not_a_match(self, answers):
        """Plain substring containment would match "gel" inside "gelatin"."""
        assert not answers.resolver.resolve("what does the gelatin cost").ok

    def test_two_products_sharing_a_name_stay_ambiguous(self, answers):
        """The catalogue holds two "Medical Dry Imager" at very different
        prices. The longer question must not turn that into a guess."""
        from anita.answers import ResolutionKind

        resolution = answers.resolver.resolve("what is the price of the medical dry imager?")
        assert resolution.kind is ResolutionKind.AMBIGUOUS
        assert len(resolution.candidates) == 2

    def test_the_more_specific_name_wins(self, answers):
        """"Medical Dry Imager" contains "Dry Imager"; the longer name is the
        one being asked about."""
        resolution = answers.resolver.resolve("what is the price of the medical dry imager?")
        assert all("Medical Dry Imager" in c.name for c in resolution.candidates)


# ---------------------------------------------------------------------------
# Choosing which fact a question asks for
# ---------------------------------------------------------------------------
class TestFactSelection:
    def test_a_price_question_selects_price(self):
        from anita.answers import FactKind, fact_kind_for

        for question in ("what is the price?", "how much does it cost?",
                         "what is the rate for this?", "give me a quotation"):
            assert fact_kind_for(question) is FactKind.PRICE, question

    def test_an_moq_question_selects_moq(self):
        from anita.answers import FactKind, fact_kind_for

        assert fact_kind_for("what is the minimum order quantity?") is FactKind.MOQ
        assert fact_kind_for("what's the MOQ?") is FactKind.MOQ

    def test_a_delivery_question_selects_delivery(self):
        from anita.answers import FactKind, fact_kind_for

        assert fact_kind_for("how long does delivery take?") is FactKind.DELIVERY
        assert fact_kind_for("what is the lead time?") is FactKind.DELIVERY

    def test_an_unrecognised_question_selects_nothing(self):
        """None is the safe answer: the question goes to retrieval instead of
        being answered against a fact nobody asked about."""
        from anita.answers import fact_kind_for

        assert fact_kind_for("is it blue?") is None
        assert fact_kind_for("") is None

    def test_a_commercial_fact_carries_its_caveat(self):
        """The caveat is produced by the same call as the number, so the two
        cannot be separated."""
        from anita.answers import FACT_REGISTRY, FactKind

        assert FACT_REGISTRY[FactKind.PRICE].needs_caveat
        assert FACT_REGISTRY[FactKind.PRICE].is_commercial
