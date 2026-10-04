"""Vector store backends (TRD-12).

:class:`ChromaStore` is the production store; :class:`~anita.retrieval.InMemoryStore`
is the fallback and the test double.

The collection name is guarded **here as well as at boot**. That is deliberate
duplication: `MOD-09`'s isolation guard runs once at startup, but a store can be
constructed later by any code path, and the failure it prevents — this product's
queries silently answered from the university's index — is both severe and
invisible. A second check costs one string comparison.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

logger = logging.getLogger("anita.stores")

# Collections belonging to the sibling university deployment. Naming one here
# would make this product answer from the wrong catalogue (REC-03).
FORBIDDEN_COLLECTIONS: frozenset[str] = frozenset({"langchain", "meridian_kb__v1", "meridian-kb"})


class CollectionNameError(ValueError):
    """A collection name that belongs to another product, or is unusable."""


def assert_collection_name(name: str) -> None:
    """Refuse a collection name that is not this product's own."""
    if not name or not name.strip():
        raise CollectionNameError("collection name must not be empty")
    if name in FORBIDDEN_COLLECTIONS:
        raise CollectionNameError(
            f"collection {name!r} belongs to the sibling university deployment. "
            f"Use a name unique to this product (e.g. anita_ams__v1)."
        )


class ChromaStore:
    """ChromaDB-backed vectors, with an explicit dimension guard.

    Chroma is imported lazily so the retrieval path can be exercised — and the
    store swapped for the in-memory one — with no Chroma installed.
    """

    def __init__(self, path: Path | str, collection: str, *, expected_dimensions: int = 768) -> None:
        assert_collection_name(collection)
        self.path = Path(path)
        self.collection_name = collection
        self.expected_dimensions = expected_dimensions
        self._client = None
        self._collection = None

    def _ensure(self):
        if self._collection is not None:
            return self._collection
        try:
            import chromadb
        except ImportError as exc:  # pragma: no cover - declared dependency
            raise RuntimeError(
                "chromadb is not installed; install requirements.txt or use InMemoryStore"
            ) from exc

        self.path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self.path))
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine", "embed_dim": self.expected_dimensions},
        )
        self._verify_dimension()
        return self._collection

    def _verify_dimension(self) -> None:
        """Refuse to write into a collection built with a different model.

        Cosine distance is meaningless across dimensions, and Chroma will
        happily store the mismatch and return quietly wrong neighbours.
        """
        if self._collection is None:
            return
        metadata = getattr(self._collection, "metadata", None) or {}
        recorded = metadata.get("embed_dim")
        if recorded is not None and int(recorded) != self.expected_dimensions:
            raise RuntimeError(
                f"collection {self.collection_name!r} was built with "
                f"{recorded}-dimensional embeddings but this instance expects "
                f"{self.expected_dimensions}. Rebuild the index (US-010) rather "
                f"than mixing dimensions."
            )

    # -- VectorStore protocol ---------------------------------------------
    def name(self) -> str:
        return self.collection_name

    def count(self) -> int:
        return self._ensure().count()

    def upsert(self, ids: Sequence[str], vectors: Sequence[Sequence[float]], metadatas: Sequence[dict]) -> None:
        collection = self._ensure()
        # Chroma rejects empty metadata values only for some types; normalising
        # here keeps the caller from having to care.
        clean = [
            {k: ("" if v is None else v) for k, v in metadata.items()} for metadata in metadatas
        ]
        collection.upsert(
            ids=list(ids),
            embeddings=[list(vector) for vector in vectors],
            metadatas=clean,
        )

    def query(self, vector: Sequence[float], top_k: int) -> list[tuple[str, float]]:
        collection = self._ensure()
        if collection.count() == 0:
            return []
        result = collection.query(
            query_embeddings=[list(vector)],
            n_results=min(top_k, collection.count()),
            include=["distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        # Chroma returns cosine *distance*; retrieval compares similarities.
        return [
            (chunk_id, 1.0 - float(distance))
            for chunk_id, distance in zip(ids, distances)
        ]

    def delete_collection(self) -> None:
        """Drop the collection. Used by the rebuild path (SM-05)."""
        if self._client is None:
            self._ensure()
        if self._client is not None:
            try:
                self._client.delete_collection(self.collection_name)
            except Exception as exc:  # noqa: BLE001 - absence is the desired state
                logger.warning("could not delete collection %s: %s", self.collection_name, exc)
        self._collection = None

    def describe(self) -> dict:
        return {
            "backend": "chroma",
            "path": str(self.path),
            "collection": self.collection_name,
            "expected_dimensions": self.expected_dimensions,
        }
