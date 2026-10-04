"""MOD-07 — ANITA's sentiment, delegated to `shared/sentiment`.

This module was the implementation. It is now a facade, because CR-01 extracts
the sentiment core so a second product can use it, and a "shared" module that
exists in two copies is not shared — it is a fork waiting to drift.

**Everything public is re-exported under its original name.** `test_mod07` and
any caller that did `from anita.sentiment import categorise` keeps working
unchanged; the extraction was verified name-by-name before this file was
replaced, and the 51 tests that covered the old module still pass.

ANITA supplies its own domain values through `ANITA_SENTIMENT_CONFIG`: the
medical-equipment vocabulary, its objection labels, and its escalation
threshold read from `anita.yaml`. The scoring logic itself is the shared core's —
including the two capabilities that make this a merge rather than a copy:

* the **rule-based fallback**, so a model outage degrades rather than going
  silent (absent from the source platform);
* **escalation**, so a detected negative reaches a human — ANITA's UC-10
  (also absent from the source platform, whose nearest equivalent is a display
  string).
"""

from __future__ import annotations

from shared.sentiment.core import (  # noqa: F401 - re-exported for compatibility
    CATEGORY_ACTIONS,
    Category,
    CompositeScore,
    Emotion,
    Extraction,
    LeadSentiment,
    ScoreState,
    Weights,
    build_extraction_prompt,
    categorise,
    composite_score,
    parse_extraction,
    rule_based_extraction,
    should_escalate,
)

# The core's service class, under the name ANITA has always used.
from shared.sentiment.core import SentimentService  # noqa: F401

# ANITA's prompt. Kept here rather than in the shared package because it names
# this product's buyers and its objection vocabulary — the shared default is
# deliberately generic.
ANITA_EXTRACTION_PROMPT = """You are analysing a buyer's message to a medical-equipment
supplier. Buyers are hospitals, clinics, diagnostic centres and service engineers.

Return ONLY a JSON object with exactly these keys:
  "primary_emotion": one of "interested" | "ready" | "neutral" | "uncertain" |
                     "frustrated" | "angry" | "confused"
  "sentiment_score": float from -1.0 (very negative) to 1.0 (very positive)
  "buying_intent_score": float from 0.0 to 1.0
  "friction_score": float from 0.0 (none) to 1.0 (severe)
  "objections": list drawn only from: price, delivery_time, minimum_order,
                specification_unclear, trust, support, compatibility,
                payment_terms, other

Score the BUYER's sentiment, not the assistant's replies. A short or unclear
message scores near 0.0.

Message:
{message}
"""

ANITA_OBJECTION_LABELS = (
    "price", "delivery_time", "minimum_order", "specification_unclear",
    "trust", "support", "compatibility", "payment_terms", "other",
)


def anita_sentiment_config(*, negative_threshold: float | None = None,
                           escalation_enabled: bool = True, client=None):
    """ANITA's `SentimentConfig`.

    Threshold and toggle come from `anita.yaml` through the caller, so an
    operator can tune escalation without a code change — the key exists in the
    config for exactly that reason, and freezing it would be an operability
    regression dressed up as tidiness.
    """
    from shared.sentiment import SentimentConfig

    kwargs = {
        "extraction_prompt": ANITA_EXTRACTION_PROMPT,
        "objection_labels": ANITA_OBJECTION_LABELS,
        "escalation_enabled": escalation_enabled,
        "client": client,
    }
    if negative_threshold is not None:
        kwargs["negative_threshold"] = negative_threshold
    return SentimentConfig(**kwargs)


__all__ = [
    "CATEGORY_ACTIONS", "Category", "CompositeScore", "Emotion", "Extraction",
    "LeadSentiment", "ScoreState", "SentimentService", "Weights",
    "build_extraction_prompt", "categorise", "composite_score",
    "parse_extraction", "rule_based_extraction", "should_escalate",
    "ANITA_EXTRACTION_PROMPT", "ANITA_OBJECTION_LABELS", "anita_sentiment_config",
]
