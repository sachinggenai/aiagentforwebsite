"""MOD-03 tests — BM25, fusion, and the grounding gate.

The gate is the reason this module matters: `BRD-04` requires the system to
decline rather than improvise, and the gate is the mechanism that makes that
structural rather than a prompt instruction.

Tested with :class:`HashEmbedder` — a deterministic bag-of-words vectoriser —
so the retrieval path is exercised without ChromaDB installed or an embedding
service warm. The dense leg's *quality* depends on the real model; its
*plumbing*, and every decision the gate makes, do not.

Run:  .venv\\Scripts\\python -m pytest tests -q
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.answers import AnitaAnswers  # noqa: E402
from anita.corpus import Corpus  # noqa: E402
from anita.retrieval import (  # noqa: E402
    DENSE_WEIGHT,
    SPARSE_WEIGHT,
    Chunk,
    GroundingGate,
    InMemoryStore,
    MiniBM25,
    Retriever,
    assert_threshold,
    load_chunks,
    tokenize,
    weighted_rrf,
)

CORPUS_DIR = PLATFORM.parent / "data" / "out"
pytestmark = pytest.mark.skipif(
    not (CORPUS_DIR / "chunks.jsonl").exists(),
    reason="corpus not built — run the scraper in src/ams first",
)


class HashEmbedder:
    """Deterministic embedder: hashed bag of words, L2-normalised.

    Not a good embedder — a *predictable* one. It makes dense retrieval testable
    without a model, and because it is purely lexical it exercises exactly the
    fusion logic (where dense and sparse disagree) rather than papering over it.
    """

    def __init__(self, dimensions: int = 256) -> None:
        self.dimensions = dimensions
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in tokenize(text):
                vector[hash(token) % self.dimensions] += 1.0
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            vectors.append([v / norm for v in vector])
        return vectors


@pytest.fixture(scope="module")
def chunks() -> list[Chunk]:
    return load_chunks(CORPUS_DIR / "chunks.jsonl")


@pytest.fixture(scope="module")
def retriever(chunks) -> Retriever:
    # Built the way production builds it — from_chunks teaches the gate the
    # catalogue's vocabulary. Constructing it by hand here previously hid a real
    # gap: part-number queries were declined because the gate never learned that
    # "apo-200" is a catalogue term.
    gate = GroundingGate.from_chunks(chunks, threshold=0.62)
    r = Retriever(chunks, HashEmbedder(), InMemoryStore(), gate, top_k=5, fetch_k=20)
    r.build_index()
    return r


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------
class TestTokenize:
    def test_keeps_part_numbers_intact(self):
        """Sparse retrieval earns its 35% weight on exactly these queries."""
        assert "apo-200" in tokenize("Apollo APO-200 injector")
        assert "fcr" in tokenize("FCR Prima T2")

    def test_lowercases(self):
        assert tokenize("Ultrasound GEL") == ["ultrasound", "gel"]

    def test_drops_stopwords(self):
        assert "the" not in tokenize("the cassette")

    def test_empty(self):
        assert tokenize("") == []


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def bm25():
    return MiniBM25([
        "X-Ray Cassette manual operation light-tight box",
        "Ultrasound Gel conductive medium paraben free",
        "Pressure Injector APO-200 CT dual head",
    ])


class TestMiniBM25:
    def test_finds_the_matching_document(self, bm25):
        hits = bm25.search("ultrasound gel")
        assert hits and hits[0][0] == 1

    def test_part_number_query_hits_the_right_document(self, bm25):
        hits = bm25.search("APO-200")
        assert hits and hits[0][0] == 2

    def test_no_match_returns_nothing(self, bm25):
        """A query with no terms in any document must return an empty list, not
        a low-scoring best guess — a best guess is how a gate gets bypassed."""
        assert bm25.search("quantum flux capacitor") == []

    def test_empty_query_returns_nothing(self, bm25):
        assert bm25.search("") == []

    def test_scores_are_positive_and_ordered(self, bm25):
        hits = bm25.search("cassette x-ray")
        assert all(score > 0 for _, score in hits)
        assert hits == sorted(hits, key=lambda p: p[1], reverse=True)

    def test_common_terms_do_not_produce_negative_scores(self):
        """A term in every document has idf ~0; it must not push a match below
        a non-match."""
        bm25 = MiniBM25(["cassette one", "cassette two", "cassette three"])
        for _, score in bm25.search("cassette"):
            assert score >= 0.0

    def test_empty_corpus_is_survivable(self):
        assert MiniBM25([]).search("anything") == []


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------
class TestWeightedRRF:
    def test_agreement_between_legs_wins(self):
        """A document both legs rank highly must beat one only a single leg likes."""
        fused = weighted_rrf(dense=[0, 1, 2], sparse=[0, 2, 1])
        assert max(fused, key=fused.get) == 0

    def test_a_document_found_by_only_one_leg_still_scores(self):
        fused = weighted_rrf(dense=[0], sparse=[1])
        assert set(fused) == {0, 1}

    def test_dense_leg_outweighs_sparse(self):
        """0.65/0.35, deliberately not 0.5/0.5."""
        fused = weighted_rrf(dense=[0], sparse=[1])
        assert fused[0] > fused[1]
        assert DENSE_WEIGHT > SPARSE_WEIGHT

    def test_weights_are_normalised_conceptually(self):
        assert pytest.approx(1.0, abs=1e-9) == DENSE_WEIGHT + SPARSE_WEIGHT

    def test_disjoint_legs_union(self):
        fused = weighted_rrf(dense=[0, 1], sparse=[2, 3])
        assert set(fused) == {0, 1, 2, 3}

    def test_empty_inputs(self):
        assert weighted_rrf([], []) == {}


# ---------------------------------------------------------------------------
# Grounding gate — the point of the module
# ---------------------------------------------------------------------------
class TestGroundingGate:
    def test_high_similarity_passes(self):
        gate = GroundingGate(threshold=0.62)
        decision = gate.evaluate("what is the price", 0.80)
        assert decision.relevant
        assert decision.reason == "similarity"

    def test_low_similarity_with_no_entity_is_declined(self):
        """This is BRD-04: refuse rather than answer from the nearest chunk."""
        gate = GroundingGate(threshold=0.62, known_entities={"X-Ray Cassette"})
        decision = gate.evaluate("what is the weather in Mumbai", 0.31)
        assert decision.not_relevant
        assert decision.reason == "below_threshold"

    def test_entity_match_passes_below_threshold(self):
        """A buyer who names a real product has asked something answerable,
        whatever the embedding scored."""
        gate = GroundingGate(threshold=0.62, known_entities={"X-Ray Cassette"})
        decision = gate.evaluate("MOQ on the X-Ray Cassette?", 0.41)
        assert decision.relevant
        assert decision.reason == "entity_match"

    def test_entity_match_is_case_insensitive(self):
        gate = GroundingGate(threshold=0.9, known_entities={"Ultrasound Gel"})
        assert gate.evaluate("tell me about ULTRASOUND GEL", 0.1).relevant

    def test_partial_entity_name_does_not_match(self):
        """'Dry' must not make every question relevant by accident."""
        gate = GroundingGate(threshold=0.9, known_entities={"Medical Dry Imager"})
        assert gate.evaluate("is it dry outside", 0.1).not_relevant

    def test_zero_threshold_is_refused(self):
        """A zero threshold disables the gate — every question becomes
        answerable from the nearest chunk, which is the exact failure BRD-04
        forbids. It is a configuration error, not a tuning choice."""
        with pytest.raises(ValueError, match="greater than 0.0"):
            assert_threshold(0.0)

    def test_negative_threshold_is_refused(self):
        with pytest.raises(ValueError):
            assert_threshold(-0.1)

    def test_threshold_above_one_is_refused(self):
        with pytest.raises(ValueError, match="cannot exceed 1.0"):
            assert_threshold(1.5)

    def test_a_sensible_threshold_passes(self):
        assert_threshold(0.62)

    def test_from_chunks_learns_the_catalogue_vocabulary(self, chunks):
        """The gate must know what a catalogue term looks like.

        Without this, a buyer typing a part number is declined despite BM25
        finding the product immediately — the sparse evidence never reached the
        gate.
        """
        gate = GroundingGate.from_chunks(chunks, threshold=0.62)
        for term in ("apo200", "cassette", "injector", "ultrasound"):
            assert term in gate.distinctive_terms
        # Non-catalogue words must never be treated as catalogue terms — an
        # earlier frequency-based version included "mumbai" and a GSTIN.
        for term in ("mumbai", "07aaaca3337r1zf", "weather"):
            assert term not in gate.distinctive_terms

    def test_the_vocabulary_stays_bounded(self, chunks):
        """A gate that matches everything is not a gate. The frequency-based
        version grew to 1,715 terms.

        The bound was 120 when the vocabulary was product names alone (~54
        terms). Specification *field names* are now included too — they are
        catalogue vocabulary in the same sense, and without them the gate
        refused "contrast media compatibility", a spec name present verbatim in
        the corpus. That took it to ~173.

        The number is a proxy, and the proxy is not the property that matters.
        What matters is asserted directly by the tests around this one: the
        spec-name queries are admitted, and `mumbai`, the GSTIN, the weather and
        the world cup are still refused. If this bound is raised again, raise it
        with an off-topic test that would fail if the gate had gone slack —
        never on its own.
        """
        gate = GroundingGate.from_chunks(chunks, threshold=0.62)
        assert len(gate.distinctive_terms) < 300

    def test_specification_field_names_are_catalogue_vocabulary(self, chunks):
        """The false negative this exists to prevent: a buyer asking for a spec
        by name — the most precise question they can ask — being told the
        catalogue does not cover it."""
        gate = GroundingGate.from_chunks(chunks, threshold=0.62)
        for query in ("contrast media compatibility", "syringe capacity",
                      "what is the flow rate range"):
            decision = gate.evaluate(query, 0.30)
            assert decision.relevant, query

    def test_adding_spec_names_did_not_admit_the_world(self, chunks):
        """The counterweight to the test above. Broadening the vocabulary is
        only safe while these still refuse."""
        gate = GroundingGate.from_chunks(chunks, threshold=0.62)
        for query in ("what is the weather in mumbai", "who won the world cup",
                      "pizza delivery", "07AAACA3337R1ZF"):
            decision = gate.evaluate(query, 0.30)
            assert not decision.relevant, query

    def test_distinctive_term_admits_a_part_number_query(self, chunks):
        gate = GroundingGate.from_chunks(chunks, threshold=0.62)
        decision = gate.evaluate("APO-200", 0.35)
        assert decision.relevant
        assert decision.reason == "entity_match"

    def test_a_short_common_name_word_is_not_enough_on_its_own(self, chunks):
        """'dry' is in three product names, so alone it made 'is it dry
        outside' look like a catalogue question."""
        gate = GroundingGate.from_chunks(chunks, threshold=0.9)
        assert gate.evaluate("is it dry outside", 0.2).not_relevant
        # With a strong term alongside, the same short word is fine.
        assert gate.evaluate("dry imager specifications", 0.1).relevant

    def test_off_topic_queries_are_declined_by_vocabulary_not_luck(self, chunks):
        """The critical regression: a frequency-based gate admitted 'what is the
        weather in Mumbai' because 'mumbai' was rare in the corpus."""
        gate = GroundingGate.from_chunks(chunks, threshold=0.62)
        for query in (
            "what is the weather in Mumbai",
            "who won the 1994 football World Cup",
            "explain quantum entanglement",
            "what is your refund policy for university admissions",
        ):
            assert gate.evaluate(query, 0.35).not_relevant, query


class TestBootGuardOnThreshold:
    def test_zero_threshold_fails_the_boot(self, tmp_path):
        """The guard is wired into config validation, so it refuses at boot
        with a named key rather than misbehaving at runtime."""
        from anita.config import _validate
        from anita.errors import ConfigError

        import dataclasses

        from anita.config import RetrievalSettings, load_settings

        settings = load_settings(project_root=PLATFORM)
        broken = dataclasses.replace(
            settings, retrieval=RetrievalSettings(top_k=5, fetch_k=20, similarity_threshold=0.0)
        )
        with pytest.raises(ConfigError) as exc:
            _validate(broken)
        assert exc.value.key == "retrieval.similarity_threshold"


# ---------------------------------------------------------------------------
# Retriever — end to end over the real corpus
# ---------------------------------------------------------------------------
class TestRetriever:
    def test_indexes_the_whole_corpus(self, retriever, chunks):
        assert retriever.store.count() == len(chunks)
        assert len(chunks) > 100

    def test_relevant_query_returns_grounded_chunks(self, retriever):
        result = retriever.retrieve("What is the minimum order quantity for the ultrasound gel?")
        assert result.ok
        assert result.chunks
        assert result.provenance

    def test_provenance_carries_a_traceable_source(self, retriever):
        """TRD-16: every answer must trace to a chunk, then to a URL."""
        result = retriever.retrieve("ultrasound gel storage")
        assert result.ok
        for entry in result.provenance:
            assert entry["chunk_id"]
            assert entry["source_url"]
            assert "anitamedicalsystems.com" in entry["source_url"]

    def test_off_topic_query_is_declined(self, retriever):
        """The whole point: an unanswerable question must not produce context."""
        result = retriever.retrieve("What is the weather in Mumbai tomorrow?")
        assert not result.ok
        assert result.chunks == []
        assert result.decision.not_relevant

    def test_declined_query_yields_no_context(self, retriever):
        """A decline must produce an empty context string — if any text reached
        the prompt, the model would answer from it."""
        result = retriever.retrieve("Who won the 1994 football World Cup?")
        assert result.context() == ""

    def test_part_number_query_finds_the_product(self, retriever):
        """The sparse leg's reason for existing."""
        result = retriever.retrieve("APO-200 pressure injector")
        assert result.ok

    def test_context_is_bounded(self, retriever):
        result = retriever.retrieve("ultrasound gel")
        assert result.ok
        assert len(result.context(max_chars=500)) <= 700

    def test_context_truncates_on_chunk_boundaries(self, retriever):
        result = retriever.retrieve("ultrasound gel")
        context = result.context(max_chars=400)
        # Never a half chunk: blocks are joined by the separator.
        if context:
            assert context.count("[§ ") == len(context.split("\n\n---\n\n"))

    def test_top_k_is_respected(self, retriever):
        result = retriever.retrieve("medical imaging equipment", top_k=3)
        assert len(result.chunks) <= 3

    def test_empty_store_declines_rather_than_crashing(self, chunks):
        gate = GroundingGate(threshold=0.62)
        empty = Retriever(chunks, HashEmbedder(), InMemoryStore(), gate)
        result = empty.retrieve("ultrasound gel")  # store never built
        assert not result.ok

    def test_rebuild_is_idempotent(self, chunks):
        store = InMemoryStore()
        gate = GroundingGate(threshold=0.62)
        r = Retriever(chunks, HashEmbedder(), store, gate)
        first = r.build_index()
        second = r.build_index()
        assert first == second == len(chunks)


# ---------------------------------------------------------------------------
# The refusal path, end to end (BRD-04)
# ---------------------------------------------------------------------------
class TestRefusalIsStructural:
    def test_a_declined_retrieval_cannot_produce_an_answer(self, retriever):
        """The gate is called *before* composition, so 'no context, no answer'
        holds without relying on a prompt instruction."""
        result = retriever.retrieve("explain quantum entanglement")
        assert result.decision.not_relevant
        assert result.provenance == []
        assert result.context() == ""

    def test_every_retrieved_chunk_is_from_the_anita_catalogue(self, retriever):
        """BRD-18: nothing from the sibling deployment can appear in context."""
        for query in ("ultrasound gel", "x-ray cassette", "pressure injector"):
            for entry in retriever.retrieve(query).provenance:
                assert entry["source_url"].startswith("https://www.anitamedicalsystems.com/")

    def test_known_absent_products_do_not_fabricate_context(self, retriever):
        """A product with no published specs must not yield a spec chunk."""
        result = retriever.retrieve("specifications of the Fuji Computed Radiography System")
        for scored in result.chunks:
            if scored.chunk.product_id == "381893":
                assert scored.chunk.chunk_type != "product_specs"
