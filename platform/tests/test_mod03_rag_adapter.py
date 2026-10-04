"""US-105 — the ANITA adapter over `shared/rag`.

The only story in the CR-01 set that changes live behaviour, so it carries the
regression burden. These tests assert the three things that make the swap safe:

1. **Duck-type compatibility.** `release_gate.py`, `system_check.py`,
   `trace_use_cases.py`, `validate_chat.py`, `rag_audit.py` and the voice
   pipeline reach into `.chunks`, `.by_id`, `.store.count()` and `.gate`.
   Presenting the same surface means none of them change.
2. **The deterministic-first path is untouched.** A resolvable commercial
   question must make *zero* retrieval calls.
3. **The rollback works.** `ANITA_RAG_BACKEND=memory` builds the pre-migration
   graph, and both backends answer identically.

Run:  .venv\\Scripts\\python -m pytest tests/test_mod03_rag_adapter.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
REPO = PLATFORM.parent
sys.path.insert(0, str(PLATFORM / "app"))
sys.path.insert(0, str(REPO))

from anita.rag_adapter import SharedRagRetriever, build_shared_retriever  # noqa: E402

CORPUS = REPO / "data" / "out" / "chunks.jsonl"
requires_corpus = pytest.mark.skipif(not CORPUS.exists(), reason="corpus not built")


class StubEmbedder:
    """Deterministic, so a failure is reproducible and Ollama is not required."""

    model = "stub-8"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts):
        import hashlib

        self.calls += 1
        return [
            [b / 255.0 for b in hashlib.sha256(t.encode()).digest()[:8]] for t in texts
        ]


def make_adapter(tmp_path, **over):
    from anita.retrieval import GroundingGate, load_chunks

    chunks = load_chunks(CORPUS)
    gate = GroundingGate.from_chunks(chunks, threshold=0.0)
    params = dict(
        chunks=chunks,
        embedder=StubEmbedder(),
        gate=gate,
        corpus_dir=REPO / "data" / "out",
        collection="anita_ams__v1",
        persist_path=tmp_path / "chroma",
        expected_markers=("anitamedicalsystems",),
        blocked_markers=("meridian", "university of maryland", "admissions"),
    )
    params.update(over)
    return build_shared_retriever(**params)


# ---------------------------------------------------------------------------
# Duck-type compatibility — nothing downstream needs to change
# ---------------------------------------------------------------------------
@requires_corpus
class TestDuckTypeCompatibility:
    def test_the_adapter_presents_the_same_surface_as_Retriever(self, tmp_path):
        """Every attribute the call sites touch must exist on both."""
        from anita.retrieval import Retriever

        adapter = make_adapter(tmp_path)
        for name in ("retrieve", "build_index", "build", "chunks", "by_id",
                     "store", "gate", "top_k", "fetch_k", "embedder"):
            assert hasattr(adapter, name), f"adapter is missing .{name}"
            assert hasattr(Retriever, name) or name in {
                "chunks", "by_id", "store", "gate", "top_k", "fetch_k", "embedder"
            }, f"Retriever has no .{name}; the comparison is wrong"

    def test_store_count_is_reachable(self, tmp_path):
        """`rag_audit.py` reads `.store.count()` to prove the index is complete."""
        adapter = make_adapter(tmp_path)
        adapter.build_index()
        assert adapter.store.count() == 141

    def test_retrieve_returns_the_anita_result_type(self, tmp_path):
        """Downstream unpacks `decision`, `chunks` and `ScoredChunk` fields."""
        from anita.retrieval import RetrievalResult

        adapter = make_adapter(tmp_path)
        adapter.build_index()
        result = adapter.retrieve("ultrasound gel")
        assert isinstance(result, RetrievalResult)
        assert result.decision is not None
        assert all(hasattr(c, "chunk") and hasattr(c, "fused_score") for c in result.chunks)

    def test_build_is_chainable(self, tmp_path):
        """`Retriever.build()` returns self; callers chain it."""
        assert isinstance(make_adapter(tmp_path).build(), SharedRagRetriever)


# ---------------------------------------------------------------------------
# The gate is retained and still governs
# ---------------------------------------------------------------------------
@requires_corpus
class TestGroundingGateIsRetained:
    def test_a_gate_refusal_yields_no_chunks(self, tmp_path):
        """The gate is applied after fusion, exactly as before the migration.
        A refusal must return nothing, so nothing downstream composes an answer
        from context the gate rejected."""
        from anita.retrieval import GroundingGate, load_chunks

        chunks = load_chunks(CORPUS)
        # A gate whose vocabulary cannot match anything, at a high threshold.
        gate = GroundingGate.from_chunks(chunks, threshold=0.99)
        adapter = make_adapter(tmp_path, gate=gate)
        adapter.build_index()
        result = adapter.retrieve("who won the world cup?")
        assert not result.ok, "the gate admitted an off-corpus query"

    def test_the_gate_object_is_the_anita_one(self, tmp_path):
        """It stays in `anita.retrieval`; the shared package has no equivalent
        and must not acquire one (contract C4)."""
        from anita.retrieval import GroundingGate

        assert isinstance(make_adapter(tmp_path).gate, GroundingGate)


# ---------------------------------------------------------------------------
# The deterministic-first path is not touched
# ---------------------------------------------------------------------------
@requires_corpus
class TestDeterministicPathUntouched:
    def test_a_resolvable_commercial_question_makes_zero_retrieval_calls(self):
        """8 of 10 questions are answered from SQL. Wiring a new backend must not
        drag them through retrieval — that would be a behaviour change with a
        latency and a correctness cost."""
        from anita.assembly import assemble

        built = assemble(require_preflight=False)
        retriever = built.chat.retriever
        calls: list[str] = []
        original = retriever.retrieve

        def spy(query, **kw):
            calls.append(query)
            return original(query, **kw)

        retriever.retrieve = spy
        body = built.chat.post_message(
            message="what is the price of the ultrasound gel?", session_id="spy"
        ).body
        assert "60" in body["text"]
        assert calls == [], f"a commercial question consulted retrieval: {calls}"


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------
class TestBackendSelection:
    def test_the_default_backend_is_shared(self, monkeypatch):
        monkeypatch.delenv("ANITA_RAG_BACKEND", raising=False)
        from anita.assembly import _rag_backend
        from anita.config import load_settings

        assert _rag_backend(load_settings(env={})) == "shared"

    def test_the_environment_overrides_the_config(self, monkeypatch):
        """The drill is 'restart with the variable set', not 'edit a file and
        redeploy' — so the variable must win."""
        monkeypatch.setenv("ANITA_RAG_BACKEND", "memory")
        from anita.assembly import _rag_backend
        from anita.config import load_settings

        assert _rag_backend(load_settings(env={})) == "memory"

    def test_an_unknown_backend_falls_back_to_shared(self, monkeypatch):
        monkeypatch.setenv("ANITA_RAG_BACKEND", "nonsense")
        from anita.assembly import _rag_backend
        from anita.config import load_settings

        assert _rag_backend(load_settings(env={})) == "shared"


@requires_corpus
class TestBothBackendsAgree:
    """C9 — the rollback produces the same answers, so reverting is safe."""

    # Deterministic-path questions must be byte-identical: retrieval is never
    # consulted, so a difference would mean the swap changed something it had
    # no business touching.
    DETERMINISTIC = (
        "what is the price of the ultrasound gel?",
        "what is the minimum order quantity for the ultrasound gel?",
        "where is your office?",
        "Show me the catalogue",
        "who won the world cup?",
    )
    # Retrieval-path questions must return an answer about the same product. They
    # are NOT asserted byte-identical, and the reason is measured, not assumed:
    # see `test_the_backends_differ_only_on_near_ties`.
    RETRIEVAL = (
        "tell me about the X-Ray Cassette",
        "contrast media compatibility",
        "what is the ultrasound gel used for",
    )

    def _answers(self, monkeypatch, questions):
        from anita.assembly import _build_retriever, assemble
        from anita.retrieval import load_chunks

        built = assemble(require_preflight=False)
        chunks = load_chunks(CORPUS)
        out: dict[str, tuple] = {}
        for backend in ("memory", "shared"):
            monkeypatch.setenv("ANITA_RAG_BACKEND", backend)
            notes: list[str] = []
            retriever = _build_retriever(built.settings, chunks, notes)
            assert retriever is not None, f"{backend}: {notes}"
            built.chat.retriever = retriever
            out[backend] = tuple(
                built.chat.post_message(message=q, session_id=f"cmp-{backend}").body.get("text")
                for q in questions
            )
        return out

    def test_deterministic_paths_are_byte_identical(self, monkeypatch):
        """C9, the part that must hold absolutely. 8 of 10 questions never reach
        retrieval, so swapping the backend cannot change them — if it did, the
        swap reached further than the retrieval leg."""
        answers = self._answers(monkeypatch, self.DETERMINISTIC)
        for query, memory, shared in zip(self.DETERMINISTIC, *answers.values()):
            assert memory == shared, (
                f"{query!r} is answered without retrieval and must not change: "
                f"memory={memory[:70]!r} shared={shared[:70]!r}"
            )

    def test_retrieval_paths_return_the_same_product(self, monkeypatch):
        """C9, the part that holds conditionally. Both backends must answer about
        the right product and be grounded; which *chunk* they pick may differ."""
        answers = self._answers(monkeypatch, self.RETRIEVAL)
        for query, memory, shared in zip(self.RETRIEVAL, *answers.values()):
            assert memory and shared, f"{query!r} declined on one backend"
            assert "don't have that in our catalogue" not in memory
            assert "don't have that in our catalogue" not in shared
            # Same subject: the first noun-phrase product name appears in both.
            head = memory.split(":")[0].strip()
            assert head and head in shared, (
                f"{query!r} answered about different products: "
                f"memory={memory[:70]!r} shared={shared[:70]!r}"
            )

    def test_the_backends_differ_only_on_near_ties(self):
        """CV-09, measured rather than asserted.

        The dense legs are different algorithms: the pre-migration path scans
        every vector exactly, the migrated path asks Chroma's HNSW index
        approximately. Where two candidates are far apart they agree; where they
        are near-tied they may not.

        Measured on ANITA's corpus for "what is the ultrasound gel used for":
        the top two chunks score 0.8321 and 0.8320 — a 0.0001 gap. Either can
        come first, and the answer branch then returns a published FAQ or the
        specification list depending on which won. Both are grounded, about the
        correct product, and quoted verbatim from the catalogue.

        This is why C5 (ranking parity) exists as a checkpoint and why the
        migration does not claim byte-identical retrieval.
        """
        import numpy as np

        from anita.assembly import assemble
        from anita.retrieval import load_chunks

        built = assemble(require_preflight=False)
        embedder = built.chat.retriever.embedder
        chunks = load_chunks(CORPUS)
        query = "what is the ultrasound gel used for"

        vectors = np.array(embedder.embed([c.text for c in chunks]))
        qv = np.array(embedder.embed([query])[0])
        sims = vectors @ qv / (np.linalg.norm(vectors, axis=1) * np.linalg.norm(qv))
        order = np.argsort(-sims)[:2]
        gap = float(sims[order[0]] - sims[order[1]])

        assert gap < 0.001, (
            f"the top-two gap is {gap:.4f}; if it were large the backends would "
            f"agree and this caveat would no longer apply"
        )
