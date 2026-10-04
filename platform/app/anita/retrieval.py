"""Retrieval and the grounding gate (TRD-12 … TRD-16).

The design constraint that shapes this whole module: **a 169-chunk corpus is
not a scale problem, it is a correctness problem.** Everything here is sized for
a corpus that fits in memory many times over, so the effort goes into refusing
to answer rather than into ranking faster.

Three pieces:

* :class:`MiniBM25` — sparse retrieval. Written in plain Python because pulling
  a search engine in for 169 documents would be 40 MB of dependency to replace
  60 lines of arithmetic.
* :func:`weighted_rrf` — fusion. Dense retrieval is good at paraphrase and bad
  at part numbers; sparse is the reverse. A catalogue is full of part numbers
  ("APO-200", "FCR Prima T2"), which is why the sparse leg carries real weight
  rather than being a tie-breaker.
* :class:`GroundingGate` — **the point of the module.** It decides whether the
  retrieved context is good enough to answer from, and returns an explicit
  ``not_relevant`` when it is not. `BRD-04` requires the system to decline
  rather than improvise; this is the mechanism that makes that enforceable
  instead of aspirational.

The gate exists as its own class, called *before* the answer is composed, so
"no context, no answer" is a structural property rather than a prompt
instruction a model may or may not follow.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol, Sequence

# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Chunk:
    """One retrievable unit, as produced by the scraper."""

    chunk_id: str
    text: str
    context_prefix: str = ""
    chunk_type: str = ""
    product_id: str | None = None
    product_name: str | None = None
    category: str | None = None
    source_url: str | None = None
    token_estimate: int = 0
    content_hash: str = ""

    @property
    def embed_text(self) -> str:
        """What gets embedded. Carries the context prefix so a bare "About"
        fragment is not embedded without the product it belongs to."""
        return f"{self.context_prefix}\n\n{self.text}" if self.context_prefix else self.text

    @classmethod
    def from_dict(cls, data: dict) -> "Chunk":
        return cls(
            chunk_id=data["chunk_id"],
            text=data.get("text", ""),
            context_prefix=data.get("context_prefix", "") or "",
            chunk_type=data.get("chunk_type", ""),
            product_id=data.get("product_id"),
            product_name=data.get("product_name"),
            category=data.get("category"),
            source_url=data.get("source_url"),
            token_estimate=data.get("token_estimate", 0) or 0,
            content_hash=data.get("content_hash", ""),
        )


def load_chunks(path: Path | str) -> list[Chunk]:
    path = Path(path)
    chunks: list[Chunk] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                chunks.append(Chunk.from_dict(json.loads(line)))
    return chunks


# ---------------------------------------------------------------------------
# Sparse retrieval
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")
# Words that appear in almost every chunk and carry no signal here. Deliberately
# short: an aggressive stop-list would strip "condition", "power" and other
# words that are genuine specification names in this catalogue.
_STOPWORDS = frozenset({"the", "a", "an", "of", "for", "is", "are", "and", "or", "to", "in", "on"})


def tokenize(text: str) -> list[str]:
    """Lowercase tokens, with part numbers normalised to a canonical form.

    ``APO-200`` and ``FCR Prima T2`` are exactly the queries where sparse
    retrieval beats dense — but the catalogue is inconsistent about how it
    writes them. The product is named "…Pressure Injector APO 200" (a space)
    while buyers and spec sheets write "APO-200" (a hyphen). Tokenised naively
    those become ``['apo', '200']`` and ``['apo-200']``, which never match.

    So every alpha/digit boundary is *also* emitted in a compact form:
    ``APO-200`` → ``apo200`` and ``APO 200`` → ``apo200``. Both sides converge
    on the same token and the part number becomes findable.
    """
    raw = _TOKEN_RE.findall((text or "").lower())
    tokens = [token for token in raw if token not in _STOPWORDS and len(token) > 1]

    extra: list[str] = []
    for token in tokens:
        # "apo-200" / "apo_200" / "apo.200" -> "apo200"
        compact = re.sub(r"[-_.]", "", token)
        if compact != token and any(ch.isdigit() for ch in compact):
            extra.append(compact)

    # Adjacent alpha+digit pairs: "apo" + "200" -> "apo200"
    for left, right in zip(raw, raw[1:]):
        if left.isalpha() and right.isdigit() and len(left) > 1:
            extra.append(f"{left}{right}")

    return tokens + extra


def _looks_like_noise(term: str) -> bool:
    """True for number-dominant tokens that would match far too much.

    Without this filter the catalogue's statutory data floods the distinctive
    set with fragments like ``03-02-199420`` (a drug-licence date), amounts, and
    ``07aaaca3337r1zf`` (a GSTIN). A query containing any of those would be
    judged "relevant" for the wrong reason, and the gate would open on a
    question about last year's tax filing.
    """
    digits = sum(ch.isdigit() for ch in term)
    if digits == 0:
        return False                       # pure words are never noise
    return digits / len(term) > 0.5        # number-dominant: dates, prices, codes


class MiniBM25:
    """BM25 Okapi in ~60 lines, sized for a small resident corpus.

    Not a general-purpose search engine: no compression, no skip lists, no
    incremental updates. It holds the token counts in memory and scores exactly
    — which for 169 chunks is both simpler and faster than the alternative.
    """

    def __init__(self, documents: Sequence[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.doc_tokens: list[list[str]] = [tokenize(doc) for doc in documents]
        self.doc_len: list[int] = [len(tokens) for tokens in self.doc_tokens]
        self.avg_len: float = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 0.0

        self.term_freqs: list[Counter[str]] = [Counter(tokens) for tokens in self.doc_tokens]
        self.doc_freq: Counter[str] = Counter()
        for tokens in self.doc_tokens:
            self.doc_freq.update(set(tokens))

        total = len(self.doc_tokens)
        # BM25 idf with the +0.5 smoothing; floored at a small positive value so
        # a term present in every document contributes nothing rather than a
        # negative score (which would rank a match *below* a non-match).
        self.idf: dict[str, float] = {
            term: max(
                0.0,
                math.log(1.0 + (total - freq + 0.5) / (freq + 0.5)),
            )
            for term, freq in self.doc_freq.items()
        }

    def search(self, query: str, top_k: int = 20) -> list[tuple[int, float]]:
        """Return ``[(doc_index, score)]``, highest first, zeros dropped."""
        terms = tokenize(query)
        if not terms:
            return []

        scores: list[tuple[int, float]] = []
        for index, freqs in enumerate(self.term_freqs):
            length = self.doc_len[index] or 1
            score = 0.0
            for term in terms:
                frequency = freqs.get(term)
                if not frequency:
                    continue
                idf = self.idf.get(term, 0.0)
                denominator = frequency + self.k1 * (
                    1 - self.b + self.b * (length / (self.avg_len or 1))
                )
                score += idf * (frequency * (self.k1 + 1)) / denominator
            if score > 0:
                scores.append((index, score))

        scores.sort(key=lambda pair: pair[1], reverse=True)
        return scores[:top_k]


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------
RRF_K = 60
DENSE_WEIGHT = 0.65
SPARSE_WEIGHT = 0.35


def weighted_rrf(
    dense: Sequence[int],
    sparse: Sequence[int],
    *,
    k: int = RRF_K,
    w_dense: float = DENSE_WEIGHT,
    w_sparse: float = SPARSE_WEIGHT,
) -> dict[int, float]:
    """Weighted Reciprocal Rank Fusion of two ranked index lists.

    Fusion is by **rank**, not score, which is what lets a cosine similarity and
    a BM25 score be combined at all — they are not on comparable scales and
    normalising them is a guessing game.

    Weights are 0.65 dense / 0.35 sparse rather than the more common 0.5/0.5.
    Dense handles the paraphrase-heavy questions ("what's the price of the gel"),
    but this catalogue is dense with part numbers, and a buyer who types
    "APO-200" must not lose to a chunk that merely talks about injectors.
    """
    fused: dict[int, float] = {}
    for rank, index in enumerate(dense):
        fused[index] = fused.get(index, 0.0) + w_dense / (k + rank + 1)
    for rank, index in enumerate(sparse):
        fused[index] = fused.get(index, 0.0) + w_sparse / (k + rank + 1)
    return fused


# ---------------------------------------------------------------------------
# The grounding gate (TRD-14)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GateDecision:
    """Whether retrieved context is good enough to answer from."""

    relevant: bool
    reason: str
    top_similarity: float = 0.0
    entity_matched: bool = False

    @property
    def not_relevant(self) -> bool:
        return not self.relevant


class GroundingGate:
    """Decide whether to answer or to decline.

    Two ways to pass, and the second matters more than it first appears:

    * **Similarity** — the best dense match is at or above the threshold.
    * **Entity match** — the query names a product that is actually in the
      catalogue. A buyer who asks "what is the MOQ on the X-Ray Cassette?" has
      asked something answerable whether or not the embedding happens to score
      well, and refusing them because a vector landed at 0.58 would be a
      self-inflicted failure.

    A threshold of ``0.0`` is refused at boot (see :func:`assert_threshold`).
    A zero threshold disables the gate entirely: every query becomes "relevant",
    including "what is the weather", and the system would answer it from
    whatever chunk ranked first. That is the exact failure mode the gate exists
    to prevent, so it is a configuration error rather than a tuning choice.
    """

    def __init__(
        self,
        threshold: float,
        known_entities: Iterable[str] = (),
        distinctive_terms: Iterable[str] = (),
    ) -> None:
        self.threshold = threshold
        self.known_entities = {e.casefold() for e in known_entities if e}
        # Part numbers and other rare catalogue tokens. See _entity_match.
        self.distinctive_terms = {t.casefold() for t in distinctive_terms if t}

    @classmethod
    def from_chunks(
        cls, chunks: Sequence["Chunk"], threshold: float, max_name_share: float = 0.4
    ) -> "GroundingGate":
        """Build a gate that knows the catalogue's **vocabulary**.

        ``distinctive_terms`` are the tokens that appear in the *product names*
        — "cassette", "drypix", "apo200", "injector". A query containing one of
        them is about a product; that is the signal, and it is a different signal
        from rarity.

        Two earlier attempts at this, both wrong in instructive ways:

        1. **Absolute document frequency (≤3 chunks)** rejected "drypix" — a
           genuinely distinctive model code — because it appears in all six
           chunks of each of its products. Absolute counts measure the scraper's
           chunking, not distinctiveness.
        2. **Relative frequency (≤15% of chunks)** admitted 1,715 terms including
           "mumbai" and a GSTIN, because the company's statutory data is itself
           rare in the corpus. It then judged *"what is the weather in Mumbai"*
           **relevant** — the gate opening on exactly the question it exists to
           refuse. Rarity is not relevance: a word can be rare and still have
           nothing to do with the catalogue.

        Vocabulary is the right test. Tokens that name a product are catalogue
        terms; everything else is not, however rare it happens to be. The
        ``max_name_share`` guard drops tokens so common across product names
        that they distinguish nothing ("ltd", "pvt").
        """
        names = {c.product_name for c in chunks if c.product_name}

        name_token_counts: Counter[str] = Counter()
        for name in names:
            name_token_counts.update(set(tokenize(name)))

        limit = max(1, int(len(names) * max_name_share))
        distinctive = {
            term
            for term, count in name_token_counts.items()
            if count <= limit
            and len(term) >= 3
            and not term.isdigit()
            and not _looks_like_noise(term)
        }

        # Specification field names are catalogue vocabulary in the same sense
        # product names are: they are what the catalogue calls its own data.
        # Without them the gate produced false negatives on the most precise
        # questions a buyer can ask — "contrast media compatibility" is a spec
        # name on the APO 200, present verbatim in the corpus, and the gate
        # refused it as not relevant.
        #
        # Deliberately limited to the *field names* (the text before the first
        # colon), not the values. Values are the rarity trap the docstring above
        # records: the company's statutory data is rare and has nothing to do
        # with the catalogue, so admitting values readmits "what is the weather
        # in Mumbai".
        spec_names: Counter[str] = Counter()
        for chunk in chunks:
            if chunk.chunk_type != "product_specs":
                continue
            for line in (chunk.text or "").splitlines():
                label = line.split(":", 1)[0].strip().casefold()
                if 3 <= len(label) <= 40:
                    spec_names.update(tokenize(label))

        spec_limit = max(1, int(len(names) * max_name_share)) if names else 1
        distinctive |= {
            term
            for term, count in spec_names.items()
            if count <= spec_limit
            and len(term) >= 4
            and not term.isdigit()
            and not _looks_like_noise(term)
        }
        return cls(threshold, names, distinctive)

    def evaluate(self, query: str, top_similarity: float) -> GateDecision:
        entity_matched = self._entity_match(query)
        if top_similarity >= self.threshold:
            return GateDecision(True, "similarity", top_similarity, entity_matched)
        if entity_matched:
            return GateDecision(True, "entity_match", top_similarity, True)
        return GateDecision(False, "below_threshold", top_similarity, False)

    def _entity_match(self, query: str) -> bool:
        """True when the query refers to something genuinely in the catalogue.

        Two signals, and the second was added after a test caught a real gap:

        * **Full product name** — the buyer named a product. Matched on the whole
          name, so "Dry" does not make every question relevant by accident.
        * **A distinctive catalogue token** — a part number or model code.

        The second matters because a buyer searching "APO-200" scores ~0.35 on
        dense similarity: the query is four characters of alphanumerics and a
        word, and no embedding represents that well. The BM25 leg finds it
        immediately — but the gate ran *before* the fused result was consulted,
        so the sparse evidence was discarded and a perfectly answerable question
        was declined. A rare token appearing in the query is strong evidence of
        relevance precisely because it is rare.
        """
        folded = (query or "").casefold()
        if not folded:
            return False
        if any(entity in folded for entity in self.known_entities):
            return True

        # A matched catalogue term must be *strong* evidence on its own.
        #
        # "cassette" (8 chars) or "apo200" (carries a part number) clearly refers
        # to a product. "dry" does not: it is in three product names, so on its
        # own it made "is it dry outside" look like a catalogue question. Short
        # tokens still contribute — they simply need a strong one alongside, so
        # "ultrasound gel" matches on "ultrasound" while "dry" alone does not.
        matched = self.distinctive_terms & set(tokenize(query))
        return any(
            any(ch.isdigit() for ch in term) or len(term) >= 5 for term in matched
        )


def assert_threshold(threshold: float) -> None:
    """Refuse a threshold that disables the gate.

    Called at boot. ``0.0`` is the value the source system shipped as its
    default and it means "always relevant" — every unanswerable question would
    be answered from the nearest chunk, which is precisely the behaviour
    `BRD-04` forbids.
    """
    if threshold <= 0.0:
        raise ValueError(
            "retrieval.similarity_threshold must be greater than 0.0 — a zero "
            "threshold disables the grounding gate and every question becomes "
            "answerable from the nearest chunk (BRD-04). Use 0.62 unless you "
            "have measured a better value."
        )
    if threshold > 1.0:
        raise ValueError(
            f"retrieval.similarity_threshold={threshold} cannot exceed 1.0 for "
            f"cosine similarity"
        )


# ---------------------------------------------------------------------------
# Pluggable backends
# ---------------------------------------------------------------------------
class Embedder(Protocol):
    """Anything that turns text into vectors."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class VectorStore(Protocol):
    """Anything that stores and queries vectors."""

    def query(self, vector: Sequence[float], top_k: int) -> list[tuple[str, float]]: ...

    def upsert(self, ids: Sequence[str], vectors: Sequence[Sequence[float]], metadatas: Sequence[dict]) -> None: ...

    def count(self) -> int: ...

    def name(self) -> str: ...


class InMemoryStore:
    """A brute-force vector store, for tests and for the fallback path.

    At 169 chunks a linear scan over cosine similarity is ~30 microseconds, so
    this is not a toy — it is a legitimate backend. It also means the retrieval
    path can be tested without ChromaDB installed or an embedding service warm.
    """

    def __init__(self, name: str = "in-memory") -> None:
        self._name = name
        self._ids: list[str] = []
        self._vectors: list[list[float]] = []
        self._metadatas: list[dict] = []

    def name(self) -> str:
        return self._name

    def count(self) -> int:
        return len(self._ids)

    def upsert(self, ids, vectors, metadatas) -> None:
        existing = {chunk_id: i for i, chunk_id in enumerate(self._ids)}
        for chunk_id, vector, metadata in zip(ids, vectors, metadatas):
            if chunk_id in existing:
                index = existing[chunk_id]
                self._vectors[index] = list(vector)
                self._metadatas[index] = dict(metadata)
            else:
                existing[chunk_id] = len(self._ids)
                self._ids.append(chunk_id)
                self._vectors.append(list(vector))
                self._metadatas.append(dict(metadata))

    def query(self, vector, top_k: int) -> list[tuple[str, float]]:
        scored: list[tuple[str, float]] = []
        for chunk_id, stored in zip(self._ids, self._vectors):
            scored.append((chunk_id, _cosine(vector, stored)))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# The retriever
# ---------------------------------------------------------------------------
@dataclass
class ScoredChunk:
    chunk: Chunk
    fused_score: float
    dense_score: float = 0.0
    sparse_score: float = 0.0

    @property
    def provenance(self) -> dict:
        """What the answer cites (TRD-16).

        Every answer carries this, so a claim can be traced to a chunk, which
        traces to a URL, which traces to the crawl that produced it.
        """
        return {
            "chunk_id": self.chunk.chunk_id,
            "chunk_type": self.chunk.chunk_type,
            "product_id": self.chunk.product_id,
            "product_name": self.chunk.product_name,
            "source_url": self.chunk.source_url,
            "content_hash": self.chunk.content_hash,
        }


@dataclass
class RetrievalResult:
    """Retrieval outcome. ``decision.relevant`` is False for a decline."""

    decision: GateDecision
    chunks: list[ScoredChunk] = field(default_factory=list)
    query: str = ""

    @property
    def ok(self) -> bool:
        return self.decision.relevant and bool(self.chunks)

    @property
    def provenance(self) -> list[dict]:
        return [scored.provenance for scored in self.chunks]

    def context(self, max_chars: int = 6000) -> str:
        """Assemble context for the prompt, respecting a character budget.

        Truncation is by whole chunks: half a specification row in the prompt is
        worse than one fewer row, because the model will complete the pattern.
        """
        parts: list[str] = []
        used = 0
        for scored in self.chunks:
            block = f"[§ {scored.chunk.product_name or scored.chunk.chunk_type}]\n{scored.chunk.text}"
            if used + len(block) > max_chars:
                if parts:
                    break
                # The first chunk alone exceeds the budget. Including it whole
                # — which the `and parts` guard used to do — meant a 500-char
                # budget could return 1,472 characters, and a single long chunk
                # could exceed it without limit. The budget is a limit, so it
                # is enforced: the chunk is cut to fit rather than the promise
                # broken. Empty is the one outcome worth avoiding, because a
                # prompt with no context is a prompt the model will fill in.
                block = block[:max_chars].rsplit(" ", 1)[0]
            parts.append(block)
            used += len(block)
        return "\n\n---\n\n".join(parts)


class Retriever:
    """Hybrid retrieval with a grounding gate in front of the answer path."""

    def __init__(
        self,
        chunks: Sequence[Chunk],
        embedder: Embedder,
        store: VectorStore,
        gate: GroundingGate,
        *,
        top_k: int = 5,
        fetch_k: int = 20,
    ) -> None:
        self.chunks = list(chunks)
        self.by_id = {chunk.chunk_id: chunk for chunk in self.chunks}
        # Reverse index, built once. Looking this up per hit would make
        # retrieval O(corpus) per candidate.
        self._index_by_id = {chunk.chunk_id: index for index, chunk in enumerate(self.chunks)}
        self.embedder = embedder
        self.store = store
        self.gate = gate
        self.top_k = top_k
        self.fetch_k = fetch_k
        self.bm25 = MiniBM25([chunk.embed_text for chunk in self.chunks])

    # -- indexing ----------------------------------------------------------
    def build_index(self, *, batch_size: int = 32) -> int:
        """Embed and store every chunk. Returns the number indexed."""
        for start in range(0, len(self.chunks), batch_size):
            batch = self.chunks[start : start + batch_size]
            vectors = self.embedder.embed([chunk.embed_text for chunk in batch])
            self.store.upsert(
                ids=[chunk.chunk_id for chunk in batch],
                vectors=vectors,
                metadatas=[
                    {
                        "product_id": chunk.product_id or "",
                        "chunk_type": chunk.chunk_type,
                        "source_url": chunk.source_url or "",
                    }
                    for chunk in batch
                ],
            )
        return self.store.count()

    # -- querying ----------------------------------------------------------
    def retrieve(self, query: str, *, top_k: int | None = None, fetch_k: int | None = None) -> RetrievalResult:
        """Retrieve, fuse, gate, and return — or decline."""
        limit = top_k or self.top_k
        fetch = fetch_k or self.fetch_k

        # 1. Dense leg.
        dense_hits: list[tuple[str, float]] = []
        if self.store.count() > 0:
            vector = self.embedder.embed([query])[0]
            dense_hits = self.store.query(vector, fetch)

        dense_rank = [
            self._index_by_id[chunk_id] for chunk_id, _ in dense_hits if chunk_id in self._index_by_id
        ]
        dense_scores = {
            self._index_by_id[chunk_id]: score
            for chunk_id, score in dense_hits
            if chunk_id in self._index_by_id
        }

        # 2. Sparse leg.
        sparse_hits = self.bm25.search(query, top_k=fetch)
        sparse_rank = [index for index, _ in sparse_hits]
        sparse_scores = dict(sparse_hits)

        # 3. Fuse.
        fused = weighted_rrf(dense_rank, sparse_rank)
        if not fused:
            return RetrievalResult(
                decision=self.gate.evaluate(query, 0.0), chunks=[], query=query
            )

        ordered = sorted(fused.items(), key=lambda pair: pair[1], reverse=True)[:limit]

        # 5. An overwhelming lexical match wins outright.
        #
        # Reciprocal rank fusion is rank-based: it discards how much better the
        # best hit is than the second. That is usually right, and it is wrong
        # for a term lookup. "contrast media compatibility" is a specification
        # field name; BM25 scores it 11.58 against 6.65 for the next, and the
        # chunk contains the phrase verbatim. Fusion ranked it below four FAQs
        # that were mediocre in *both* legs — because a chunk appearing in two
        # lists beats a chunk appearing at the top of one.
        #
        # So the buyer asked for a specification by name and was shown FAQs
        # about unrelated products. The gate had already been fixed to admit
        # that query; admitting it into a wrong answer is worse than the
        # refusal it replaced.
        #
        # The rule is deliberately narrow: only when the leader clears the
        # runner-up by half again *and* clears an absolute floor, which is the
        # signature of an exact phrase match rather than a good-but-close
        # result. A close race keeps the fused ranking. Measured on this corpus,
        # "contrast media compatibility" leads 11.58 to 6.65 — 1.7×.
        if len(sparse_hits) >= 2:
            (best_index, best_score), (_, second_score) = sparse_hits[0], sparse_hits[1]
            if best_score >= 1.5 * max(second_score, 1e-9) and best_score >= 5.0:
                rest = [index for index, _ in ordered if index != best_index]
                ordered = [(best_index, fused[best_index])] + [
                    (index, fused[index]) for index in rest
                ][: limit - 1]

        # 4. Gate — before anything is composed from the context.
        top_similarity = max((score for _, score in dense_hits), default=0.0)
        decision = self.gate.evaluate(query, top_similarity)
        if decision.not_relevant:
            return RetrievalResult(decision=decision, chunks=[], query=query)

        scored = [
            ScoredChunk(
                chunk=self.chunks[index],
                fused_score=score,
                dense_score=dense_scores.get(index, 0.0),
                sparse_score=sparse_scores.get(index, 0.0),
            )
            for index, score in ordered
        ]
        return RetrievalResult(decision=decision, chunks=scored, query=query)

    def build(self) -> "Retriever":
        """Embed the whole corpus into the store. Idempotent by chunk id."""
        self.build_index()
        return self
