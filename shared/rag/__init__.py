"""shared.rag — product-neutral retrieval.

Extracted from universityDemo's ``app/rag.py`` + ``app/rag_legacy.py`` with every
product-specific value injected through ``RagConfig`` (CR-01 decision Q8→b).

Nothing here imports from ANITA or from universityDemo: two products import this
package, this package imports neither. That constraint is asserted by a test
that scans the source and by one that inspects ``sys.modules`` after import.

Importing this package performs **no** ``os.environ`` writes. The source mutated
``LANGCHAIN_*`` and ``HF_*`` process-wide at import (`rag_legacy.py:34-41`); that
block is deleted, not copied (migration obligation M-3).
"""

from __future__ import annotations

from .breaker import BreakerState, CircuitBreaker
from .chunking import SourceChunk, load_sources, validate_corpus
from .config import RagConfig
from .errors import (
    CorpusGateError,
    DimensionMismatch,
    EmbeddingUnavailable,
    RagConfigError,
    RagError,
    SpaceMismatch,
    VectorStoreUnavailable,
)
from .fusion import MiniBM25, lexical_override, tokenize, weighted_rrf
from .service import (
    RUNG_LOCAL,
    RUNG_MCP,
    RUNG_NONE,
    Embedder,
    McpClient,
    RagService,
    RetrievalResult,
    RetrievedChunk,
)
from .store import ChromaVectorStore, InMemoryVectorStore, VectorStore

__all__ = [
    "RagConfig", "RagService", "RetrievalResult", "RetrievedChunk",
    "Embedder", "McpClient", "VectorStore", "InMemoryVectorStore", "ChromaVectorStore",
    "CircuitBreaker", "BreakerState", "SourceChunk", "load_sources", "validate_corpus",
    "MiniBM25", "weighted_rrf", "lexical_override", "tokenize",
    "RagError", "RagConfigError", "CorpusGateError", "EmbeddingUnavailable",
    "VectorStoreUnavailable", "DimensionMismatch", "SpaceMismatch",
    "RUNG_MCP", "RUNG_LOCAL", "RUNG_NONE",
]
