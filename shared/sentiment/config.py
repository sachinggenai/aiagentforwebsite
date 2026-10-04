"""SentimentConfig — the injected surface for the sentiment core.

Same principle as `shared/rag`: every value a product could differ on is
injected, nothing is read at import, and neither product's vocabulary or
thresholds are baked in.

The extraction client is a **seam**, not an implementation. `shared/rag` carries
the same shape for the primary transport, and for the same reason: a product
that has no model can still run the rule-based path, and the difference is
visible in `status()` rather than implied by which code happens to be present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable

# ONE definition. Before this module the value existed in four unlinked places —
# `config.py:211`, `sentiment.py:348`, `sentiment.py:373`, `sentiment.py:429` —
# any of which could drift without anything noticing. Products may override it
# through their own config; they may not restate it.
DEFAULT_NEGATIVE_THRESHOLD = -0.20


@runtime_checkable
class ExtractionClient(Protocol):
    """The optional model seam.

    Synchronous on purpose: the core is synchronous (ANITA's path), and an
    async product wraps its client rather than forcing `async` up through every
    caller. `shared/rag` made the mirror-image choice for the same reason — the
    seam takes the shape of the caller that has to hold it.
    """

    def extract(self, prompt: str) -> str: ...


# The default prompt is deliberately generic. A product supplies its own domain
# vocabulary through `SentimentConfig.extraction_prompt`; if it supplies none,
# this one asks for the same JSON without claiming to know the industry.
GENERIC_EXTRACTION_PROMPT = """You are analysing a buyer's message to a supplier.

Return ONLY a JSON object with exactly these keys:
  "primary_emotion": one of "interested" | "ready" | "neutral" | "uncertain" |
                     "frustrated" | "angry" | "confused"
  "sentiment_score": float from -1.0 (very negative) to 1.0 (very positive)
  "buying_intent_score": float from 0.0 to 1.0
  "friction_score": float from 0.0 (none) to 1.0 (severe)
  "objections": list of short labels, drawn only from the configured set

Score the buyer's sentiment, not the assistant's replies. If the message is very
short or unclear, set the scores near 0.0.

Message:
{message}
"""


@dataclass(frozen=True)
class SentimentConfig:
    """Everything a product can differ on.

    Field names and types are fixed by the CR-01 contract §C3.2. The defaults
    are the values measured in both products: the weight set and the EWMA decay
    were identical across the two implementations before this merge
    (`scorer.py:258` / `config.py:490-496` and `sentiment.py:325` /
    `sentiment.py:267-271`), so keeping them means the merge changes no scores.
    """

    # --- composite weights (identical in both source implementations) ----
    w1: float = 0.30      # the turn's own sentiment
    w2: float = 0.30      # momentum — the change since the last turn
    w3: float = 0.25      # buying intent
    w4: float = 0.15      # friction, subtracted
    ewma_lambda: float = 0.35

    negative_threshold: float = DEFAULT_NEGATIVE_THRESHOLD

    # --- domain vocabulary, supplied by the product ----------------------
    extraction_prompt: str = GENERIC_EXTRACTION_PROMPT
    objection_labels: tuple[str, ...] = (
        "price", "delivery_time", "minimum_order", "specification_unclear",
        "trust", "support", "compatibility", "payment_terms", "other",
    )

    # --- behaviour toggles ------------------------------------------------
    # Gates *dispatch*, never the *decision*: a suppressed escalation is still
    # recorded, because silence must not erase the evidence that it happened.
    escalation_enabled: bool = True

    # --- the optional model seam -----------------------------------------
    client: ExtractionClient | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not 0 < self.ewma_lambda <= 1:
            raise ValueError(f"ewma_lambda must be in (0, 1], got {self.ewma_lambda}")
        if self.w4 < 0:
            raise ValueError("w4 (friction) must be >= 0")
        if self.negative_threshold >= 0:
            # A non-negative escalation threshold would escalate on neutral
            # traffic, which is the same as escalating on everything.
            raise ValueError(
                f"negative_threshold must be < 0, got {self.negative_threshold}"
            )
        if not self.objection_labels:
            raise ValueError("objection_labels must not be empty")
        if "{message}" not in self.extraction_prompt:
            # Without the placeholder the prompt renders with the buyer's
            # message missing and the model scores nothing.
            raise ValueError("extraction_prompt must contain the '{message}' placeholder")

    @property
    def has_client(self) -> bool:
        return self.client is not None


__all__ = [
    "SentimentConfig", "ExtractionClient", "GENERIC_EXTRACTION_PROMPT",
    "DEFAULT_NEGATIVE_THRESHOLD",
]
