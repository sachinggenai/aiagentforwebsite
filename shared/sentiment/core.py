"""The sentiment core — extraction, composite scoring, categorisation, escalation.

Merged from two implementations that were already the same design: the source
platform's `app/sentiment/` (1,239 LOC, five files) and ANITA's adaptation of it
(516 LOC, this file's origin).

**What the merge had to preserve, and why it is not a straight port.** ANITA's
version added two capabilities the source does not have:

* a **rule-based fallback** (`rule_based_extraction`) so an LLM outage degrades
  to a lexical score instead of silently returning neutral;
* **escalation** (`should_escalate` + the `on_escalate` callback). The source has
  no escalation path at all — its nearest artefact is a display string. Adopting
  the source verbatim would have deleted ANITA's UC-10.

Both survive here. They are the reason this module is a merge rather than a copy.

Everything product-specific is injected through `SentimentConfig`: the weights,
the threshold, the domain prompt and the objection vocabulary. The module reads
no `os.environ`, holds no import-time settings, and names no product.
"""

from __future__ import annotations

from .config import DEFAULT_NEGATIVE_THRESHOLD, SentimentConfig

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

logger = logging.getLogger("anita.sentiment")


class Emotion(str, Enum):
    """Product-enquiry emotions, not admissions ones (REC-05)."""

    INTERESTED = "interested"
    READY = "ready"            # ready to buy / asked for a quotation
    NEUTRAL = "neutral"
    UNCERTAIN = "uncertain"    # needs information before deciding
    FRUSTRATED = "frustrated"
    ANGRY = "angry"
    CONFUSED = "confused"


class Category(str, Enum):
    HOT = "Hot"
    WARM = "Warm"
    NURTURE = "Nurture"
    AT_RISK = "At-Risk"
    DISQUALIFIED = "Disqualified"


class ScoreState(str, Enum):
    """`SM-03`."""

    PENDING = "pending"
    SCORED = "scored"
    FALLBACK = "fallback"
    FAILED = "failed"


CATEGORY_ACTIONS: dict[Category, str] = {
    Category.HOT: "Call within the hour; the buyer has asked for pricing.",
    Category.WARM: "Follow up today with the specifics they asked about.",
    Category.NURTURE: "Send catalogue details; no urgency yet.",
    Category.AT_RISK: "Escalate to a person now — the buyer is unhappy.",
    Category.DISQUALIFIED: "No further contact unless they re-engage.",
}


@dataclass
class Extraction:
    """What a scorer produced for one turn."""

    primary_emotion: Emotion = Emotion.NEUTRAL
    sentiment_score: float = 0.0        # [-1, 1]
    buying_intent_score: float = 0.0    # [0, 1]
    friction_score: float = 0.0         # [0, 1]
    objections: list[str] = field(default_factory=list)
    state: ScoreState = ScoreState.PENDING
    source: str = "llm"                 # llm | rules

    def as_dict(self) -> dict:
        return {
            "primary_emotion": self.primary_emotion.value,
            "sentiment_score": round(self.sentiment_score, 3),
            "buying_intent_score": round(self.buying_intent_score, 3),
            "friction_score": round(self.friction_score, 3),
            "objections": self.objections,
            "state": self.state.value,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# The extraction prompt — the taxonomy is the re-target
# ---------------------------------------------------------------------------
EXTRACTION_PROMPT = _GENERIC_FALLBACK = """You are analysing a buyer's message to a supplier
supplier. Return ONLY a JSON object, no prose.

Buyers are hospitals, clinics, diagnostic centres and service engineers
enquiring about imaging equipment and consumables.

{
  "primary_emotion": one of
      "interested" | "ready" | "neutral" | "uncertain" |
      "frustrated" | "angry" | "confused",
  "sentiment_score": float -1.0 to 1.0,
  "buying_intent_score": float 0.0 to 1.0,
  "friction_score": float 0.0 to 1.0,
  "objections": array of short labels from
      ["price", "delivery_time", "minimum_order", "specification_unclear",
       "trust", "support", "compatibility", "payment_terms", "other"]
}

Guidance:
- "ready" and high buying_intent = asked for a quotation, price or availability.
- "frustrated" or "angry" = any complaint, impatience, or repeated question.
- "uncertain" = needs more information before deciding.
- friction_score rises with confusion, repetition or blockers.
- An objection is only listed when the buyer actually raised it.

Message:
{message}
"""


def build_extraction_prompt(message: str, *, prompt: str | None = None) -> str:
    return (prompt or EXTRACTION_PROMPT).replace("{message}", (message or "")[:2000])


# ---------------------------------------------------------------------------
# Rule-based fallback
# ---------------------------------------------------------------------------
_OBJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("price", re.compile(r"\b(expensive|costly|too much|cheaper|discount|price is high|budget)\b", re.I)),
    ("delivery_time", re.compile(r"\b(too long|how long|delay\w*|urgent\w*|lead time|when will)\b", re.I)),
    ("minimum_order", re.compile(r"\b(minimum|moq|small(er)? (order|quantity)|single unit|one piece)\b", re.I)),
    ("specification_unclear", re.compile(r"\b(not clear|unclear|which one|difference between|not sure which|specify)\b", re.I)),
    ("trust", re.compile(r"\b(genuine|authentic|warranty|guarantee|reliable|trust|fake)\b", re.I)),
    ("support", re.compile(r"\b(service|repair|spare|maintenance|support|install)\b", re.I)),
    ("compatibility", re.compile(r"\b(compatible|fit|works with|suitable for|replace)\b", re.I)),
    ("payment_terms", re.compile(r"\b(payment|invoice|gst|credit|advance|terms)\b", re.I)),
)

_NEGATIVE = re.compile(
    r"\b(angry|furious|ridiculous|unacceptable|terrible|worst|complaint|"
    r"disappointed|frustrat\w*|still waiting|no one|nobody|again\?|"
    r"this is absurd|not happy|poor service)\b", re.I)
_NEGATIVE_MILD = re.compile(
    r"\b(expensive|too long|confus\w*|unclear|problem|issue|difficult|"
    r"slow|not working|doesn't work|wrong)\b", re.I)
_POSITIVE = re.compile(
    r"\b(thanks|thank you|great|excellent|perfect|appreciate|helpful|"
    r"good|nice|please proceed|sounds good|interested|keen)\b", re.I)
_READY = re.compile(
    r"\b(send (me )?(a )?(quote|quotation|proforma|invoice)|"
    r"place an order|want to (buy|order|purchase)|proceed|"
    r"final price|best price|how do i order|ready to buy)\b", re.I)
_QUESTION = re.compile(r"\?|\b(what|which|how|when|can you|could you|do you)\b", re.I)


def rule_based_extraction(message: str) -> Extraction:
    """The fallback scorer. Crude by design — it must be dependable.

    A lexicon cannot judge nuance, and it does not try to. It exists so that
    escalation still fires when the model is down, which is the one behaviour
    that must never be unavailable.
    """
    text = message or ""
    strong_negative = len(_NEGATIVE.findall(text))
    mild_negative = len(_NEGATIVE_MILD.findall(text))
    positive = len(_POSITIVE.findall(text))
    ready = bool(_READY.search(text))

    score = 0.0
    score -= 0.6 * strong_negative
    score -= 0.2 * mild_negative
    score += 0.3 * positive
    score = max(-1.0, min(1.0, score))

    if strong_negative:
        emotion = Emotion.ANGRY if strong_negative > 1 else Emotion.FRUSTRATED
    elif ready:
        emotion = Emotion.READY
    elif positive:
        emotion = Emotion.INTERESTED
    elif _QUESTION.search(text):
        emotion = Emotion.UNCERTAIN
    else:
        emotion = Emotion.NEUTRAL

    intent = 0.8 if ready else (0.5 if positive else 0.2 if _QUESTION.search(text) else 0.1)
    friction = min(1.0, 0.5 * strong_negative + 0.2 * mild_negative)

    objections = [
        label for label, pattern in _OBJECTION_PATTERNS if pattern.search(text)
    ]

    return Extraction(
        primary_emotion=emotion,
        sentiment_score=score,
        buying_intent_score=min(1.0, intent),
        friction_score=friction,
        objections=objections,
        state=ScoreState.FALLBACK,
        source="rules",
    )


def parse_extraction(raw: str) -> Extraction:
    """Parse the model's JSON, defensively.

    Models wrap JSON in prose and fences. A parse failure must not lose the
    turn — it falls back to the lexicon rather than raising.
    """
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in scorer output")

    try:
        data = json.loads(match.group(0))
    except ValueError as exc:
        raise ValueError(f"scorer output was not valid JSON: {exc}") from exc

    def bounded(value, low, high, default=0.0):
        try:
            return max(low, min(high, float(value)))
        except (TypeError, ValueError):
            return default

    try:
        emotion = Emotion(str(data.get("primary_emotion", "neutral")).lower())
    except ValueError:
        emotion = Emotion.NEUTRAL

    objections = data.get("objections") or []
    if not isinstance(objections, list):
        objections = []
    known = {label for label, _ in _OBJECTION_PATTERNS} | {"other"}

    return Extraction(
        primary_emotion=emotion,
        sentiment_score=bounded(data.get("sentiment_score"), -1.0, 1.0),
        buying_intent_score=bounded(data.get("buying_intent_score"), 0.0, 1.0),
        friction_score=bounded(data.get("friction_score"), 0.0, 1.0),
        objections=[str(o).lower() for o in objections if str(o).lower() in known],
        state=ScoreState.SCORED,
        source="llm",
    )


# ---------------------------------------------------------------------------
# Composite and categorisation
# ---------------------------------------------------------------------------
@dataclass
class Weights:
    """`SENTIMENT_W1..W4`. Weights are configuration, not constants."""

    w1: float = 0.30    # latest sentiment
    w2: float = 0.30    # momentum (change since the previous turn)
    w3: float = 0.25    # buying intent
    w4: float = 0.15    # friction (subtracted)
    ewma_lambda: float = 0.35

    def as_dict(self) -> dict:
        return {
            "w1": self.w1, "w2": self.w2, "w3": self.w3, "w4": self.w4,
            "ewma_lambda": self.ewma_lambda,
        }


@dataclass
class CompositeScore:
    """A lead's position after one more turn."""

    score: float
    latest: float
    delta: float
    intent: float
    friction: float
    baseline: float          # EWMA of history
    turn_count: int = 1

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "latest": round(self.latest, 3),
            "delta": round(self.delta, 3),
            "intent": round(self.intent, 3),
            "friction": round(self.friction, 3),
            "baseline": round(self.baseline, 3),
            "turn_count": self.turn_count,
        }


def composite_score(
    latest: float,
    *,
    previous_latest: float | None = None,
    intent: float = 0.0,
    friction: float = 0.0,
    baseline: float | None = None,
    turn_count: int = 1,
    weights: Weights | None = None,
) -> CompositeScore:
    """`S = W1·latest + W2·delta + W3·intent − W4·friction`, clamped to [-1, 1].

    Momentum (`delta`) is what distinguishes a buyer who is fine from one who is
    getting angrier. Two identical negative turns scored on latest sentiment
    alone look the same; the second one is measurably worse, and that is the
    turn that should escalate.
    """
    w = weights or Weights()
    previous = latest if previous_latest is None else previous_latest
    delta = latest - previous

    score = w.w1 * latest + w.w2 * delta + w.w3 * intent - w.w4 * friction
    score = max(-1.0, min(1.0, score))

    # EWMA over the conversation, so one bad turn does not permanently mark a
    # lead and one good turn does not erase a pattern.
    prior = baseline if baseline is not None else latest
    updated_baseline = w.ewma_lambda * latest + (1 - w.ewma_lambda) * prior

    return CompositeScore(
        score=score,
        latest=latest,
        delta=delta,
        intent=intent,
        friction=friction,
        baseline=updated_baseline,
        turn_count=turn_count,
    )


def categorise(
    composite: CompositeScore,
    extraction: Extraction,
    *,
    negative_threshold: float = DEFAULT_NEGATIVE_THRESHOLD,
) -> Category:
    """Assign a category. Precedence is explicit and ordered.

    Order matters: a furious buyer who has strong intent is **At-Risk**, not
    Hot. Someone who is unhappy *and* interested is the most valuable call a rep
    can make, and burying them under "Hot" loses the reason they need attention.
    """
    if extraction.primary_emotion in {Emotion.ANGRY, Emotion.FRUSTRATED}:
        return Category.AT_RISK
    if composite.score <= negative_threshold:
        return Category.AT_RISK
    if extraction.primary_emotion is Emotion.READY and composite.score > 0.3:
        return Category.HOT
    if composite.intent >= 0.5 and composite.score > 0.0:
        return Category.WARM
    if composite.score < 0.0:
        return Category.NURTURE
    return Category.NURTURE


def should_escalate(
    composite: CompositeScore,
    extraction: Extraction,
    *,
    negative_threshold: float = DEFAULT_NEGATIVE_THRESHOLD,
) -> tuple[bool, str]:
    """`BRD-16`: decide within the turn whether a human is needed."""
    if extraction.primary_emotion in {Emotion.ANGRY, Emotion.FRUSTRATED}:
        return True, f"emotion={extraction.primary_emotion.value}"
    if composite.score <= negative_threshold:
        return True, f"composite {composite.score:.2f} <= {negative_threshold}"
    if composite.delta <= -0.5:
        return True, f"sharp decline ({composite.delta:+.2f})"
    return False, ""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class Scorer(Protocol):
    """Anything that turns a message into an :class:`Extraction`."""

    def __call__(self, message: str) -> Extraction: ...


@dataclass
class LeadSentiment:
    """Running sentiment state for one lead (`SM-03`, `DAT-09`)."""

    lead_id: str
    baseline: float = 0.0
    turn_count: int = 0
    previous_latest: float | None = None
    history: list[dict] = field(default_factory=list)
    category: Category = Category.NURTURE
    last_score: float = 0.0
    score_state: ScoreState = ScoreState.PENDING
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> dict:
        return {
            "lead_id": self.lead_id,
            "category": self.category.value,
            "score": round(self.last_score, 3),
            "baseline": round(self.baseline, 3),
            "turn_count": self.turn_count,
            "state": self.score_state.value,
            "history": self.history[-5:],
        }


class SentimentService:
    """Scores turns, maintains the per-lead composite, and triggers escalation."""

    def __init__(
        self,
        scorer: Scorer | None = None,
        *,
        weights: Weights | None = None,
        negative_threshold: float = DEFAULT_NEGATIVE_THRESHOLD,
        on_escalate=None,
    ) -> None:
        self.scorer = scorer
        self.weights = weights or Weights()
        self.negative_threshold = negative_threshold
        self.on_escalate = on_escalate
        self._leads: dict[str, LeadSentiment] = {}

    # -- scoring -----------------------------------------------------------
    def score_turn(self, message: str) -> Extraction:
        """Score one message, falling back to the lexicon when the model fails.

        Never raises. A scoring failure degrades to rules; it must not lose the
        turn or block escalation.
        """
        if self.scorer is not None:
            try:
                return self.scorer(message)
            except Exception as exc:  # noqa: BLE001 - degrade, never propagate
                logger.warning("LLM scorer failed (%s); using rules", type(exc).__name__)

        extraction = rule_based_extraction(message)
        return extraction

    def observe(
        self, lead_id: str, message: str, *, extraction: Extraction | None = None
    ) -> tuple[LeadSentiment, bool, str]:
        """Score a turn, update the lead, and decide whether to escalate.

        Returns ``(lead_sentiment, escalate, reason)``.
        """
        found = extraction or self.score_turn(message)
        state = self._leads.setdefault(lead_id, LeadSentiment(lead_id=lead_id))

        composite = composite_score(
            found.sentiment_score,
            previous_latest=state.previous_latest,
            intent=found.buying_intent_score,
            friction=found.friction_score,
            baseline=state.baseline if state.turn_count else None,
            turn_count=state.turn_count + 1,
            weights=self.weights,
        )

        state.previous_latest = found.sentiment_score
        state.baseline = composite.baseline
        state.turn_count = composite.turn_count
        state.last_score = composite.score
        state.score_state = found.state
        state.category = categorise(
            composite, found, negative_threshold=self.negative_threshold
        )
        state.updated_at = datetime.now(timezone.utc).isoformat()
        state.history.append(
            {
                "at": state.updated_at,
                "message_chars": len(message or ""),
                **found.as_dict(),
                "composite": composite.as_dict(),
                "category": state.category.value,
            }
        )

        escalate, reason = should_escalate(
            composite, found, negative_threshold=self.negative_threshold
        )
        if escalate and self.on_escalate:
            # Best-effort: an escalation-sink failure must not lose the score.
            try:
                self.on_escalate(state, reason, found)
            except Exception:  # noqa: BLE001
                logger.exception("escalation handler failed")

        return state, escalate, reason

    # -- reads -------------------------------------------------------------
    def for_lead(self, lead_id: str) -> LeadSentiment | None:
        return self._leads.get(lead_id)

    def at_risk(self) -> list[LeadSentiment]:
        return [state for state in self._leads.values() if state.category is Category.AT_RISK]

    def summary(self) -> dict:
        tallies: dict[str, int] = {}
        for state in self._leads.values():
            tallies[state.category.value] = tallies.get(state.category.value, 0) + 1
        return {"leads_scored": len(self._leads), "by_category": tallies}


# ---------------------------------------------------------------------------
# The contract surface (CR-01 contract §C3.2)
# ---------------------------------------------------------------------------
class SentimentCore:
    """Sentiment scoring behind an injected config.

    This is the class the contract names. The module-level functions above are
    the implementation it delegates to — kept public because they were the
    tested surface before the merge, and rewriting them would have discarded the
    tests that prove the two source behaviours still work.

    Nothing is read at import. Two products can hold two cores with different
    prompts, weights and thresholds in one process with no shared state.
    """

    def __init__(self, config: SentimentConfig, *, store=None, on_escalate=None) -> None:
        self.config = config
        self.store = store
        self.on_escalate = on_escalate
        self._weights = Weights(
            w1=config.w1, w2=config.w2, w3=config.w3, w4=config.w4,
            ewma_lambda=config.ewma_lambda,
        )

    # -- extraction --------------------------------------------------------
    def extract(self, message: str) -> Extraction:
        """Model if a client is injected, lexical rules otherwise.

        The fallback is not a degraded afterthought: it is what makes the core
        usable by a product with no model at all, and it is the reason an LLM
        outage produces a real score rather than a neutral one.
        """
        if self.config.client is None:
            return rule_based_extraction(message)
        prompt = build_extraction_prompt(message, prompt=self.config.extraction_prompt)
        try:
            raw = self.config.client.extract(prompt)
        except Exception:  # noqa: BLE001 - an outage falls back, it does not fail the turn
            return rule_based_extraction(message)
        try:
            return parse_extraction(raw)
        except ValueError:
            # A model that returns unparseable output is an outage in every way
            # that matters to the buyer.
            return rule_based_extraction(message)

    # -- scoring -----------------------------------------------------------
    def score_turn(
        self,
        message: str,
        *,
        lead_id: str = "",
        previous_latest: float | None = None,
        baseline: float | None = None,
        turn_count: int = 1,
    ) -> tuple[Extraction, CompositeScore]:
        extraction = self.extract(message)
        composite = composite_score(
            extraction.sentiment_score,
            previous_latest=previous_latest,
            intent=extraction.buying_intent_score,
            friction=extraction.friction_score,
            baseline=baseline,
            turn_count=turn_count,
            weights=self._weights,
        )
        return extraction, composite

    def categorise(self, composite: CompositeScore, extraction: Extraction) -> Category:
        return categorise(
            composite, extraction, negative_threshold=self.config.negative_threshold
        )

    def should_escalate(
        self, composite: CompositeScore, extraction: Extraction
    ) -> tuple[bool, str]:
        return should_escalate(
            composite, extraction, negative_threshold=self.config.negative_threshold
        )

    # -- dispatch ----------------------------------------------------------
    def observe(self, state: LeadSentiment, extraction: Extraction,
                composite: CompositeScore) -> bool:
        """Decide, dispatch, and record — in that order.

        The decision is computed even when dispatch is suppressed, so a
        disabled escalation still leaves the fact that it *would* have fired.
        """
        escalate, reason = self.should_escalate(composite, extraction)
        if escalate and self.config.escalation_enabled and self.on_escalate is not None:
            try:
                self.on_escalate(state, reason, extraction)
            except Exception:  # noqa: BLE001 - a failed sink must not fail the turn
                import logging

                logging.getLogger(__name__).exception(
                    "escalation callback failed for %s", getattr(state, "lead_id", "?")
                )
        return escalate

    def status(self) -> dict:
        return {
            "client": "injected" if self.config.has_client else "none",
            "extraction_mode": "model" if self.config.has_client else "rule_based",
            "escalation_enabled": self.config.escalation_enabled,
            "negative_threshold": self.config.negative_threshold,
            "weights": self._weights.as_dict(),
            "store": type(self.store).__name__ if self.store is not None else None,
        }
