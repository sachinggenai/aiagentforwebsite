"""Vector stores.

Two implementations behind one Protocol. ``InMemoryVectorStore`` is a
brute-force scan — legitimate at this corpus size (microseconds for hundreds of
chunks) and it keeps the package testable with no Chroma installed.
``ChromaVectorStore`` is the persistent one.

Persistence is the point of US-103: the index currently lives in a Python list,
is rebuilt on every process start (~25s for 141 chunks), and is thrown away on
exit.
"""

from __future__ import annotations

import math
from typing import Protocol, Sequence, runtime_checkable

from .errors import DimensionMismatch, SpaceMismatch, VectorStoreUnavailable


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-12
    nb = math.sqrt(sum(y * y for y in b)) or 1e-12
    return dot / (na * nb)


@runtime_checkable
class VectorStore(Protocol):
    def upsert(self, ids: Sequence[str], vectors: Sequence[Sequence[float]],
               metadatas: Sequence[dict] | None = None) -> None: ...
    def query(self, vector: Sequence[float], top_k: int) -> list[tuple[str, float]]: ...
    def count(self) -> int: ...
    def name(self) -> str: ...


class InMemoryVectorStore:
    """Brute-force cosine scan. No dependency, no persistence."""

    def __init__(self, *, expected_dimensions: int | None = None) -> None:
        self._ids: list[str] = []
        self._vectors: list[list[float]] = []
        self._metadatas: list[dict] = []
        self._positions: dict[str, int] = {}
        self.expected_dimensions = expected_dimensions

    def name(self) -> str:
        return "in-memory"

    def count(self) -> int:
        return len(self._ids)

    def metadata(self, chunk_id: str) -> dict:
        position = self._positions.get(chunk_id)
        return self._metadatas[position] if position is not None else {}

    def upsert(self, ids, vectors, metadatas=None) -> None:
        if metadatas is None:
            metadatas = [{} for _ in ids]
        if not (len(ids) == len(vectors) == len(metadatas)):
            raise ValueError("ids, vectors and metadatas must be the same length")
        for chunk_id, vector, meta in zip(ids, vectors, metadatas):
            vector = list(vector)
            if self.expected_dimensions is None:
                self.expected_dimensions = len(vector)
            elif len(vector) != self.expected_dimensions:
                raise DimensionMismatch(self.expected_dimensions, len(vector))
            if chunk_id in self._positions:
                # Idempotent by chunk_id: a rebuild replaces, never duplicates.
                at = self._positions[chunk_id]
                self._vectors[at] = vector
                self._metadatas[at] = dict(meta)
                continue
            self._positions[chunk_id] = len(self._ids)
            self._ids.append(chunk_id)
            self._vectors.append(vector)
            self._metadatas.append(dict(meta))

    def query(self, vector, top_k) -> list[tuple[str, float]]:
        if not self._ids:
            return []
        scored = [
            (self._ids[i], cosine(vector, self._vectors[i]))
            for i in range(len(self._ids))
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]


class ChromaVectorStore:
    """Persistent Chroma collection.

    Chroma is imported lazily so the package imports — and the in-memory store
    is usable — with Chroma absent.

    The distance space is checked, not assumed. A collection built with L2 and
    queried as though its distances were cosine similarities produces numbers
    that look like similarities and are not, which is worse than an error.
    """

    def __init__(self, path, collection: str, *, expected_dimensions: int | None = None,
                 distance_space: str = "cosine") -> None:
        self.path = str(path)
        self.collection_name = collection
        self.expected_dimensions = expected_dimensions
        self.distance_space = distance_space
        self._collection = None

    def name(self) -> str:
        return f"chroma:{self.collection_name}"

    def _open(self):
        if self._collection is not None:
            return self._collection
        try:
            import chromadb
        except ImportError as exc:
            raise VectorStoreUnavailable(
                "chromadb is not installed — pip install 'chromadb>=1.0,<2.0'"
            ) from exc
        try:
            client = chromadb.PersistentClient(path=self.path)
            existing = {c.name for c in client.list_collections()}
            if self.collection_name in existing:
                self._collection = client.get_collection(self.collection_name)
                self._verify_space()
            else:
                self._collection = client.create_collection(
                    self.collection_name,
                    metadata={"hnsw:space": self.distance_space},
                )
        except SpaceMismatch:
            raise
        except Exception as exc:  # noqa: BLE001 - surface as our own type
            raise VectorStoreUnavailable(f"could not open collection: {exc}") from exc
        return self._collection

    def _verify_space(self) -> None:
        try:
            meta = dict(self._collection.metadata or {})
        except Exception:  # noqa: BLE001
            return
        found = meta.get("hnsw:space")
        if found and found != self.distance_space:
            raise SpaceMismatch(
                f"collection {self.collection_name!r} was built with hnsw:space={found!r} "
                f"but is queried as {self.distance_space!r}; the distances are not comparable"
            )

    def count(self) -> int:
        try:
            return int(self._open().count())
        except (VectorStoreUnavailable, SpaceMismatch):
            raise
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreUnavailable(str(exc)) from exc

    def upsert(self, ids, vectors, metadatas=None) -> None:
        if metadatas is None:
            metadatas = [{} for _ in ids]
        vectors = [list(v) for v in vectors]
        if vectors:
            width = len(vectors[0])
            if self.expected_dimensions is not None and width != self.expected_dimensions:
                raise DimensionMismatch(self.expected_dimensions, width)
            self.expected_dimensions = self.expected_dimensions or width
        try:
            # Idempotent by id: re-running a build replaces rather than appends.
            self._open().upsert(ids=list(ids), embeddings=vectors,
                                metadatas=[dict(m) for m in metadatas])
        except (VectorStoreUnavailable, SpaceMismatch, DimensionMismatch):
            raise
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreUnavailable(str(exc)) from exc

    def query(self, vector, top_k) -> list[tuple[str, float]]:
        try:
            result = self._open().query(query_embeddings=[list(vector)], n_results=top_k)
        except (VectorStoreUnavailable, SpaceMismatch):
            raise
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreUnavailable(str(exc)) from exc
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        # Chroma returns distance; the contract is similarity. Cosine distance
        # is 1 - similarity — asserted in the tests rather than assumed here.
        return [(chunk_id, 1.0 - float(d)) for chunk_id, d in zip(ids, distances)]


__all__ = ["VectorStore", "InMemoryVectorStore", "ChromaVectorStore", "cosine"]
