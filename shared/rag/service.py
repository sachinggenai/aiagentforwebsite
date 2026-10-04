"""RagService — the facade callers import.

Holds no product knowledge. Every value it uses arrives through ``RagConfig``;
the embedder and the optional primary client arrive through constructor seams.
Two products can therefore run this package in one process with different
corpora and no shared state.

The transport for the primary rung is **not implemented here** (contract C7 —
the external ERC service is out of scope). The seam exists so a future change
can inject one without touching this file, and ``status()`` reports the
degradation explicitly rather than leaving a mechanism that looks present and
can never fire.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from .breaker import CircuitBreaker
from .chunking import SourceChunk, load_sources, validate_corpus
from .config import RagConfig
from .errors import EmbeddingUnavailable, VectorStoreUnavailable
from .fusion import MiniBM25, lexical_override, weighted_rrf
from .store import ChromaVectorStore, InMemoryVectorStore, VectorStore

RUNG_MCP = "mcp"
RUNG_LOCAL = "local"
RUNG_NONE = "none"


class Embedder(Protocol):
    """Injected embedding client.

    A Protocol rather than an implementation, because the two products embed
    differently — ANITA calls Ollama with task-specific prefixes, universityDemo
    goes through LangChain — and neither belongs in a shared package.
    """

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class McpClient(Protocol):
    """The optional primary-retrieval seam. NO implementation ships here.

    The contract lists it in the constructor (C3.1) and this package keeps the
    seam so a future change can inject a client without a signature change.
    Nothing may implement it over HTTP in this change (contract C7).
    """

    def retrieve(self, query: str, top_k: int) -> list["RetrievedChunk"]: ...


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    text: str
    score: float = 0.0
    dense_score: float = 0.0
    sparse_score: float = 0.0
    section: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalResult:
    query: str
    chunks: tuple[RetrievedChunk, ...]
    rung: str
    breaker: str
    degraded: bool = False
    top_dense_sim: float = 0.0
    collection: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.chunks)

    def context(self, max_chars: int | None = None) -> str:
        """``[§ section]\\n text`` joined by a rule, whole chunks only.

        Truncation is by whole chunk: half a specification row in a prompt is
        worse than one fewer row, because a model completes the pattern. When
        the *first* chunk alone exceeds the budget it is cut rather than
        included whole — the budget is a limit, and an unenforced one is not a
        budget.
        """
        parts: list[str] = []
        used = 0
        for chunk in self.chunks:
            block = f"[§ {chunk.section or chunk.chunk_id}]\n{chunk.text}"
            if max_chars is not None and used + len(block) > max_chars:
                if parts:
                    break
                block = block[:max_chars].rsplit(" ", 1)[0]
            parts.append(block)
            used += len(block)
        return "\n\n---\n\n".join(parts)


class RagService:
    """Build, retrieve, report. No I/O until ``build`` or ``retrieve``."""

    def __init__(self, config: RagConfig, *, mcp: McpClient | None = None,
                 embedder: Embedder | None = None,
                 store: VectorStore | None = None) -> None:
        # Construction opens nothing and mutates no global. A misconfiguration
        # has already been refused by RagConfig.__post_init__, before this runs.
        self.config = config
        self.mcp = mcp
        self.embedder = embedder
        self._store = store
        self._chunks: list[SourceChunk] = []
        # One BM25 per service instance, built from this service's own chunks.
        # The source cached by the literal "bm25" regardless of collection, so a
        # collection switch served a stale corpus index (defect B-6).
        self._bm25: MiniBM25 | None = None
        self._bm25_key: tuple[str, str] | None = None
        # `off` means "do not use the primary", which is the same situation as
        # having no primary at all. Gating on `mcp is not None` alone made
        # `mode="off"` call the primary anyway — the setting was read, reported
        # in status(), and then ignored.
        self._primary_enabled = mcp is not None and config.normalised_mode != "off"
        self._breaker = CircuitBreaker(
            enabled=self._primary_enabled,
            failure_threshold=config.failure_threshold,
            cooldown_s=config.cooldown_s,
            read_timeout_s=config.read_timeout_s,
            probe_fraction=config.probe_fraction,
            probe_floor_s=config.probe_floor_s,
        )
        self._last_rung: str | None = None
        self._degraded = False

    # -- store -------------------------------------------------------------
    def _vector_store(self) -> VectorStore:
        if self._store is None:
            self._store = ChromaVectorStore(
                self.config.persist_path,
                self.config.collection_name,
                distance_space=self.config.distance_space,
            )
        return self._store

    # -- build -------------------------------------------------------------
    def build(self) -> int:
        """Load, gate, embed and write. Returns the number of vectors written.

        The corpus gate runs **before** anything is embedded or written: a
        corpus that is not this product's must not reach a collection, because
        a mixed index answers confidently from another product's data.
        """
        if self.embedder is None:
            raise EmbeddingUnavailable(
                "no embedder injected — pass embedder= to RagService"
            )
        chunks = load_sources(
            self.config.sources,
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
        )
        validate_corpus(
            chunks,
            expected_markers=self.config.expected_markers,
            blocked_markers=self.config.blocked_markers,
        )
        self._chunks = chunks

        store = self._vector_store()
        batch = self.config.embed_batch_size
        written = 0
        for start in range(0, len(chunks), batch):
            window = chunks[start:start + batch]
            try:
                vectors = self.embedder.embed([c.text for c in window])
            except Exception as exc:  # noqa: BLE001 - surface as our own type
                raise EmbeddingUnavailable(str(exc)) from exc
            if len(vectors) != len(window):
                raise EmbeddingUnavailable(
                    f"embedder returned {len(vectors)} vectors for {len(window)} texts"
                )
            store.upsert(
                ids=[c.chunk_id for c in window],
                vectors=vectors,
                metadatas=[{**dict(c.metadata), "section": c.section,
                            "source_file": c.source_file} for c in window],
            )
            written += len(window)

        self._bm25 = None  # force a rebuild against the new corpus
        self._bm25_key = None
        return written

    def _ensure_chunks(self) -> None:
        """Load the corpus's chunks if this process has not built them.

        A warm restart finds vectors already in the store but an empty
        ``_chunks``, because ``__init__`` performs no I/O and ``build()`` was not
        called. Retrieval then returned nothing: the store had 141 vectors and
        the service had no text or metadata to turn a hit into a result — a
        persisted index that could not be read.

        Reading the sources costs no embedding — that is the whole point of
        persistence — so it is done lazily here rather than eagerly in
        ``__init__``, which the contract requires to stay I/O-free.
        """
        if self._chunks:
            return
        try:
            self._chunks = load_sources(
                self.config.sources,
                chunk_size=self.config.chunk_size,
                chunk_overlap=self.config.chunk_overlap,
            )
        except Exception:  # noqa: BLE001 - a corpus that cannot be read degrades, loudly
            self._chunks = []
            self._degraded = True

    def _sparse_index(self) -> MiniBM25 | None:
        """BM25 over this service's chunks, rebuilt when the corpus changes."""
        key = (str(self.config.persist_path), self.config.collection_name)
        if self._bm25 is None or self._bm25_key != key:
            self._ensure_chunks()
            if not self._chunks:
                return None
            self._bm25 = MiniBM25([c.text for c in self._chunks])
            self._bm25_key = key
        return self._bm25

    # -- retrieve ----------------------------------------------------------
    def retrieve(self, query: str) -> RetrievalResult:
        """Primary if admitted, else local, else none — rung recorded always."""
        primary = self._try_primary(query)
        if primary is not None:
            self._last_rung = RUNG_MCP
            return RetrievalResult(
                query=query, chunks=tuple(primary), rung=RUNG_MCP,
                breaker=self._breaker.state, collection=self.config.collection_name,
            )
        return self._retrieve_local(query)

    def _try_primary(self, query: str) -> list[RetrievedChunk] | None:
        """The optional first rung. Absent by design in this change."""
        if not self._primary_enabled or self.mcp is None or not self._breaker.admit():
            return None
        try:
            chunks = list(self.mcp.retrieve(query, self.config.top_k))
        except Exception as exc:  # noqa: BLE001 - a failed primary is not fatal
            self._breaker.record_failure(str(exc))
            return None
        self._breaker.record_success()
        return chunks

    def _retrieve_local(self, query: str) -> RetrievalResult:
        """Dense + sparse, fused, with the lexical override applied."""
        self._ensure_chunks()
        dense_rank: list[int] = []
        dense_scores: dict[int, float] = {}
        try:
            # The store is opened on demand. Guarding on `self._store is not
            # None` meant a warm restart — where the store exists on disk but
            # has not been opened in this process — silently skipped the dense
            # leg entirely.
            store = self._vector_store() if self.embedder is not None else None
            if store is not None and store.count() > 0:
                vector = self.embedder.embed([query])[0]
                hits = store.query(vector, self.config.fetch_k)
                positions = {c.chunk_id: i for i, c in enumerate(self._chunks)}
                for chunk_id, score in hits:
                    if chunk_id in positions:
                        at = positions[chunk_id]
                        dense_rank.append(at)
                        dense_scores[at] = score
        except (VectorStoreUnavailable, EmbeddingUnavailable):
            # Dense unavailable is a degradation, not a failure: the sparse leg
            # still answers. Returning nothing would make a working system look
            # broken. The result says so via degraded=True.
            self._degraded = True
        except Exception:  # noqa: BLE001
            self._degraded = True

        sparse_hits: list[tuple[int, float]] = []
        sparse_scores: dict[int, float] = {}
        index = self._sparse_index()
        if index is not None:
            sparse_hits = index.search(query, top_k=self.config.fetch_k)
            sparse_scores = dict(sparse_hits)

        if not dense_rank and not sparse_hits:
            self._last_rung = RUNG_NONE
            return RetrievalResult(
                query=query, chunks=(), rung=RUNG_NONE, breaker=self._breaker.state,
                degraded=self._degraded, collection=self.config.collection_name,
            )

        if self.config.search_mode == "dense" and dense_rank:
            order = dense_rank[: self.config.top_k]
        else:
            fused = weighted_rrf(
                dense_rank, [i for i, _ in sparse_hits],
                k=self.config.rrf_k,
                dense_weight=self.config.dense_weight,
                sparse_weight=self.config.sparse_weight,
            )
            ordered = sorted(fused.keys(), key=lambda i: fused[i], reverse=True)
            ordered = lexical_override(ordered, sparse_hits)
            order = ordered[: self.config.top_k]

        chunks = tuple(
            RetrievedChunk(
                chunk_id=self._chunks[i].chunk_id,
                text=self._chunks[i].text,
                score=round(dense_scores.get(i, 0.0) * self.config.dense_weight
                            + sparse_scores.get(i, 0.0) * self.config.sparse_weight, 6),
                dense_score=dense_scores.get(i, 0.0),
                sparse_score=sparse_scores.get(i, 0.0),
                section=self._chunks[i].section,
                metadata=dict(self._chunks[i].metadata),
            )
            for i in order
            if 0 <= i < len(self._chunks)
        )
        self._last_rung = RUNG_LOCAL
        return RetrievalResult(
            query=query, chunks=chunks, rung=RUNG_LOCAL, breaker=self._breaker.state,
            degraded=self._degraded,
            top_dense_sim=max(dense_scores.values(), default=0.0),
            collection=self.config.collection_name,
        )

    # -- status ------------------------------------------------------------
    def status(self) -> dict:
        """Everything an operator needs, with the degradation made explicit.

        ``mode_effective`` is the honest field: ``mode`` is what was configured,
        but with no primary injected no primary can be tried, so the effective
        mode is ``off`` and the breaker reports ``"off"``. Visible, not silent.
        """
        try:
            vectors = self._vector_store().count()
        except Exception:  # noqa: BLE001 - status must never raise
            vectors = 0
        snapshot = self._breaker.snapshot()
        return {
            "collection": self.config.collection_name,
            "persist_path": str(self.config.persist_path),
            "embed_model": self.config.embed_model,
            "vectors": vectors,
            "chunks_loaded": len(self._chunks),
            "rung": self._last_rung,
            **snapshot.as_dict(),
            "failure_threshold": self.config.failure_threshold,
            "mode_configured": self.config.normalised_mode,
            "mode_effective": self.config.normalised_mode if self._primary_enabled else "off",
            "degraded": self._degraded,
        }


__all__ = ["RagService", "RetrievalResult", "RetrievedChunk", "Embedder", "McpClient",
           "RUNG_MCP", "RUNG_LOCAL", "RUNG_NONE"]
