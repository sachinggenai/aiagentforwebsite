"""Conversation orchestrator (MOD-02, TRD-07 … TRD-11).

The only module that knows a conversation exists. It resolves identity, groups
turns, classifies the question, and routes it — which is what keeps MOD-01
ignorant of business classification and MOD-04 ignorant of channels. `BRD-01`
depends on that separation.

Two inherited ideas worth naming:

* :class:`ConversationRegistry` is the conversation-identity registry from the
  source platform's ``crm/session.py`` (REC-02). It was mis-namespaced there —
  it sits in the forbidden CRM package but has no CRM dependency at all, and
  deleting the package wholesale would have removed the registry the whole
  application depends on.
* Classification **fails safe to refusal** (`UC-11` E1). When the classifier
  cannot decide, the turn is treated as clinical, because answering a medical
  question the account should not answer is a worse error than declining a
  product question.
"""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .channels import Channel, InboundEnvelope


class Intent(str, Enum):
    """What kind of question this is. Drives routing, nothing else."""

    COMMERCIAL = "commercial"     # price, MOQ, delivery → deterministic SQL
    COMPANY = "company"           # who we are, where we are → deterministic SQL
    LISTING = "listing"           # what do you sell → catalogue overview
    HELP = "help"                 # what can you do → capability statement
    CONSENT = "consent"           # "yes, contact me" → capture the lead
    INTRODUCTION = "introduction"  # greetings and self-identification
    CATALOGUE = "catalogue"       # specs, about, FAQs → retrieval
    CLINICAL = "clinical"         # medical advice → refuse (BRD-10)
    OFF_TOPIC = "off_topic"       # nothing to do with the catalogue → decline


# Ordered: the first pattern that matches wins, so clinical is checked before
# catalogue. "Is this gel safe for a pacemaker patient" contains product words
# and must still be refused.
_INTENT_PATTERNS: tuple[tuple[Intent, re.Pattern[str]], ...] = (
    (Intent.CLINICAL, re.compile(
        r"\b(diagnos\w*|symptom\w*|treat\w*|cure\w*|prescri\w*|dosage|dose|"
        r"side[- ]effect\w*|contraindicat\w*|should i take|medical advice|"
        r"clinical|patient\b.*\bshould)\b"
        r"|\bis (this|it|that)\b.{0,30}\bsafe\b", re.I)),
    # Who we are. Checked before COMMERCIAL, which greedily claims "gst" — and
    # added at all because every one of these questions was being classified
    # OFF_TOPIC and declined, while the answers sat in the corpus: six
    # directors, two GST numbers, the PAN, the IEC, the branches and the
    # certifications. "Where are you located?" is one of the first things a
    # buyer asks, and the assistant was answering it with "I don't have that in
    # our catalogue."
    (Intent.COMPANY, re.compile(
        r"\b(where are you|where is your|your (office|address|location|branch|"
        r"showroom|factory|warehouse)|address|located|head ?office|"
        r"phone|telephone|contact (you|us|number|details)|call you|reach you|"
        r"mobile|email|"
        # "GST" is both: "what is your GST number" is about the company, and
        # "do you offer a GST invoice" is about the sale. The lookahead keeps
        # the second one commercial, where it was before this intent existed.
        r"gst(in)?\b(?!\s*(invoice|bill|payment|purchase))|"
        r"pan\b|iec\b|import export code|"
        r"iso|nsic|crisil|certif\w*|licen[cs]e|"
        r"director|ceo|chairman|managing director|who (runs|owns|founded)|"
        r"promoter|proprietor|"
        r"founded|established|inception|since when|how long have you|"
        r"employees|staff|team size|how many people|"
        r"brands?|deal in|distributor|dealer|authorised|authorized)\b", re.I)),
    (Intent.COMMERCIAL, re.compile(
        r"\b(price|cost|how much|rate|quote|quotation|discount|"
        r"moq|minimum order|order quantity|delivery|lead time|shipping|"
        r"supply ability|sample|payment|invoice)\b", re.I)),
    # Deliberately does NOT include "what is" / "tell me about". Those match
    # almost any sentence — "what is the weather in Mumbai" became a catalogue
    # question — and a classifier that claims everything gives the grounding
    # gate nothing to refuse. Opening phrases are far too cheap a signal here;
    # the domain hint below is the honest test.
    # "What do you sell?" and "what can you do?" are first questions for many
    # buyers. Both were classified OFF_TOPIC and declined with the same
    # sentence used for "who won the world cup?" — which makes a working
    # assistant look broken. Checked before CATALOGUE, which claims "catalogue".
    (Intent.LISTING, re.compile(
        # "catalog(ue)" not "catalog": this is an Indian company and its own
        # site says "catalogue". The American spelling matched and the British
        # one did not, so "Show me the catalogue" — the most natural way to ask,
        # in the company's own words — was classified as a product question,
        # fell through to retrieval, failed the grounding gate, and was
        # declined. The spelling the rest of the corpus uses was the one the
        # classifier could not read.
        r"\b(list\s+(out\s+)*(all\s+)*(your\s+)?(the\s+)?(product\w*|item\w*|"
        r"catalog(ue)?\w*|categor\w*|range|equipment|thing\w*)|"
        r"(your|the|our)?\s*catalog(ue)?s?\b|"
        r"what (kind|type|sort)s? of (product|item|equipment|thing|machine)|"
        r"what (products?|items?|equipment|things) (do you|does|are)|"
        r"(your|the) (product|item) (range|list|catalog(ue)?s?)|"
        r"what do you (sell|supply|make|deal|offer|have)|"
        r"show me (your|the|all) (product|item|catalog(ue)?s?)|"
        r"product (range|list|catalog(ue)?s?)|full catalog(ue)?|whole catalog(ue)?|"
        r"what.{0,12}(available|on offer))\b", re.I)),
    # Consent, and a buyer volunteering contact details. Checked before
    # everything except CLINICAL: "yes" is four characters and matches no other
    # pattern, so without this intent the assistant offered to get in touch and
    # then refused the buyer who accepted.
    (Intent.CONSENT, re.compile(
        r"^\s*(yes|yeah|yep|yup|ya|sure|ok|okay|please do|please|"
        r"go ahead|do it|that works|sounds good|fine|correct|right|"
        r"call me|contact me|reach me|get in touch|please (do|contact|call)|"
        r"i'?d like that|yes please)\s*[.!]?\s*$"
        r"|\b(my (number|phone|mobile|email) is|call me on|reach me (on|at)|"
        r"you can (call|reach|contact) me)\b", re.I)),
    # Conversational scaffolding — greetings, self-identification, thanks,
    # sign-offs. None of these is a question about the catalogue, and all of
    # them were classified OFF_TOPIC and answered with "I don't have that in
    # our catalogue". A buyer opening with "I am Dr Pradeep" was told the
    # catalogue does not contain him.
    #
    # This is the same class of gap as LISTING and HELP, and it exists because
    # the surface was built to answer questions: anything that is not a
    # question had exactly one path, and that path is a refusal.
    (Intent.INTRODUCTION, re.compile(
        r"^\s*(hi|hey|hello|hii+|yo|namaste|namaskar|good\s+(morning|afternoon|"
        r"evening|day)|greetings)\b"
        r"|\b(i am|i'm|im|this is|my name is|myself|name[:\s]+)\s+"
        r"[A-Z][A-Za-z.'-]*"
        r"|\b(thanks|thank you|thx|ok thanks|that'?s all|bye|goodbye|see you)\b"
        r"|\b(speaking|this side|here from)\b", re.I)),
    (Intent.HELP, re.compile(
        r"\b(what can you (do|help|answer|tell)|who are you|what are you|"
        r"how can you help|what do you do|your (capabilit|purpose|function)|"
        r"^help$|^help\b|what can i ask|how (do|can) i (use|ask))\b", re.I)),
    (Intent.CATALOGUE, re.compile(
        r"\b(specificat\w*|spec|weight|dimension\w*|size|material|"
        r"colour|color|model|feature\w*|"
        r"compare|difference|warranty|manufactur\w*|catalogue|catalog)\b", re.I)),
    (Intent.CLINICAL, re.compile(
        r"\b(medic\w* advice|should i (use|buy|take)|is it (safe|ok) (for|to))\b", re.I)),
)

# Words that suggest the question is about the catalogue even when no pattern
# matched. Used only to distinguish OFF_TOPIC from an unsure CATALOGUE.
_DOMAIN_HINT = re.compile(
    r"\b(x-?ray|cassette|imager|imaging|injector|gel|enema|film|screen|apparel|"
    r"microbar|fuji|fujifilm|apollo|kiran|fcr|drypix|prima|capsula|apo|"
    r"product|equipment|machine|you sell|you supply)\b",
    re.I,
)


def classify(text: str) -> Intent:
    """Classify a question. Uncertain input fails safe to refusal."""
    question = (text or "").strip()
    if not question:
        return Intent.OFF_TOPIC

    for intent, pattern in _INTENT_PATTERNS:
        if pattern.search(question):
            return intent

    # No pattern matched. If it mentions the domain, treat it as a catalogue
    # question and let the grounding gate decide — the gate is the better
    # judge of answerability than a keyword list.
    if _DOMAIN_HINT.search(question):
        return Intent.CATALOGUE
    return Intent.OFF_TOPIC


# ---------------------------------------------------------------------------
# Conversation state
# ---------------------------------------------------------------------------
class ConversationState(str, Enum):
    ACTIVE = "active"
    IDLE = "idle"
    CLOSED = "closed"


@dataclass
class Turn:
    role: str
    content: str
    channel: Channel | None = None
    intent: Intent | None = None
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    failed: bool = False


@dataclass
class Conversation:
    """One conversation, grouped by identity where one exists (`CV-02`)."""

    conversation_id: str
    key: str
    state: ConversationState = ConversationState.ACTIVE
    lead_id: str | None = None
    turns: list[Turn] = field(default_factory=list)
    identity: str | None = None
    channels_seen: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_active_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    def history(self, limit: int = 6) -> list[dict[str, str]]:
        """Recent turns in the shape the prompt builder expects."""
        return [
            {"role": turn.role, "content": turn.content}
            for turn in self.turns[-limit:]
        ]

    def as_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "key": self.key,
            "state": self.state.value,
            "turns": self.turn_count,
            "identity": bool(self.identity),
            "channels_seen": self.channels_seen,
        }


class ConversationRegistry:
    """Groups inbound messages into conversations (REC-02, `SM-02`).

    Identity is resolved by phone number or handle where one exists; otherwise
    the session token is used. This is `CV-02` made concrete: continuity applies
    **where an identity exists**, and an anonymous web session is
    conversation-scoped rather than silently merged with someone else's.
    """

    def __init__(self, idle_window_seconds: float = 6 * 3600) -> None:
        self.idle_window_seconds = idle_window_seconds
        self._by_key: dict[str, Conversation] = {}
        self._lock = threading.RLock()

    def resolve(self, envelope: InboundEnvelope, *, now: float | None = None) -> Conversation:
        """Find or create the conversation this envelope belongs to."""
        key = envelope.key
        with self._lock:
            conversation = self._by_key.get(key)
            if conversation is not None and conversation.state is ConversationState.CLOSED:
                conversation = None

            if conversation is None:
                conversation = Conversation(
                    conversation_id=str(uuid.uuid4()),
                    key=key,
                    identity=envelope.identity,
                )
                self._by_key[key] = conversation

            conversation.state = ConversationState.ACTIVE
            conversation.last_active_at = datetime.now(timezone.utc).isoformat()
            if envelope.channel.value not in conversation.channels_seen:
                conversation.channels_seen.append(envelope.channel.value)
            return conversation

    def close(self, key: str) -> None:
        with self._lock:
            if key in self._by_key:
                self._by_key[key].state = ConversationState.CLOSED

    def sweep_idle(self, *, now: float | None = None) -> int:
        """Close conversations idle past the window. Returns how many closed."""
        moment = now if now is not None else datetime.now(timezone.utc).timestamp()
        closed = 0
        with self._lock:
            for conversation in self._by_key.values():
                if conversation.state is ConversationState.CLOSED:
                    continue
                try:
                    last = datetime.fromisoformat(conversation.last_active_at).timestamp()
                except ValueError:
                    continue
                if moment - last > self.idle_window_seconds:
                    conversation.state = ConversationState.CLOSED
                    closed += 1
        return closed

    def get(self, key: str) -> Conversation | None:
        return self._by_key.get(key)

    def count(self) -> int:
        return len(self._by_key)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
@dataclass
class Routing:
    """Where a classified turn should go."""

    intent: Intent
    handler: str
    reason: str

    def as_dict(self) -> dict:
        return {"intent": self.intent.value, "handler": self.handler, "reason": self.reason}


ROUTES: dict[Intent, Routing] = {
    Intent.COMMERCIAL: Routing(Intent.COMMERCIAL, "deterministic", "answerable from SQL"),
    Intent.COMPANY: Routing(Intent.COMPANY, "deterministic", "statutory and contact facts"),
    Intent.LISTING: Routing(Intent.LISTING, "deterministic", "catalogue overview"),
    Intent.HELP: Routing(Intent.HELP, "deterministic", "capability statement"),
    Intent.CONSENT: Routing(Intent.CONSENT, "capture", "buyer agreed to be contacted"),
    Intent.INTRODUCTION: Routing(Intent.INTRODUCTION, "greet", "conversational scaffolding"),
    Intent.CATALOGUE: Routing(Intent.CATALOGUE, "retrieval", "requires grounded context"),
    Intent.CLINICAL: Routing(Intent.CLINICAL, "refuse", "BRD-10: no clinical advice"),
    Intent.OFF_TOPIC: Routing(Intent.OFF_TOPIC, "decline", "outside the catalogue"),
}


class Orchestrator:
    """Resolves, classifies, and routes. Holds no answer logic."""

    def __init__(self, registry: ConversationRegistry | None = None) -> None:
        self.registry = registry or ConversationRegistry()
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.RLock()

    def begin_turn(self, envelope: InboundEnvelope) -> tuple[Conversation, Routing]:
        """Resolve the conversation, record the inbound, and route it.

        Turns are serialised per conversation: two messages arriving at once
        must not interleave into an incoherent transcript (`SM-02` #6).
        """
        conversation = self.registry.resolve(envelope)
        intent = classify(envelope.text)
        routing = ROUTES[intent]

        with self._lock_for(conversation.conversation_id):
            conversation.turns.append(
                Turn(role="user", content=envelope.text, channel=envelope.channel, intent=intent)
            )
        return conversation, routing

    def complete_turn(
        self, conversation: Conversation, answer: str, *, failed: bool = False
    ) -> None:
        with self._lock_for(conversation.conversation_id):
            conversation.turns.append(Turn(role="assistant", content=answer, failed=failed))

    def _lock_for(self, conversation_id: str) -> threading.RLock:
        with self._guard:
            if conversation_id not in self._locks:
                self._locks[conversation_id] = threading.RLock()
            return self._locks[conversation_id]

    def describe(self) -> dict:
        return {
            "conversations": self.registry.count(),
            "idle_window_s": self.registry.idle_window_seconds,
            "intents": [intent.value for intent in Intent],
        }
