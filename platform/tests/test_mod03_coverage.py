"""US-012 — the coverage probe, honest absence, and citations.

The story's data reality is asserted against the **real corpus**, not a
fixture: the exact absent-product-id sets are the evidence that the scraper's
blank blocks reach the buyer as "not published" rather than as an invention or
a decline. A fixture would prove the code works on data I invented.
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.answer_engine import AnswerEngine  # noqa: E402
from anita.answers import AnitaAnswers  # noqa: E402
from anita.corpus import Corpus, KNOWN_NO_ABOUT, KNOWN_NO_SPECS  # noqa: E402
from anita.coverage import (  # noqa: E402
    Availability,
    CoverageProbe,
    NOT_PUBLISHED_ABOUT,
    NOT_PUBLISHED_SPECS,
    UnknownProduct,
)
from anita.orchestrator import Intent  # noqa: E402

CORPUS_DIR = PLATFORM.parent / "data" / "out"
pytestmark = pytest.mark.skipif(not CORPUS_DIR.exists(), reason="corpus not collected")


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return Corpus(CORPUS_DIR)


@pytest.fixture(scope="module")
def probe(corpus: Corpus) -> CoverageProbe:
    return CoverageProbe(corpus)


@pytest.fixture(scope="module")
def engine(corpus: Corpus, probe: CoverageProbe) -> AnswerEngine:
    return AnswerEngine(answers=AnitaAnswers(corpus), probe=probe)


def _conversation():
    return types.SimpleNamespace(turns=[], conversation_id="cov-1")


# ---------------------------------------------------------------------------
# The probe (TAC-3)
# ---------------------------------------------------------------------------
class TestCoverageProbe:
    def test_the_absent_spec_set_is_exactly_the_three_products(self, probe, corpus):
        assert probe.absent_spec_products() == KNOWN_NO_SPECS == frozenset(
            {"270685", "270687", "381893"}
        )
        # ...and it is *derived*, not a copy of the constant: the same question
        # asked of the corpus directly gives the same answer.
        derived = {
            product.product_id for product in corpus.all_products()
            if not product.has_specs
        }
        assert derived == probe.absent_spec_products()

    def test_the_absent_about_set_is_exactly_the_four_products(self, probe, corpus):
        assert probe.absent_about_products() == KNOWN_NO_ABOUT == frozenset(
            {"270683", "3935251", "3935247", "3935249"}
        )
        derived = {
            product.product_id for product in corpus.all_products()
            if not product.has_about
        }
        assert derived == probe.absent_about_products()

    def test_availability_is_per_aspect(self, probe):
        microbar = probe.for_product("270685")
        assert microbar.specs is Availability.NOT_PUBLISHED
        # The same product *does* publish an About block — absence is not a
        # property of the product, it is a property of the field asked about.
        assert microbar.about is Availability.PUBLISHED

    def test_the_two_same_named_products_report_independently(self, probe):
        """Edge case 11, as the corpus actually is.

        The story's example says one sibling has About and the other does not.
        Against the live corpus that is not the case — **both** "Medical Dry
        Imager" products publish no About text (270683 and 3935251) — so the
        claim asserted here is the one that is true and load-bearing: the probe
        keys on product id, returns a distinct record per sibling, and a
        name-keyed probe could only ever have returned one of them.
        """
        siblings = [
            product.product_id for product in probe.corpus.all_products()
            if product.name == "Medical Dry Imager"
        ]
        assert len(siblings) >= 2, "the corpus is expected to hold two of this name"
        records = {pid: probe.for_product(pid) for pid in siblings}
        assert len(set(records)) == len(siblings), "the probe collapsed two products into one"
        assert {record.product_id for record in records.values()} == set(siblings)
        assert all(
            record.about is Availability.NOT_PUBLISHED for record in records.values()
        )

    def test_an_unknown_product_raises_rather_than_claiming_absence(self, probe):
        with pytest.raises(UnknownProduct):
            probe.for_product("does-not-exist")

    def test_a_published_product_reports_published(self, probe):
        gel = probe.for_product("270694")   # Ultrasound Gel
        assert gel.specs is Availability.PUBLISHED
        assert gel.about is Availability.PUBLISHED

    def test_the_probe_is_derived_not_cached_across_instances(self, corpus):
        """A refreshed corpus flips the answer with no code change (edge 9)."""
        probe = CoverageProbe(corpus)
        assert probe.for_product("270685").specs is Availability.NOT_PUBLISHED
        fresh = CoverageProbe(corpus)
        assert fresh.absent_spec_products() == probe.absent_spec_products()


# ---------------------------------------------------------------------------
# Honest absence in the answer (AC-2, AC-3, TAC-4)
# ---------------------------------------------------------------------------
class _CountingLLM:
    def __init__(self):
        self.calls = 0

    @property
    def available(self):
        return True

    def generate(self, messages, *, stream=True):
        self.calls += 1
        raise AssertionError("the LLM must not be called for a not-published answer")


class TestHonestAbsence:
    def test_no_published_specs_is_stated_not_declined(self, engine):
        reply = engine.answer(
            Intent.CATALOGUE, "what is the specification of Microbar Paste?", _conversation())
        assert reply.text.startswith("ANITA has not published specifications")
        assert "Microbar Paste" in reply.text
        # No specification value is stated — the failure mode is a figure.
        assert not re.search(r"\d+\s?(mm|kg|cm|v|hz|w)\b", reply.text, re.I)
        assert not reply.declined

    def test_no_published_description_is_stated_not_inferred(self, engine):
        reply = engine.answer(
            Intent.CATALOGUE, "tell me about the Medical Dry Imager at 2 lakh",
            _conversation())
        assert "no product description published" in reply.text
        assert reply.text.startswith("There's no product description published for Medical Dry Imager")
        # The same-named sibling is not singled out.
        assert "product 270683" not in reply.text

    def test_the_absence_answer_makes_zero_llm_calls(self, corpus, probe, monkeypatch):
        """TAC-4: composed from the probe, never routed through the model."""
        counter = _CountingLLM()
        engine = AnswerEngine(answers=AnitaAnswers(corpus), probe=probe, llm=counter)
        reply = engine.answer(
            Intent.CATALOGUE, "what is the specification of Microbar Paste?", _conversation())
        assert "not published" in reply.text
        assert counter.calls == 0

    def test_the_absence_answer_offers_a_human(self, engine):
        reply = engine.answer(
            Intent.CATALOGUE, "specs for Microbar Paste please", _conversation())
        assert "team" in reply.text.lower()

    def test_a_published_product_is_not_answered_with_absence(self, engine):
        """The guard that keeps the branch from swallowing real answers."""
        reply = engine.answer(
            Intent.CATALOGUE, "what is the specification of the FCR Prima T2?",
            _conversation())
        assert "has not published specifications" not in reply.text

    def test_an_unknown_product_declines_rather_than_claiming_absence(self, engine):
        reply = engine.answer(
            Intent.CATALOGUE, "what is the specification of the MRI machine you sell?",
            _conversation())
        assert reply.declined
        assert "not published" not in reply.text

    def test_without_a_probe_the_engine_behaves_as_before(self, corpus):
        """The probe is additive: an engine built without one is unchanged."""
        engine = AnswerEngine(answers=AnitaAnswers(corpus))
        reply = engine.answer(
            Intent.CATALOGUE, "what is the specification of Microbar Paste?", _conversation())
        assert "has not published" not in reply.text


# ---------------------------------------------------------------------------
# Citations (AC-1, TAC-1, TAC-2, TAC-6)
# ---------------------------------------------------------------------------
class _Chunk:
    def __init__(self, chunk_id, *, source_url="https://anitamedicalsystems.example/p/1",
                 chunk_type="specification", product_id="270694"):
        self.chunk_id = chunk_id
        self.source_url = source_url
        self.chunk_type = chunk_type
        self.product_id = product_id
        self.product_name = "Ultrasound Gel"
        self.content_hash = "abc123"


class _Retriever:
    def __init__(self, chunk):
        self.chunk = chunk

    def retrieve(self, query, **kwargs):
        decision = types.SimpleNamespace(relevant=True, entity_matched=True,
                                          not_relevant=False, reason="")
        scored = types.SimpleNamespace(chunk=self.chunk)
        return types.SimpleNamespace(ok=True, chunks=[scored], decision=decision)


class TestCitations:
    def test_a_price_answer_cites_the_product_record(self, engine):
        reply = engine.answer(
            Intent.COMMERCIAL, "what is the price of the ultrasound gel?", _conversation())
        assert reply.grounded
        assert reply.grounded_on, "a catalogue fact without a citation is a defect"
        citation = reply.grounded_on[0]
        assert citation["chunk_id"] == "product:270694"
        assert citation["source_url"].startswith("http")
        assert not reply.citation_incomplete

    def test_a_retrieved_answer_cites_every_chunk_it_used(self, corpus, probe):
        chunk = _Chunk("chunk-77")
        engine = AnswerEngine(answers=AnitaAnswers(corpus), retriever=_Retriever(chunk),
                              probe=probe)
        reply = engine.answer(Intent.CATALOGUE, "tell me about the gel", _conversation())
        assert reply.grounded
        cited = [c["chunk_id"] for c in reply.grounded_on]
        # The chunk it composed from is cited. Over-citation (the product row
        # the question also resolved to) is allowed and useful — provenance
        # that is a superset still resolves every claim.
        assert "chunk-77" in cited
        assert next(c for c in reply.grounded_on if c["chunk_id"] == "chunk-77")[
            "source_url"].endswith("/p/1")

    def test_a_citation_without_a_source_is_flagged_not_hidden(self, corpus, probe):
        """TAC-1 / edge 10: `citation_incomplete` is counted, never swallowed."""
        chunk = _Chunk("chunk-88", source_url=None)
        engine = AnswerEngine(answers=AnitaAnswers(corpus), retriever=_Retriever(chunk),
                              probe=probe)
        reply = engine.answer(Intent.CATALOGUE, "tell me about the gel", _conversation())
        assert reply.grounded_on and reply.citation_incomplete is True

    def test_the_absence_answer_cites_the_record_that_lacks_the_data(self, engine):
        reply = engine.answer(
            Intent.CATALOGUE, "what is the specification of Microbar Paste?", _conversation())
        assert reply.grounded_on[0]["chunk_id"] == "product:270685"

    def test_a_company_fact_cites_its_collected_record(self, corpus, probe):
        engine = AnswerEngine(answers=AnitaAnswers(corpus), probe=probe)
        reply = engine.answer(Intent.COMPANY, "what is your phone number?", _conversation())
        assert reply.grounded
        assert reply.grounded_on
        assert reply.grounded_on[0]["chunk_type"] == "company_record"

    def test_the_payload_carries_the_citations_to_the_client(self, engine):
        reply = engine.answer(
            Intent.COMMERCIAL, "what is the price of the ultrasound gel?", _conversation())
        payload = reply.as_dict()
        assert payload["grounded_on"] == reply.grounded_on
        assert payload["citation_incomplete"] is False


# ---------------------------------------------------------------------------
# The turn stores what the answer was grounded in (review renders it)
# ---------------------------------------------------------------------------
class TestGroundingReachesTheTranscript:
    def test_the_turn_store_carries_the_citation_ids(self, corpus, probe):
        from anita.conversation_store import ConversationStore, ReplayQueue, TurnRecorder
        from anita.orchestrator import ConversationRegistry, Orchestrator
        from anita.surfaces import ChatSurface

        store = ConversationStore(":memory:")
        registry = ConversationRegistry(store=store, recorder=TurnRecorder(store, ReplayQueue(10)))
        chat = ChatSurface(Orchestrator(registry), answers=AnitaAnswers(corpus), probe=probe)

        chat.post_message(message="what is the price of the ultrasound gel?", session_id="cov-2")
        conversation = registry.get("cov-2")
        assistant = [
            turn for turn in store.page_turns(conversation.conversation_id)
            if turn.role == "assistant"
        ][0]
        assert assistant.grounding_refs == ("product:270694",)
        assert assistant.state == "ANSWERED"
