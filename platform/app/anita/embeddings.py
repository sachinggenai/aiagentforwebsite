"""Embedding backends (TRD-12).

Ollama for Windows/Linux, and the same shape can front MLX on Apple Silicon.

Two details here are not incidental:

**Documents and queries are embedded differently.** `nomic-embed-text` was
trained with task prefixes — ``search_document:`` for indexed content and
``search_query:`` for questions. Skipping them costs real retrieval quality, and
the failure is invisible: results still come back, they are just worse. Hence
:meth:`OllamaEmbedder.embed_documents` and :meth:`OllamaEmbedder.embed_query`
rather than a single method that silently does the wrong thing for one side.

**Keep-alive is set explicitly.** Without it Ollama unloads the model after five
minutes and the next caller pays a 1–3 s reload — which lands directly in the
voice latency budget. The source system learned this the hard way; the setting
is carried over deliberately.
"""

from __future__ import annotations

import logging
import time
from typing import Sequence

logger = logging.getLogger("anita.embeddings")

# Model families that expect task prefixes. Keyed by a substring of the model
# name so `nomic-embed-text` and any `nomic-embed-text:v1.5` both match.
PREFIX_SCHEMES: dict[str, tuple[str, str]] = {
    "nomic": ("search_document: ", "search_query: "),
}


class EmbeddingError(RuntimeError):
    """The embedding backend could not be reached or returned something unusable."""


def prefix_scheme_for(model: str) -> tuple[str, str] | None:
    """Return ``(document_prefix, query_prefix)`` for a model, or None."""
    folded = (model or "").lower()
    for key, scheme in PREFIX_SCHEMES.items():
        if key in folded:
            return scheme
    return None


def _normalise_keep_alive(value: str) -> str:
    """Give a bare number the unit Ollama requires.

    ``keep_alive`` is a Go duration on the wire, so ``-1`` is rejected with
    ``400: time: missing unit in duration "-1"`` while ``-1m`` means the same
    thing and is accepted. A unitless value is the obvious thing to write in a
    config file — and it was written in three places here — so it is repaired
    rather than rejected. A value that already carries a unit passes through.
    """
    text = str(value or "").strip()
    if not text:
        return "-1m"
    if text[-1].isdigit():
        return text + "s"
    return text


class OllamaEmbedder:
    """Embeds text via an Ollama server's ``/api/embed`` endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str = "nomic-embed-text",
        *,
        keep_alive: str = "-1m",
        timeout: float = 20.0,
        batch_size: int = 32,
        max_retries: int = 2,
        expected_dimensions: int | None = 768,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.keep_alive = _normalise_keep_alive(keep_alive)
        self.timeout = timeout
        self.batch_size = max(1, batch_size)
        self.max_retries = max_retries
        self.expected_dimensions = expected_dimensions
        self._scheme = prefix_scheme_for(model)
        self._dimensions: int | None = None

    # -- protocol ----------------------------------------------------------
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed documents (indexed content), applying the document prefix."""
        return self.embed_documents(texts)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        prefix = self._scheme[0] if self._scheme else ""
        return self._embed_many([prefix + text for text in texts])

    def embed_query(self, text: str) -> list[float]:
        """Embed a question, applying the query prefix.

        Separate from :meth:`embed_documents` because the prefixes differ; a
        shared method would embed one side wrongly and there would be no
        symptom other than slightly worse answers.
        """
        prefix = self._scheme[1] if self._scheme else ""
        vectors = self._embed_many([prefix + (text or "")])
        return vectors[0]

    # -- transport ---------------------------------------------------------
    def _embed_many(self, prefixed: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(prefixed), self.batch_size):
            batch = prefixed[start : start + self.batch_size]
            vectors.extend(self._request(batch))
        # The dimension guard lives here, not in _request, because it is a
        # property of the embedder rather than of the HTTP transport. Putting it
        # in the transport meant any other transport — a batch API, a cache, a
        # test double — silently skipped the check that stops two models being
        # mixed in one collection.
        self._verify_dimensions(vectors)
        return vectors

    def _request(self, batch: list[str]) -> list[list[float]]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - declared dependency
            raise EmbeddingError("httpx is required to reach the embedding backend") from exc

        payload = {
            "model": self.model,
            "input": batch,
            "keep_alive": self.keep_alive,
        }
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = httpx.post(
                    f"{self.base_url}/api/embed", json=payload, timeout=self.timeout
                )
                if response.status_code >= 500:
                    raise EmbeddingError(f"embedding backend returned {response.status_code}")
                if response.status_code >= 400:
                    # 4xx is our fault, not a blip — retrying cannot help.
                    raise EmbeddingError(
                        f"embedding request rejected ({response.status_code}): "
                        f"{response.text[:200]}"
                    )
                data = response.json()
                vectors = data.get("embeddings") or []
                if len(vectors) != len(batch):
                    raise EmbeddingError(
                        f"expected {len(batch)} embeddings, got {len(vectors)}"
                    )
                return vectors
            except EmbeddingError:
                raise
            except Exception as exc:  # noqa: BLE001 - transport failures are retryable
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(0.5 * attempt)
        raise EmbeddingError(f"embedding backend unreachable: {last_error}") from last_error

    def _verify_dimensions(self, vectors: list[list[float]]) -> None:
        """Catch a model swap before it corrupts the index.

        Mixing embedding models in one collection produces plausible-looking
        nonsense — every query returns something, none of it meaningful. The
        first response pins the dimension and every later one is checked against
        it; the collection carries the same expectation (TRD-12).
        """
        if not vectors or not vectors[0]:
            raise EmbeddingError("embedding backend returned an empty vector")
        observed = len(vectors[0])
        if self._dimensions is None:
            self._dimensions = observed
            if self.expected_dimensions and observed != self.expected_dimensions:
                raise EmbeddingError(
                    f"model {self.model!r} produces {observed}-dimensional vectors "
                    f"but the index expects {self.expected_dimensions}. Refusing to "
                    f"mix dimensions — check EMBED_MODEL or rebuild the index."
                )
        elif observed != self._dimensions:
            raise EmbeddingError(
                f"embedding dimension changed mid-run ({self._dimensions} -> {observed})"
            )

    # -- lifecycle ---------------------------------------------------------
    def warm(self) -> bool:
        """Force the model resident so the first real query is not the one that
        pays the load. Returns True when the backend answered."""
        try:
            self._embed_many(["warmup"])
            return True
        except EmbeddingError as exc:
            logger.warning("embedding warm-up failed: %s", exc)
            return False

    @property
    def dimensions(self) -> int | None:
        return self._dimensions

    def describe(self) -> dict:
        return {
            "backend": "ollama",
            "model": self.model,
            "base_url": self.base_url,
            "prefix_scheme": self._scheme is not None,
            "batch_size": self.batch_size,
            "dimensions": self._dimensions or self.expected_dimensions,
        }
