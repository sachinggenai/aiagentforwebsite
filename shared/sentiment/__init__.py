"""shared.sentiment — product-neutral sentiment scoring.

Merged from the source platform's `app/sentiment/` and ANITA's adaptation of it.
Everything a product differs on is injected through `SentimentConfig`; nothing
is read at import and no product's vocabulary is baked in.

Two capabilities exist here that the source platform does not have, and both are
required to survive:
  * a rule-based fallback, so an LLM outage degrades instead of going silent;
  * escalation, so a detected negative actually reaches a human (ANITA's UC-10).

Importing this package performs no `os.environ` writes and imports no product
module.
"""

from __future__ import annotations

from .config import (
    DEFAULT_NEGATIVE_THRESHOLD,
    GENERIC_EXTRACTION_PROMPT,
    ExtractionClient,
    SentimentConfig,
)
from .core import (
    CATEGORY_ACTIONS,
    Category,
    CompositeScore,
    Emotion,
    Extraction,
    LeadSentiment,
    ScoreState,
    SentimentCore,
    SentimentService,
    Weights,
    build_extraction_prompt,
    categorise,
    composite_score,
    parse_extraction,
    rule_based_extraction,
    should_escalate,
)
from .store import (
    InMemorySentimentStore,
    LeadSentimentState,
    SentimentStore,
    StoreUnavailable,
    StoredScore,
)
from .testing import assert_store_conforms

__all__ = [
    # config
    "SentimentConfig", "ExtractionClient", "GENERIC_EXTRACTION_PROMPT",
    "DEFAULT_NEGATIVE_THRESHOLD",
    # core
    "SentimentCore", "SentimentService", "Extraction", "CompositeScore", "Weights",
    "Category", "Emotion", "ScoreState", "LeadSentiment", "CATEGORY_ACTIONS",
    "rule_based_extraction", "parse_extraction", "build_extraction_prompt",
    "composite_score", "categorise", "should_escalate",
    # store
    "SentimentStore", "StoredScore", "LeadSentimentState",
    "InMemorySentimentStore", "StoreUnavailable",
    # testing
    "assert_store_conforms",
]
