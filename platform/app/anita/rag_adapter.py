"""MOD-03 adapter — ANITA's retrieval leg, delegated to `shared/rag`.

**This is the only module US-105 changes in live behaviour.** Everything else in
the story set is additive, which is why they carry no regression burden and this
one does.

What does *not* change here, deliberately:

* **The deterministic-first path.** `ChatSurface._answer` asks `AnitaAnswers`
  first; 8 of 10 questions are answered from SQL and never reach retrieval. This
  adapter is not in that path and does not alter it.
* **`GroundingGate`.** It stays in `anita.retrieval` and is applied *after*
  fusion, exactly as before. the source platform has no equivalent; moving it into
  shared code would export ANITA's grounding guarantee to a product that never
  had one (contract C4).
* **The fusion policy.** `shared.rag` defaults to the source's unweighted,
  text-keyed fusion. ANITA passes its own weights (0.65/0.35) and keeps its
  lexical-override rule — adopting the source policy unchanged would change
  ANITA's rankings (CV-09).

The duck-typing is deliberate and load-bearing. `release_gate.py`,
`system_check.py`, `trace_use_cases.py`, `validate_chat.py`, `rag_audit.py` and
the voice pipeline all reach into `.chunks`, `.by_id`, `.store.count()` and
`.gate`. Presenting the same surface means none of them change, so the swap is
reversible by configuration rather than by reverting code.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .retrieval import Chunk, GateDecision, RetrievalResult, ScoredChunk

logger = logging.getLogger("anita.rag_adapter")


class SharedRagRetriever:
    """A `Retriever`-shaped façade over `shared.rag.RagService`.

    Not a subclass: the point is that a caller holding either object cannot tell
    them apart, and inheritance would make the two drift together.
    """

    def __init__(
        self,
        service: Any,
        *,
        chunks: list[Chunk],
        embedder: Any,
        gate: Any,
        top_k: int = 5,
        fetch_k: int = 20,
    ) -> None:
        self.service = service
        self.chunks = list(chunks)
        self.by_id = {chunk.chunk_id: chunk for chunk in self.chunks}
        self.embedder = embedder
        self.gate = gate
        self.top_k = top_k
        self.fetch_k = fetch_k
        # `store` is exposed for `.store.count()` in the audit scripts. The
        # adapter is a thin shim, not a second cache: this proxies.
        self.store = service._vector_store()
        self._ensured = False

    def _ensure_index(self) -> None:
        """Build the index once if the collection is empty.

        Without this, a fresh deployment — or any caller that constructs the
        retriever without calling `build_index()` — silently served
        **sparse-only** results: the dense leg found an empty collection, the
        service degraded to BM25, and the answer still looked normal. Nothing
        signalled it. Measured: with an empty collection the two backends
        disagreed on which chunk to return for "what is the ultrasound gel used
        for", because one had a dense leg and the other did not.

        The pre-migration `Retriever` needed an explicit `build_index()` too, but
        callers had always done it, so the requirement was invisible rather than
        absent. Making the adapter self-building keeps the swap transparent: no
        caller has to learn a new step.
        """
        if self._ensured:
            return
        self._ensured = True
        try:
            if self.store.count() == 0:
                written = self.build_index()
                logger.info("built %d vectors into %s", written, self.store.name())
        except Exception as exc:  # noqa: BLE001 - a build failure degrades, loudly
            logger.warning("could not build the vector index (%s); "
                           "retrieval will be sparse-only", exc)

    @property
    def collection(self) -> str:
        """The collection this retriever reads and writes."""
        return str(getattr(self.service.config, "collection_name", ""))

    def for_collection(self, collection: str) -> "SharedRagRetriever":
        """A sibling bound to another collection, sharing everything else.

        US-010: a rebuild must embed into — and validate against — the *staging*
        collection it is about to promote, not the collection that is live while
        it runs. Retargeting this object would be a lie: `RagService` caches the
        vector store it opens, and the serving process is using it. A sibling
        over a `RagConfig` whose `collection_name` is the staging name is the
        only honest way to point the write somewhere else.

        The chunks, embedder, gate and fusion policy are shared by reference —
        only the collection differs, so a staging retriever cannot accidentally
        answer from a different corpus or with a different threshold.
        """
        from dataclasses import replace

        from shared.rag import RagService

        config = replace(self.service.config, collection_name=collection)
        service = RagService(config, embedder=self.embedder)
        return SharedRagRetriever(
            service,
            chunks=self.chunks,
            embedder=self.embedder,
            gate=self.gate,
            top_k=self.top_k,
            fetch_k=self.fetch_k,
        )

    # -- indexing ----------------------------------------------------------
    def build_index(self, *, batch_size: int = 32) -> int:
        """Embed and persist. Returns the number of vectors written.

        The name matches `Retriever.build_index` because callers use it; the
        work is `RagService.build()`.
        """
        return self.service.build()

    def build(self) -> "SharedRagRetriever":
        """Chainable, matching `Retriever.build()`."""
        self.build_index()
        return self

    # -- querying ----------------------------------------------------------
    def retrieve(self, query: str, *, top_k: int | None = None,
                 fetch_k: int | None = None) -> RetrievalResult:
        """Retrieve, gate, and return — or decline.

        The gate runs **after** the shared service returns chunks, and its
        decision is unchanged from the pre-migration path. A gate refusal
        returns no chunks, so nothing downstream can compose an answer from
        context the gate rejected.
        """
        self._ensure_index()
        limit = top_k or self.top_k
        shared = self.service.retrieve(query)

        # Sparse-only results carry no similarity. The gate's similarity rule
        # needs one, so a sparse-only result is evaluated against 0.0 and can
        # still pass on an entity match — the same behaviour as before, where
        # an empty dense leg produced the same 0.0.
        decision = self.gate.evaluate(query, shared.top_dense_sim)
        if decision.not_relevant:
            return RetrievalResult(decision=decision, chunks=[], query=query)

        scored: list[ScoredChunk] = []
        for hit in shared.chunks[:limit]:
            chunk = self.by_id.get(hit.chunk_id)
            if chunk is None:
                # A vector in the store with no chunk in the corpus means the
                # index is ahead of the corpus — possible during a rebuild, and
                # skipping is the safe reading: we cannot cite what we cannot
                # name.
                logger.warning("retrieved chunk %s is not in the corpus", hit.chunk_id)
                continue
            scored.append(ScoredChunk(
                chunk=chunk,
                fused_score=hit.score,
                dense_score=hit.dense_score,
                sparse_score=hit.sparse_score,
            ))

        if not scored:
            return RetrievalResult(
                decision=GateDecision(False, "no_citable_chunks", shared.top_dense_sim),
                chunks=[], query=query,
            )
        return RetrievalResult(decision=decision, chunks=scored, query=query)

    # -- diagnostics -------------------------------------------------------
    def status(self) -> dict:
        return self.service.status()


def build_shared_retriever(
    *,
    chunks: list[Chunk],
    embedder: Any,
    gate: Any,
    corpus_dir: Path,
    collection: str,
    persist_path: Path,
    expected_markers: tuple[str, ...],
    blocked_markers: tuple[str, ...],
    top_k: int = 5,
    fetch_k: int = 20,
) -> SharedRagRetriever:
    """Construct the adapter over ANITA's corpus and config.

    ANITA's corpus is JSONL, so the shared chunker's pass-through path is used
    and `chunk_size`/`chunk_overlap` are inert — stated here because a reader
    who assumes they apply would expect 141 chunks to become some other number.
    """
    from shared.rag import RagConfig, RagService

    config = RagConfig(
        collection_name=collection,
        persist_path=persist_path,
        embed_model=str(getattr(embedder, "model", "unknown")),
        sources=(Path(corpus_dir) / "chunks.jsonl",),
        expected_markers=expected_markers,
        blocked_markers=blocked_markers,
        # The system prompt is unused on this path: ANITA answers from the
        # deterministic engine or returns the retrieved chunk verbatim, and
        # never composes a prompt from context. It is still required by the
        # contract, so it carries ANITA's own refusal wording rather than a
        # placeholder.
        system_prompt=(
            "Answer only from the catalogue context below.\n{context}"
        ),
        refusal_text="I don't have that in our catalogue.",
        top_k=top_k,
        fetch_k=fetch_k,
        # ANITA's fusion policy, not the source's (CV-09).
        search_mode="hybrid",
        dense_weight=0.65,
        sparse_weight=0.35,
        rrf_k=60,
    )
    service = RagService(config, embedder=embedder)
    return SharedRagRetriever(
        service, chunks=chunks, embedder=embedder, gate=gate,
        top_k=top_k, fetch_k=fetch_k,
    )


__all__ = ["SharedRagRetriever", "build_shared_retriever"]
