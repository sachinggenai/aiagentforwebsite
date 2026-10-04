"""MOD-03 tests — embedding adapter and index lifecycle (TRD-12, TRD-15).

The embedding transport is exercised through a subclass that captures the
payload instead of making an HTTP call, so the *prefix scheme*, *batching* and
*dimension guard* — the parts that are ours — are tested without a live Ollama.
Whether Ollama returns good vectors is Ollama's problem.

The lifecycle is pure state logic and tested exhaustively: it is the thing that
decides whether a bad rebuild reaches a buyer.

Run:  .venv\\Scripts\\python -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.embeddings import (  # noqa: E402
    EmbeddingError,
    OllamaEmbedder,
    prefix_scheme_for,
)
from anita.index import (  # noqa: E402
    INDEX_BUILDS_DDL,
    CorpusError,
    IndexLifecycle,
    IndexState,
    MemoryBuildStore,
    assert_corpus_is_anita,
    corpus_fingerprint,
)
from anita.retrieval import Chunk, GroundingGate, InMemoryStore, Retriever  # noqa: E402
from anita.stores import CollectionNameError, assert_collection_name  # noqa: E402

CORPUS_DIR = PLATFORM.parent / "data" / "out"
HAS_CORPUS = (CORPUS_DIR / "chunks.jsonl").exists()


class RecordingEmbedder(OllamaEmbedder):
    """Captures the payload instead of calling Ollama."""

    def __init__(
        self,
        dimensions: int = 768,
        server_dimensions: int | None = None,
        fail_on: str | None = None,
        **kwargs,
    ):
        super().__init__("http://fake", expected_dimensions=dimensions, **kwargs)
        # What the *server* returns, which is not necessarily what the index
        # expects. Conflating the two made the dimension guard untestable.
        self.server_dimensions = server_dimensions or dimensions
        self.payloads: list[list[str]] = []
        self.fail_on = fail_on

    def _request(self, batch):
        self.payloads.append(list(batch))
        if self.fail_on and any(self.fail_on in text for text in batch):
            raise EmbeddingError(f"synthetic failure on {self.fail_on!r}")
        return [[0.1] * self.server_dimensions for _ in batch]


def make_chunks(n: int = 6, url: str = "https://www.anitamedicalsystems.com/p-1.html") -> list[Chunk]:
    return [
        Chunk(
            chunk_id=f"chunk-{i}",
            text=f"X-Ray Cassette specification row {i}",
            context_prefix="X-Ray Cassette [270691]",
            chunk_type="product_specs",
            product_id="270691",
            product_name="X-Ray Cassette",
            source_url=url,
            content_hash=f"hash{i}",
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Embedding adapter
# ---------------------------------------------------------------------------
class TestPrefixScheme:
    def test_nomic_uses_task_prefixes(self):
        """Skipping the prefixes costs real retrieval quality and produces no
        symptom — results still come back, just worse."""
        assert prefix_scheme_for("nomic-embed-text") == ("search_document: ", "search_query: ")
        assert prefix_scheme_for("nomic-embed-text:v1.5") is not None

    def test_unknown_model_has_no_scheme(self):
        assert prefix_scheme_for("all-minilm") is None

    def test_empty_model_name(self):
        assert prefix_scheme_for("") is None


class TestOllamaEmbedder:
    def test_documents_get_the_document_prefix(self):
        embedder = RecordingEmbedder()
        embedder.embed_documents(["cassette"])
        assert embedder.payloads[0][0].startswith("search_document:")

    def test_queries_get_the_query_prefix(self):
        """Documents and queries are embedded differently on purpose."""
        embedder = RecordingEmbedder()
        embedder.embed_query("what is the MOQ")
        assert embedder.payloads[0][0].startswith("search_query:")

    def test_embed_defaults_to_documents(self):
        embedder = RecordingEmbedder()
        embedder.embed(["chunk text"])
        assert embedder.payloads[0][0].startswith("search_document:")

    def test_batching_splits_into_batches(self):
        embedder = RecordingEmbedder(batch_size=4)
        embedder.embed_documents([f"doc {i}" for i in range(10)])
        assert [len(batch) for batch in embedder.payloads] == [4, 4, 2]

    def test_empty_input_makes_no_call(self):
        embedder = RecordingEmbedder()
        assert embedder.embed_documents([]) == []
        assert embedder.payloads == []

    def test_dimension_mismatch_is_refused(self):
        """Mixing models in one collection produces plausible nonsense — every
        query returns something, none of it meaningful.

        Simulates the real scenario: someone changed EMBED_MODEL, the server now
        returns 384 dimensions, and the index was built at 768.
        """
        embedder = RecordingEmbedder(dimensions=768, server_dimensions=384)
        with pytest.raises(EmbeddingError, match="dimensional"):
            embedder.embed_documents(["x"])

    def test_dimensions_are_pinned_after_first_call(self):
        embedder = RecordingEmbedder(dimensions=768)
        embedder.embed_documents(["x"])
        assert embedder.dimensions == 768

    def test_dimension_change_mid_run_is_refused(self):
        """A server that changes shape between batches is not trustworthy."""
        embedder = RecordingEmbedder(dimensions=768)
        embedder.embed_documents(["first"])
        embedder.server_dimensions = 512
        with pytest.raises(EmbeddingError, match="mid-run"):
            embedder.embed_documents(["second"])

    def test_describe_reports_the_scheme(self):
        described = RecordingEmbedder().describe()
        assert described["prefix_scheme"] is True
        assert described["backend"] == "ollama"


# ---------------------------------------------------------------------------
# Store guard
# ---------------------------------------------------------------------------
class TestCollectionGuard:
    @pytest.mark.parametrize("name", ["langchain", "meridian_kb__v1", "meridian-kb"])
    def test_sibling_collections_are_refused(self, name):
        """Naming a sibling collection means answering from the wrong catalogue."""
        with pytest.raises(CollectionNameError, match="sibling"):
            assert_collection_name(name)

    def test_own_collection_is_accepted(self):
        assert_collection_name("anita_ams__v1")

    def test_empty_collection_is_refused(self):
        with pytest.raises(CollectionNameError):
            assert_collection_name("")


# ---------------------------------------------------------------------------
# Corpus guard (REC-23 pattern)
# ---------------------------------------------------------------------------
class TestCorpusGuard:
    def test_anita_corpus_passes(self):
        assert_corpus_is_anita(make_chunks())

    def test_corpus_without_the_company_is_refused(self):
        with pytest.raises(CorpusError, match="anitamedicalsystems"):
            assert_corpus_is_anita(make_chunks(url="https://example.com/product.html"))

    def test_sibling_corpus_is_refused(self):
        """Indexing the university KB under this product returns confident,
        well-formed, entirely irrelevant answers — and nothing looks broken."""
        chunks = make_chunks()
        chunks[0] = Chunk(
            chunk_id=chunks[0].chunk_id,
            text="Meridian University admissions requirements",
            source_url="https://www.anitamedicalsystems.com/p-1.html",
        )
        with pytest.raises(CorpusError, match="sibling"):
            assert_corpus_is_anita(chunks)

    def test_empty_corpus_is_refused(self):
        with pytest.raises(CorpusError, match="empty"):
            assert_corpus_is_anita([])

    def test_fingerprint_is_stable_and_order_independent(self):
        chunks = make_chunks()
        assert corpus_fingerprint(chunks) == corpus_fingerprint(list(reversed(chunks)))

    def test_fingerprint_changes_with_content(self):
        a, b = make_chunks(), make_chunks()
        b[0] = Chunk(chunk_id=b[0].chunk_id, text="changed", content_hash="different")
        assert corpus_fingerprint(a) != corpus_fingerprint(b)

    def test_ddl_declares_the_build_table(self):
        assert "anita.index_builds" in INDEX_BUILDS_DDL
        assert "state" in INDEX_BUILDS_DDL


# ---------------------------------------------------------------------------
# Index lifecycle (SM-05)
# ---------------------------------------------------------------------------
class TestIndexLifecycle:
    def test_starts_absent(self):
        assert IndexLifecycle("anita_ams__v1").state is IndexState.ABSENT

    def test_full_happy_path(self):
        life = IndexLifecycle("anita_ams__v1")
        build = life.begin(embed_model="nomic-embed-text")
        assert build.state is IndexState.BUILDING
        assert life.state is IndexState.BUILDING  # not live until promoted

        life.built(chunks=139, dimensions=768, corpus_hash="abc")
        promoted = life.promote({"passed": True, "reason": "gate green"})

        assert promoted.state is IndexState.LIVE
        assert life.state is IndexState.LIVE
        assert life.live_collection == promoted.collection

    def test_collections_are_versioned(self):
        life = IndexLifecycle("anita_ams__v1")
        first = life.begin()
        second_build = life.begin()
        assert first.collection == "anita_ams__v1"
        assert second_build.collection == "anita_ams__v2"
        assert first.collection != second_build.collection

    def test_illegal_transition_is_refused(self):
        """BUILDING → LIVE directly is how an unvalidated index reaches buyers."""
        life = IndexLifecycle("anita_ams__v1")
        life.begin()
        with pytest.raises(RuntimeError, match="illegal transition"):
            life.promote({"passed": True})

    def test_failed_validation_is_not_promoted(self):
        life = IndexLifecycle("anita_ams__v1")
        life.begin()
        life.built(chunks=10, dimensions=768, corpus_hash="x")
        build = life.promote({"passed": False, "reason": "recall dropped to 0.71"})

        assert build.state is IndexState.REJECTED
        assert "recall" in build.error
        assert life.state is IndexState.ABSENT  # nothing went live

    def test_a_rejected_rebuild_leaves_the_previous_index_serving(self):
        """The property that makes it safe to refresh a catalogue on a running
        system: a failed rebuild is a no-op, not an outage."""
        life = IndexLifecycle("anita_ams__v1")
        life.begin()
        life.built(chunks=139, dimensions=768, corpus_hash="v1")
        good = life.promote({"passed": True, "reason": "ok"})
        assert life.live_collection == good.collection

        life.begin()                                    # v2
        life.built(chunks=139, dimensions=768, corpus_hash="v2")
        life.promote({"passed": False, "reason": "regression"})

        assert life.state is IndexState.LIVE
        assert life.live_collection == good.collection  # still v1

    def test_a_crash_mid_build_leaves_the_previous_index_serving(self):
        life = IndexLifecycle("anita_ams__v1")
        life.begin()
        life.built(chunks=139, dimensions=768, corpus_hash="v1")
        live = life.promote({"passed": True, "reason": "ok"})

        life.begin()          # v2 starts
        life.reject("worker died during embedding")

        assert life.live_collection == live.collection

    def test_promotion_supersedes_the_previous_build(self):
        life = IndexLifecycle("anita_ams__v1")
        life.begin()
        life.built(chunks=1, dimensions=768, corpus_hash="a")
        first = life.promote({"passed": True})

        life.begin()
        life.built(chunks=2, dimensions=768, corpus_hash="b")
        second = life.promote({"passed": True})

        assert life.live_collection == second.collection
        assert first.state is IndexState.REJECTED   # superseded, kept for history

    def test_version_continues_across_restarts(self):
        """A restart must not reuse a collection name that already holds a
        rejected build."""
        store = MemoryBuildStore()
        first = IndexLifecycle("anita_ams__v1", store)
        first.begin()
        first.built(chunks=1, dimensions=768, corpus_hash="a")
        first.promote({"passed": True})

        resumed = IndexLifecycle("anita_ams__v1", store)
        assert resumed.live_collection == "anita_ams__v1"      # remembers what is live
        assert resumed.begin().collection == "anita_ams__v2"   # and does not reuse

    def test_reject_with_no_build_raises(self):
        with pytest.raises(RuntimeError):
            IndexLifecycle("anita_ams__v1").reject("nothing to reject")


# ---------------------------------------------------------------------------
# Full build path
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not HAS_CORPUS, reason="corpus not built")
class TestBuildIndex:
    def _retriever(self, chunks):
        gate = GroundingGate.from_chunks(chunks, threshold=0.62)
        return Retriever(chunks, RecordingEmbedder(), InMemoryStore(), gate)

    def _chunks(self):
        from anita.retrieval import load_chunks

        return load_chunks(CORPUS_DIR / "chunks.jsonl")

    def test_build_promotes_a_valid_index(self):
        from anita.index import build_index

        chunks = self._chunks()
        life = IndexLifecycle("anita_ams__v1")
        build = build_index(life, chunks, retriever=self._retriever(chunks),
                            embed_model="nomic-embed-text", dimensions=768)
        assert build.state is IndexState.LIVE
        assert build.chunks == len(chunks)

    def test_the_embed_targets_the_collection_the_build_names(self):
        """US-010: a rebuild must write into the fresh staging collection it
        names, not the collection that is live while it runs.

        The defect only bit from the second rebuild onward — the first build's
        name coincides with the configured collection — which is why it looked
        correct in the fifth-pass evidence. This test fails on both rebuilds if
        the embed goes through the live retriever."""
        from anita.index import build_index

        chunks = self._chunks()
        embedded: list[str] = []

        class RecordingRetriever:
            """Stands in for a retriever that can address a collection."""

            def __init__(self, inner, collection):
                self.inner = inner
                self.collection = collection
                self.store = inner.store

            def for_collection(self, name):
                return RecordingRetriever(self.inner, name)

            def build_index(self):
                embedded.append(self.collection)
                return self.inner.build_index()

        life = IndexLifecycle("anita_ams__v1")
        live = RecordingRetriever(self._retriever(chunks), "anita_ams__v1")

        first = build_index(life, chunks, retriever=live)
        second = build_index(life, chunks, retriever=live)

        assert [first.collection, second.collection] == ["anita_ams__v1", "anita_ams__v2"]
        assert embedded == ["anita_ams__v1", "anita_ams__v2"], (
            "each build must embed into its own staging collection; embedding "
            "into the live one is the defect this pins"
        )
        assert live.collection == "anita_ams__v1", "the serving retriever is not retargeted"

    def test_a_retriever_that_cannot_retarget_is_used_as_is(self):
        """The in-memory backend has no collections to version: a retriever
        without `for_collection` must keep working exactly as before."""
        from anita.index import build_index

        chunks = self._chunks()
        life = IndexLifecycle("anita_ams__v1")
        build = build_index(life, chunks, retriever=self._retriever(chunks))
        assert build.state is IndexState.LIVE
        assert build.chunks == len(chunks)

    def test_failed_gate_blocks_promotion(self):
        from anita.index import build_index

        chunks = self._chunks()
        life = IndexLifecycle("anita_ams__v1")
        build = build_index(
            life, chunks, retriever=self._retriever(chunks),
            validate=lambda _: {"passed": False, "reason": "grounded-answer rate 91% < 98%"},
        )
        assert build.state is IndexState.REJECTED
        assert life.state is IndexState.ABSENT

    def test_embedding_failure_rejects_without_crashing(self):
        from anita.index import build_index

        chunks = self._chunks()
        life = IndexLifecycle("anita_ams__v1")
        failing = Retriever(
            chunks, RecordingEmbedder(fail_on="search_document:"), InMemoryStore(),
            GroundingGate.from_chunks(chunks, threshold=0.62),
        )
        build = build_index(life, chunks, retriever=failing)
        assert build.state is IndexState.REJECTED

    def test_wrong_corpus_is_rejected_before_embedding(self):
        """Guarding before embedding matters: building from the wrong corpus is
        expensive to undo, not just wrong."""
        from anita.index import build_index

        life = IndexLifecycle("anita_ams__v1")
        foreign = make_chunks(url="https://example.com/x.html")
        embedder = RecordingEmbedder()
        retriever = Retriever(foreign, embedder, InMemoryStore(),
                              GroundingGate(threshold=0.62))
        build = build_index(life, foreign, retriever=retriever)

        assert build.state is IndexState.REJECTED
        assert embedder.payloads == [], "must not embed a corpus it has already refused"
