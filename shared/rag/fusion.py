"""Sparse retrieval and rank fusion.

Carried from the source with one substantive change: the fusion is
**parameterised**. The source's fusion is unweighted and keyed on document text
(`rag_legacy.py:438-453`); ANITA's is weighted 0.65/0.35 and keyed on chunk
index, with a lexical-override rule. Adopting the source unchanged would change
ANITA's rankings (CR-01 CV-09), so both policies are expressible here and each
product passes its own.

Nothing here reads configuration or a global — a ``MiniBM25`` instance owns its
own index, so two services in one process cannot share one (source defect B-6,
where the cache was keyed by the literal "bm25" regardless of collection).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Sequence

_WORD_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens.

    Deliberately simple and dependency-free: a stemmer would change ranking on
    every corpus and the ranking is what the migration must preserve.
    """
    return _WORD_RE.findall((text or "").lower())


class MiniBM25:
    """Okapi BM25 over an in-memory corpus.

    Brute-force by design. At the corpus sizes this package serves (hundreds to
    low thousands of chunks) a linear scan is microseconds, and an inverted
    index would add a structure whose staleness is a new failure mode.
    """

    def __init__(self, documents: Sequence[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._docs = [tokenize(doc) for doc in documents]
        self._lengths = [len(d) for d in self._docs]
        self._avg_length = (sum(self._lengths) / len(self._lengths)) if self._docs else 0.0
        self._frequencies = [Counter(d) for d in self._docs]
        self._document_frequency: Counter[str] = Counter()
        for freq in self._frequencies:
            self._document_frequency.update(freq.keys())

    def __len__(self) -> int:
        return len(self._docs)

    def _idf(self, term: str) -> float:
        n = len(self._docs)
        df = self._document_frequency.get(term, 0)
        # BM25's probabilistic idf, floored at a small positive value so a term
        # present in every document still contributes rather than going negative.
        return max(math.log((n - df + 0.5) / (df + 0.5) + 1.0), 1e-9)

    def search(self, query: str, top_k: int = 20) -> list[tuple[int, float]]:
        """(document index, score) for the best ``top_k``, highest first.

        Documents scoring zero are omitted: returning them would let fusion
        promote a chunk the sparse leg did not actually match.
        """
        terms = tokenize(query)
        if not terms or not self._docs:
            return []
        scored: list[tuple[int, float]] = []
        for index, freq in enumerate(self._frequencies):
            length = self._lengths[index] or 1
            score = 0.0
            for term in terms:
                tf = freq.get(term)
                if not tf:
                    continue
                norm = self.k1 * (1 - self.b + self.b * length / (self._avg_length or 1))
                score += self._idf(term) * (tf * (self.k1 + 1)) / (tf + norm)
            if score > 0:
                scored.append((index, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]


def weighted_rrf(
    dense_rank: Sequence[int],
    sparse_rank: Sequence[int],
    *,
    k: int = 60,
    dense_weight: float = 0.65,
    sparse_weight: float = 0.35,
) -> dict[int, float]:
    """Reciprocal rank fusion over two ranked index lists.

    Rank-based, so it deliberately discards score magnitude — a property that is
    usually right and is wrong for an exact term lookup, which is why
    ``RagService`` applies a lexical-override rule after this (ANITA's policy,
    `retrieval.py:648-674`). The override lives in the service, not here, so a
    product that does not want it is not given it.
    """
    fused: dict[int, float] = {}
    for rank, index in enumerate(dense_rank):
        fused[index] = fused.get(index, 0.0) + dense_weight / (k + rank + 1)
    for rank, index in enumerate(sparse_rank):
        fused[index] = fused.get(index, 0.0) + sparse_weight / (k + rank + 1)
    return fused


def lexical_override(
    fused_order: Sequence[int],
    sparse_hits: Sequence[tuple[int, float]],
    *,
    margin: float = 1.5,
    floor: float = 5.0,
) -> list[int]:
    """Promote an overwhelming sparse leader to the front.

    BM25's top hit can be an exact phrase match while fusion ranks it below
    chunks that were mediocre in *both* legs — a chunk appearing in two lists
    beats a chunk at the top of one. Measured on ANITA's corpus: "contrast media
    compatibility" leads 11.58 to 6.65, and fusion ranked it below four FAQs
    about unrelated products.

    Narrow by construction: only when the leader clears the runner-up by
    ``margin`` **and** an absolute ``floor``, which is the signature of a phrase
    match rather than a good-but-close result.
    """
    if len(sparse_hits) < 2 or not fused_order:
        return list(fused_order)
    (best, best_score), (_, second_score) = sparse_hits[0], sparse_hits[1]
    if best_score < margin * max(second_score, 1e-9) or best_score < floor:
        return list(fused_order)
    rest = [index for index in fused_order if index != best]
    return [best] + rest


__all__ = ["MiniBM25", "tokenize", "weighted_rrf", "lexical_override"]
