"""Error types for shared.rag.

Every failure this package can produce is one of these, and each names the thing
that was wrong. A caller must be able to tell "your config is broken" (fixable
before any I/O) from "the store is unavailable" (degrade) from "this corpus is
not yours" (refuse) without parsing a message.
"""

from __future__ import annotations


class RagError(Exception):
    """Base for every error this package raises."""


class RagConfigError(RagError):
    """A RagConfig value is missing, empty or out of range.

    Raised from ``RagConfig.__post_init__`` — before any I/O — so a
    misconfiguration costs nothing and cannot half-start a service.
    """

    def __init__(self, field: str, detail: str = "") -> None:
        self.field = field
        super().__init__(f"invalid RagConfig.{field}{(': ' + detail) if detail else ''}")


class CorpusGateError(RagError):
    """The corpus failed its acceptance check.

    Raised by ``build()``. No vectors are written: a corpus that is not this
    product's must not reach a collection, because a mixed index produces
    confident answers from another product's data.
    """

    def __init__(self, reason: str, *, missing: tuple[str, ...] = (),
                 blocked: tuple[str, ...] = ()) -> None:
        self.reason = reason
        self.missing = missing
        self.blocked = blocked
        detail = []
        if missing:
            detail.append(f"no expected marker found among {list(missing)}")
        if blocked:
            detail.append(f"found blocked marker(s) {list(blocked)}")
        super().__init__(f"corpus rejected: {reason}" + (f" — {'; '.join(detail)}" if detail else ""))


class EmbeddingUnavailable(RagError):
    """The embedder could not be reached or returned a malformed response."""


class VectorStoreUnavailable(RagError):
    """The vector store could not be opened, read or written."""


class DimensionMismatch(RagError):
    """The embedder's output width differs from the store's expectation."""

    def __init__(self, expected: int, got: int) -> None:
        self.expected = expected
        self.got = got
        super().__init__(f"embedding dimension {got} does not match the store's {expected}")


class SpaceMismatch(RagError):
    """The existing collection was built with a different distance space.

    Cosine distances compared against an L2 threshold are silently meaningless,
    so this is refused rather than tolerated.
    """


__all__ = [
    "RagError", "RagConfigError", "CorpusGateError", "EmbeddingUnavailable",
    "VectorStoreUnavailable", "DimensionMismatch", "SpaceMismatch",
]
