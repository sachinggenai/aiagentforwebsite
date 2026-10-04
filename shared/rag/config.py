"""RagConfig — the single injected surface.

Every value a product could differ on lives here. Nothing in this package reads
``os.environ``, holds a module-level mutable, or knows a product's name, so two
products can share the package with no fork and no code edit.

The validation is deliberately in ``__post_init__``: a broken config must fail
before any I/O, so a typo costs nothing rather than half-building an index.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import RagConfigError

_MODES = frozenset({"off", "on", "auto"})
_SEARCH_MODES = frozenset({"hybrid", "dense"})
_SPACES = frozenset({"cosine", "l2", "ip"})


@dataclass(frozen=True)
class RagConfig:
    """Everything product-specific, injected.

    Field names and types are fixed by the CR-01 contract §C3.1; the additive
    fields below carry constants cited from the source (breaker timings, RRF k,
    fusion weights) so they are visible and tunable rather than buried.
    """

    # --- contract-fixed -------------------------------------------------
    collection_name: str
    persist_path: Path
    embed_model: str
    sources: tuple[Path, ...]
    expected_markers: tuple[str, ...]
    blocked_markers: tuple[str, ...]
    system_prompt: str
    refusal_text: str
    top_k: int = 5
    fetch_k: int = 20
    chunk_size: int = 600
    chunk_overlap: int = 90

    # --- breaker (constants carried from the source, see Appendix B.3) ---
    mode: str = "off"
    cooldown_s: float = 30.0
    read_timeout_s: float = 6.0
    probe_fraction: float = 0.25
    probe_floor_s: float = 0.5
    # NOT carried from source: the source opens on a SINGLE failure. That is
    # defect B-8 — one transient timeout disables the primary for a full
    # cooldown. A threshold is the fix.
    failure_threshold: int = 3
    retrieval_budget_s: float = 8.0
    fallback_reserve_s: float = 1.5

    # --- fusion ---------------------------------------------------------
    rrf_k: int = 60
    dense_weight: float = 0.65
    sparse_weight: float = 0.35
    search_mode: str = "hybrid"

    # --- store / embedding ----------------------------------------------
    embed_batch_size: int = 32
    max_context_chars: int = 6000
    distance_space: str = "cosine"

    def __post_init__(self) -> None:
        """Validate and refuse. Never coerce — a silently repaired config is a
        config the operator did not write."""
        if not self.collection_name or any(c.isspace() for c in self.collection_name):
            raise RagConfigError("collection_name", "must be non-empty with no whitespace")
        if not str(self.persist_path):
            raise RagConfigError("persist_path", "must be non-empty")
        if not self.embed_model:
            raise RagConfigError("embed_model", "must be non-empty")
        if not self.sources:
            raise RagConfigError("sources", "at least one source is required")
        if not self.expected_markers:
            # An empty acceptance list accepts anything, which makes the gate a
            # no-op while looking present. That is the B-1 failure mode.
            raise RagConfigError("expected_markers", "an empty acceptance list would accept any corpus")
        if not self.blocked_markers:
            raise RagConfigError("blocked_markers", "an empty rejection list would reject nothing")
        if "{context}" not in self.system_prompt:
            # Without the placeholder the prompt renders with the context
            # missing and the model answers from nothing — a silent KeyError at
            # answer time rather than a loud one at build time.
            raise RagConfigError("system_prompt", "must contain the '{context}' placeholder")
        if not self.refusal_text:
            raise RagConfigError("refusal_text", "must be non-empty")

        mode = self.mode.strip().lower()
        if mode not in _MODES:
            # B-10: the source admitted an unrecognised mode like 'auto' but then
            # skipped the fallback, returning "" with rung "none". Refuse instead.
            raise RagConfigError("mode", f"must be one of {sorted(_MODES)}, got {self.mode!r}")
        if self.search_mode not in _SEARCH_MODES:
            raise RagConfigError("search_mode", f"must be one of {sorted(_SEARCH_MODES)}")
        if self.dense_weight <= 0 or self.sparse_weight <= 0:
            raise RagConfigError("dense_weight/sparse_weight", "both must be > 0")
        if self.failure_threshold < 1:
            raise RagConfigError("failure_threshold", "must be >= 1")
        if not 0 < self.probe_fraction <= 1.0:
            raise RagConfigError("probe_fraction", "must be in (0, 1]")
        if self.distance_space not in _SPACES:
            raise RagConfigError("distance_space", f"must be one of {sorted(_SPACES)}")
        if self.top_k < 1 or self.fetch_k < 1:
            raise RagConfigError("top_k/fetch_k", "must be >= 1")
        if self.chunk_overlap >= self.chunk_size:
            raise RagConfigError("chunk_overlap", "must be smaller than chunk_size")
        if self.rrf_k < 1:
            raise RagConfigError("rrf_k", "must be >= 1")

    @property
    def normalised_mode(self) -> str:
        """The mode with surrounding whitespace and case removed.

        One reader, so the two-readers-disagree defect (B-9: ``.lower()`` in one
        place and ``.strip().lower()`` in another) cannot recur.
        """
        return self.mode.strip().lower()

    def with_(self, **changes: object) -> "RagConfig":
        """A copy with fields replaced, re-validated.

        Exists so a caller can derive a variant (a test's tiny corpus, a
        different collection) without mutating a frozen config or bypassing
        validation.
        """
        from dataclasses import replace

        return replace(self, **changes)  # type: ignore[arg-type]


__all__ = ["RagConfig"]
