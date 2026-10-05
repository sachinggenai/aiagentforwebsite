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

import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .channels import Channel, InboundEnvelope

logger = logging.getLogger("anita.orchestrator")


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


class IdentityType(str, Enum):
    """What a channel could actually tell us about who is asking (`CV-02`)."""

    PHONE = "phone"
    HANDLE = "handle"       # a WhatsApp id, an extra-channel sender, an email
    SESSION = "session"     # a browser session token — a conversation, not a buyer
    ANONYMOUS = "anonymous"


@dataclass(frozen=True)
class Identity:
    type: IdentityType
    value: str

    @property
    def key(self) -> str:
        """The registry key. A phone normalises to E.164 so the same person on
        two channels reaches one conversation (`BRD-01`)."""
        return self.value


_PHONE_STRIP = re.compile(r"[^\d+]")


def normalise_phone(raw: str) -> str | None:
    """E.164 where it can be, `None` where it cannot.

    The one judgement here is the +91 assumption: a bare 10-digit Indian mobile
    is the format buyers actually type ("98xxxxxxxx"), and Twilio always sends
    E.164 ("+9198xxxxxxxx"). Without bridging the two, the story's own AC-1 —
    a web-chat number recognised when the same person calls — can never fire.
    Anything else that does not normalise is kept as a flagged handle and never
    used for merging (edge case 9).
    """
    value = _PHONE_STRIP.sub("", (raw or "").strip())
    if not value:
        return None
    if value.startswith("00"):
        # The ITU international access code ("0091 98200 11122") is the same
        # number as "+9198…" and is how a good share of buyers write it.
        value = f"+{value[2:]}"
    if value.startswith("+"):
        digits = value[1:]
        return f"+{digits}" if digits.isdigit() and 8 <= len(digits) <= 15 else None
    if not value.isdigit():
        return None
    if len(value) == 10 and value[0] in "6789":
        return f"+91{value}"
    if 11 <= len(value) <= 15:
        return f"+{value}"
    return None


def resolve_identity(envelope: InboundEnvelope) -> Identity:
    """Precedence: phone > handle > session > anonymous (CO-1).

    Never fails: an identity that cannot be resolved degrades to ANONYMOUS,
    which is a conversation-scoped thread with no lead — not an error.
    """
    candidate = (envelope.identity or "").strip()
    if candidate:
        phone = normalise_phone(candidate)
        if phone:
            return Identity(IdentityType.PHONE, phone)
        # A handle: lowercased so "+Acme@Example" and "acme@example" are one
        # sender rather than two.
        return Identity(IdentityType.HANDLE, candidate.lower())
    session = (envelope.session_id or "").strip()
    if session:
        return Identity(IdentityType.SESSION, session)
    # A blank key mints one rather than sharing a bucket called "anonymous":
    # every unnamed visitor landing in one conversation would merge strangers
    # (edge case 4).
    return Identity(IdentityType.ANONYMOUS, str(uuid.uuid4()))


@dataclass
class Turn:
    role: str
    content: str
    channel: Channel | None = None
    intent: Intent | None = None
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    failed: bool = False
    # US-006: a turn loaded from another conversation as context carries its
    # own seq (so it is never re-persisted here); a fresh turn has none and
    # gets one from the store.
    seq: int | None = None
    external: bool = False


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
    identity_type: IdentityType = IdentityType.ANONYMOUS
    parent_conversation_id: str | None = None
    summary: str | None = None
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
            "identity_type": self.identity_type.value,
            "lead_id": self.lead_id,
            "parent_conversation_id": self.parent_conversation_id,
            "channels_seen": self.channels_seen,
        }


class QueueFull(Exception):
    """Too many turns are already waiting for this conversation."""


class ConversationQueue:
    """One in-flight turn per conversation; arrival order preserved (`SM-02` #6).

    The audit reproduced the defect this exists to kill: user A's question,
    user B's question, B's answer, A's answer — because the answer path ran
    outside the per-conversation lock. Here the *whole* turn (record → classify
    → answer → persist) holds the conversation's slot, so the second message
    waits and the transcript cannot interleave.

    Bounded at `depth` waiting turns: beyond it the buyer is re-prompted rather
    than silently dropped or left queueing forever behind a stuck turn.
    """

    def __init__(self, depth: int = 20) -> None:
        self.depth = int(depth)
        self._locks: dict[str, threading.Lock] = {}
        self._waiting: dict[str, int] = {}
        self._guard = threading.RLock()
        self.overflowed = 0

    def _lock_for(self, conversation_id: str) -> threading.Lock:
        with self._guard:
            if conversation_id not in self._locks:
                self._locks[conversation_id] = threading.Lock()
            return self._locks[conversation_id]

    def submit(self, conversation_id: str, work):
        """Run `work()` under the conversation's slot. Raises QueueFull."""
        with self._guard:
            waiting = self._waiting.get(conversation_id, 0)
            if waiting >= self.depth:
                self.overflowed += 1
                raise QueueFull(
                    f"{waiting} turns already waiting on this conversation"
                )
            self._waiting[conversation_id] = waiting + 1
        lock = self._lock_for(conversation_id)
        acquired = False
        try:
            lock.acquire()
            acquired = True
            return work()
        finally:
            if acquired:
                lock.release()
            with self._guard:
                self._waiting[conversation_id] = max(
                    0, self._waiting.get(conversation_id, 1) - 1
                )


class ConversationRegistry:
    """Groups inbound messages into conversations (REC-02, `SM-02`, US-004).

    Identity is resolved by phone or handle where one exists; otherwise the
    session token is used. This is `CV-02` made concrete: continuity applies
    **where an identity exists**, and an anonymous web session is
    conversation-scoped rather than silently merged with someone else's.

    With a store attached the registry survives its own restart: a miss is
    answered from the database inside the idle window (`AC-2`, `AC-6`), and a
    conversation found closed yields a *child* conversation carrying
    `parent_conversation_id` so review shows the thread's history rather than
    an orphan (`AC-4`).
    """

    def __init__(self, idle_window_seconds: float = 6 * 3600, *,
                 store=None, recorder=None, context_turns: int = 6) -> None:
        self.idle_window_seconds = idle_window_seconds
        self.store = store
        self.recorder = recorder
        self.context_turns = int(context_turns)
        self._by_key: dict[str, Conversation] = {}
        self._lock = threading.RLock()

    # -- resolution --------------------------------------------------------
    def resolve(self, envelope: InboundEnvelope, *, now: float | None = None) -> Conversation:
        """Find or create the conversation this envelope belongs to."""
        identity = resolve_identity(envelope)
        key = identity.key
        with self._lock:
            conversation = self._by_key.get(key)
            if conversation is not None and conversation.state is ConversationState.CLOSED:
                conversation = None

            if conversation is None:
                conversation = self._resume_or_open(identity, envelope)

            conversation.state = ConversationState.ACTIVE
            conversation.last_active_at = datetime.now(timezone.utc).isoformat()
            if envelope.channel.value not in conversation.channels_seen:
                conversation.channels_seen.append(envelope.channel.value)
            self._by_key[key] = conversation
            if self.store is not None:
                try:
                    self.store.touch(conversation.conversation_id)
                except Exception:  # noqa: BLE001 - the turn path must not fail here
                    pass
            return conversation

    def _resume_or_open(self, identity: Identity, envelope: InboundEnvelope) -> Conversation:
        """Registry miss: try the database, then open (a child if closed)."""
        record = None
        if self.store is not None and identity.type is not IdentityType.ANONYMOUS:
            record = self.store.find_recent(
                identity_type=identity.type.value,
                identity_value=identity.value,
                within_seconds=None,
            )

        if record is not None and record.state != "CLOSED" and self._in_window(record):
            return self._from_record(record)

        parent = record.id if record is not None else None
        conversation_id = None
        if self.store is not None:
            created = self.store.create_conversation(
                channel_kind=envelope.channel.value,
                identity_type=identity.type.value,
                identity_value=identity.value,
                lead_id=self._lead_for(identity),
                parent_conversation_id=parent,
            )
            conversation_id = created.id

        conversation = Conversation(
            conversation_id=conversation_id or str(uuid.uuid4()),
            key=identity.key,
            identity=envelope.identity,
            identity_type=identity.type,
            parent_conversation_id=parent,
            lead_id=self._lead_for(identity),
        )
        if self.store is not None and parent is not None:
            # The prior thread's tail becomes context — this is what makes
            # `AC-1` ("the products discussed are available") true when the
            # earlier conversation is outside the window.
            self._seed_context(conversation, parent)
        elif self.store is not None and conversation.lead_id:
            # A different thread, the same lead: the buyer who typed their
            # number on web chat and then calls must not have to say which
            # product they meant (`AC-1`).
            self._seed_context(conversation, None, lead_id=conversation.lead_id)
        return conversation

    def _in_window(self, record) -> bool:
        try:
            last = datetime.fromisoformat(record.last_activity_at).timestamp()
        except ValueError:
            return False
        return (datetime.now(timezone.utc).timestamp() - last) <= self.idle_window_seconds

    def _lead_for(self, identity: Identity) -> str | None:
        if self.store is None or identity.type is IdentityType.ANONYMOUS:
            return None
        try:
            return self.store.lead_for_identity(identity.type.value, identity.value)
        except Exception:  # noqa: BLE001 - a lookup failure must not fail a turn
            return None

    def _from_record(self, record) -> Conversation:
        conversation = Conversation(
            conversation_id=record.id,
            key=record.identity_value or record.id,
            identity=record.identity_value,
            identity_type=IdentityType(record.identity_type),
            state=ConversationState(record.state.lower())
            if record.state.lower() in {state.value for state in ConversationState}
            else ConversationState.ACTIVE,
            lead_id=record.lead_id,
            parent_conversation_id=record.parent_conversation_id,
            summary=record.summary,
            created_at=record.started_at,
            last_active_at=record.last_activity_at,
        )
        if self.store is not None:
            self._seed_context(conversation, record.id)
        return conversation

    def _seed_context(self, conversation: Conversation, conversation_id: str | None,
                      *, lead_id: str | None = None) -> None:
        """Load recent turns as context — the thread's own, or the lead's."""
        if self.store is None or conversation.turns:
            return
        records: list = []
        try:
            if lead_id:
                records = self.store.recent_turns_for_lead(
                    lead_id, limit=self.context_turns,
                    exclude_conversation=conversation.conversation_id,
                )
            elif conversation_id:
                records = self.store.recent_turns(
                    conversation_id, limit=self.context_turns
                )
            # A thread of its own with nothing in it is not context: fall back
            # to the lead's other conversations. This is the common shape for a
            # buyer who chats, gives their number and then calls —
            # the call's conversation is new *and empty*, and the products they
            # discussed live in the chat thread (`AC-1`).
            if not records and conversation.lead_id and not lead_id:
                records = self.store.recent_turns_for_lead(
                    conversation.lead_id, limit=self.context_turns,
                    exclude_conversation=conversation.conversation_id,
                )
        except Exception:  # noqa: BLE001 - context is a bonus, never a blocker
            return
        for record in records:
            conversation.turns.append(Turn(
                role=record.role, content=record.text, seq=record.seq, external=True,
                intent=None, at=record.created_at,
            ))

    # -- lifecycle ---------------------------------------------------------
    def close(self, key: str, *, reason: str = "") -> None:
        """Close a conversation and write its summary (idempotent, `TAC-5`)."""
        with self._lock:
            conversation = self._by_key.get(key)
            if conversation is None:
                return
            conversation.state = ConversationState.CLOSED
        # The store row is the durable state; an in-memory close that never
        # reached it would let the next boot resume a conversation this
        # process reported closed.
        if self.store is not None:
            try:
                self.store.close(conversation.conversation_id)
            except Exception:  # noqa: BLE001 - the summary below still gets written
                logger.exception("closing the conversation row failed")
        self._summarise(conversation, reason=reason)

    def sweep_idle(self, *, now: float | None = None) -> int:
        """Close conversations idle past the window, with summaries."""
        moment = now if now is not None else datetime.now(timezone.utc).timestamp()
        closed: list[Conversation] = []
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
                    closed.append(conversation)

        if self.store is not None:
            try:
                for conversation_id in self.store.sweep_idle(
                    idle_window_seconds=self.idle_window_seconds
                ):
                    known = next(
                        (c for c in self._by_key.values()
                         if c.conversation_id == conversation_id),
                        None,
                    )
                    if known is not None and known not in closed:
                        known.state = ConversationState.CLOSED
                        closed.append(known)
            except Exception:  # noqa: BLE001 - a sweep failure is retried next tick
                logger.exception("idle sweep: store sweep failed")

        for conversation in closed:
            self._summarise(conversation, reason="idle")
        return len(closed)

    def _summarise(self, conversation: Conversation, *, reason: str) -> None:
        if self.store is None:
            return
        try:
            self.store.write_summary(
                conversation.conversation_id, self.summary_for(conversation, reason=reason)
            )
        except Exception:  # noqa: BLE001 - a summary failure never blocks teardown
            logger.exception("conversation summary failed")

    def finalise(self, conversation: Conversation, *, reason: str = "teardown") -> None:
        """Summarise without closing — the voice/browser teardown path."""
        self._summarise(conversation, reason=reason)

    @staticmethod
    def summary_for(conversation: Conversation, *, reason: str = "") -> str:
        """The handoff's one-paragraph payload (`BRD-11`, `UC-13`).

        Written from the turns rather than from a live object graph, so it is
        reproducible after a restart — a summary that could only be produced by
        the process that took the call is not a record.
        """
        if not conversation.turns:
            return f"No transcript — conversation closed ({reason or 'teardown'})."
        channels = " + ".join(conversation.channels_seen) or "unknown channel"
        first = next((t.content for t in conversation.turns if t.role == "user"), "")
        intents = sorted({
            t.intent.value for t in conversation.turns if t.intent is not None
        })
        return (
            f"{len(conversation.turns)} turns on {channels}"
            + (f"; lead {conversation.lead_id}" if conversation.lead_id else "; no lead")
            + (f"; intents: {', '.join(intents)}" if intents else "")
            + (f"; opened with: {first[:120]}" if first else "")
            + f"; closed by {reason or 'teardown'}."
        )

    def get(self, key: str, *, channel: Channel | None = None) -> Conversation | None:
        if channel is not None:
            return self._by_key.get(resolve_identity(InboundEnvelope(
                channel=channel, text="", identity=None, session_id=key
            )).key)
        if key in self._by_key:
            return self._by_key[key]
        # An identity may have normalised the key it was registered under
        # (a bare 10-digit number became E.164); scan so callers that hold the
        # raw key still find their conversation.
        for conversation in self._by_key.values():
            if conversation.key == key:
                return conversation
        return None

    # Aliases the story's LLD names, so the vocabulary matches the design.
    def start(self, envelope: InboundEnvelope) -> Conversation:
        return self.resolve(envelope)

    def recover(self, key: str) -> Conversation | None:
        """Find a conversation from the store when the registry has no memory.

        This is the path that makes a restart survivable (`AC-2`, `AC-6`): the
        registry is empty after a deploy, and the only record of the WhatsApp
        thread that was active 20 minutes ago is the database. Tried as a
        phone first (normalised), then as a session token, then as a handle.
        """
        if self.store is None or not (key or "").strip():
            return None

        lookups: list[tuple[IdentityType, str]] = []
        phone = normalise_phone(key)
        if phone:
            lookups.append((IdentityType.PHONE, phone))
        lookups.append((IdentityType.SESSION, key))
        lookups.append((IdentityType.HANDLE, key.strip().lower()))

        for identity_type, value in lookups:
            try:
                record = self.store.find_recent(
                    identity_type=identity_type.value, identity_value=value,
                    within_seconds=self.idle_window_seconds,
                )
            except Exception:  # noqa: BLE001 - a lookup failure is a miss, not an error
                return None
            if record is not None and record.state != "CLOSED":
                conversation = self._from_record(record)
                with self._lock:
                    self._by_key[conversation.key] = conversation
                return conversation
        return None

    def end(self, key: str, *, reason: str = "") -> None:
        self.close(key, reason=reason)

    def touch(self, key: str) -> None:
        conversation = self.get(key)
        if conversation is not None:
            conversation.last_active_at = datetime.now(timezone.utc).isoformat()

    def active(self, channel: str | None = None) -> list[Conversation]:
        with self._lock:
            return [
                conversation for conversation in self._by_key.values()
                if conversation.state is not ConversationState.CLOSED
                and (channel is None or channel in conversation.channels_seen)
            ]

    def purge_expired(self, idle_window_seconds: float | None = None) -> int:
        window = self.idle_window_seconds if idle_window_seconds is None else idle_window_seconds
        moment = datetime.now(timezone.utc).timestamp()
        removed = 0
        with self._lock:
            for key in list(self._by_key):
                conversation = self._by_key[key]
                if conversation.state is not ConversationState.CLOSED:
                    continue
                try:
                    last = datetime.fromisoformat(conversation.last_active_at).timestamp()
                except ValueError:
                    continue
                if moment - last > window:
                    del self._by_key[key]
                    removed += 1
        return removed

    def count(self) -> int:
        return len(self._by_key)


# `resolve_whatsapp_session` was removed 2026-10-04 (§8.28): no caller in the
# product, and it was wrong if one had appeared — it returned `None` on the
# registry-less path even after finding a record. `ConversationRegistry.recover`
# is the fallback actually used, and the WhatsApp path reaches it through the
# ordinary identity resolution.


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
    """Resolves, classifies, routes — and persists (US-006).

    Holds no answer logic. Every turn it sees is written to the store with its
    classification, route and answer reference, so the transcript a sales rep
    opens is the same transcript the engine answered from (`BRD-11`).
    """

    def __init__(self, registry: ConversationRegistry | None = None, *,
                 queue: ConversationQueue | None = None,
                 queue_depth: int = 20) -> None:
        self.registry = registry or ConversationRegistry()
        self.queue = queue or ConversationQueue(depth=queue_depth)
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.RLock()

    @property
    def store(self):
        return self.registry.store

    @property
    def recorder(self):
        return self.registry.recorder

    def begin_turn(self, envelope: InboundEnvelope) -> tuple[Conversation, Routing]:
        """Resolve the conversation, record the inbound, and route it.

        Turns are serialised per conversation: two messages arriving at once
        must not interleave into an incoherent transcript (`SM-02` #6). The
        queue in `submit_turn` holds the slot for the *whole* turn; the lock
        here only protects the in-memory transcript list.
        """
        started = datetime.now(timezone.utc)
        conversation = self.registry.resolve(envelope)
        intent = classify(envelope.text)
        routing = ROUTES[intent]

        with self._lock_for(conversation.conversation_id):
            conversation.turns.append(
                Turn(role="user", content=envelope.text, channel=envelope.channel, intent=intent)
            )

        self._record(conversation, envelope, intent, routing, started)
        return conversation, routing

    def _record(self, conversation, envelope, intent, routing, started) -> None:
        """Persist the inbound turn. Best-effort by design (`AC-4`)."""
        recorder = self.recorder
        if recorder is None or not recorder.enabled:
            return
        from .conversation_store import CLASSIFIED, NewTurn

        raw = envelope.raw or {}
        record = recorder.record(NewTurn(
            conversation_id=conversation.conversation_id,
            role="user",
            text=envelope.text,
            channel_kind=envelope.channel.value,
            state=CLASSIFIED,
            classification=intent.value,
            route=routing.handler,
            provider_event_id=(raw.get("MessageSid") or raw.get("CallSid") or None),
            latency_ms=int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
        ))
        if record is not None:
            for turn in conversation.turns:
                if turn.role == "user" and turn.seq is None:
                    turn.seq = record.seq

    def complete_turn(
        self, conversation: Conversation, answer: str, *, failed: bool = False,
        answer_ref: str | None = None, grounding_refs: tuple[str, ...] = (),
        failure_reason: str | None = None, latency_ms: int | None = None,
    ) -> None:
        with self._lock_for(conversation.conversation_id):
            conversation.turns.append(Turn(role="assistant", content=answer, failed=failed))

        recorder = self.recorder
        if recorder is not None and recorder.enabled:
            from .conversation_store import ANSWERED, FAILED, NewTurn

            record = recorder.record(NewTurn(
                conversation_id=conversation.conversation_id,
                role="assistant",
                text=answer,
                channel_kind=(conversation.channels_seen[-1] if conversation.channels_seen
                              else "web_chat"),
                state=FAILED if failed else ANSWERED,
                classification=None,
                route=None,
                answer_ref=answer_ref or conversation.conversation_id,
                grounding_refs=tuple(grounding_refs),
                failure_reason=failure_reason if failed else None,
                latency_ms=latency_ms,
            ))
            if record is not None:
                with self._lock_for(conversation.conversation_id):
                    for turn in reversed(conversation.turns):
                        if turn.role == "assistant" and turn.seq is None:
                            turn.seq = record.seq
                            break

        # The lead link is written when the engine captured details on this
        # turn: the identity that produced a lead is what a later call from
        # that number resolves through (`BRD-02`).
        self._link_identity(conversation)

    def _link_identity(self, conversation: Conversation) -> None:
        """Link every identity this turn revealed to the captured lead.

        The phone the buyer **supplied** is the important one: a web visitor
        who types their number is exactly the person who calls tomorrow, and
        the channel identity alone (a browser session) could never match them
        (`AC-1`). The channel's own identity is linked too when it is a real
        one — a WhatsApp sender who leaves a number keeps both handles.
        """
        store = self.store
        if store is None or not conversation.lead_id:
            return

        candidates: list[tuple[IdentityType, str]] = []
        phone = normalise_phone(getattr(conversation, "lead_phone", "") or "")
        if phone:
            candidates.append((IdentityType.PHONE, phone))
        email = (getattr(conversation, "lead_email", "") or "").strip().lower()
        if email and "@" in email:
            candidates.append((IdentityType.HANDLE, email))
        if (conversation.identity_type is not IdentityType.ANONYMOUS
                and conversation.identity_type is not IdentityType.SESSION
                and conversation.key):
            candidates.append((conversation.identity_type, conversation.key))
        if not candidates:
            return

        try:
            store.set_lead(conversation.conversation_id, conversation.lead_id)
            for identity_type, value in candidates:
                store.upsert_identity_link(
                    identity_type=identity_type.value, identity_value=value,
                    lead_id=conversation.lead_id,
                )
        except Exception:  # noqa: BLE001 - a link failure never fails a turn
            logger.exception("identity link failed")

    def submit_turn(self, conversation_id: str, work):
        """Run one turn's work under the conversation's single slot.

        Raises :class:`QueueFull` when the conversation already has `depth`
        turns waiting — the caller re-prompts rather than dropping (`AC-3`
        edge 3).
        """
        return self.queue.submit(conversation_id, work)

    def close(self, conversation: Conversation, *, reason: str = "teardown") -> None:
        """Close and summarise on teardown (`TAC-5`, US-003/US-004)."""
        self.registry.close(conversation.key, reason=reason)

    def finalise(self, conversation: Conversation, *, reason: str = "teardown") -> None:
        """Write the summary without closing (voice/browser teardown).

        TAC-5 asks for a summary at teardown; closing is not asked for, and
        closing would fork the thread the next time the same buyer makes
        contact inside the idle window.
        """
        self.registry.finalise(conversation, reason=reason)

    def sweep_idle(self) -> int:
        """Close idle conversations and summarise them. Returns how many."""
        return self.registry.sweep_idle()

    def retry_summaries(self, *, limit: int = 50) -> int:
        """Re-summarise closed conversations whose summary never landed.

        `write_summary` is guarded on `summary_updated_at IS NULL`, so a retry
        is idempotent and cannot overwrite a good summary (`TAC-5`). Nothing
        called it before this — a summary lost to a transient store failure
        stayed lost, and `unsummarised_closed`, the query that finds them, had
        no caller at all (§8.28).

        Returns how many were **retried**; a retry that fails again is logged
        and will be picked up on the next tick, because the guard is still
        unset.
        """
        store = self.registry.store
        if store is None:
            return 0
        try:
            pending = set(store.unsummarised_closed(limit=limit))
        except Exception:  # noqa: BLE001 - a read failure is retried next tick
            logger.exception("summary retry: could not read the pending list")
            return 0
        if not pending:
            return 0
        with self.registry._lock:  # noqa: SLF001 - the registry owns this map
            candidates = [
                conversation for conversation in self.registry._by_key.values()
                if conversation.conversation_id in pending
            ]
        for conversation in candidates:
            self.registry._summarise(conversation, reason="retry")  # noqa: SLF001
        return len(candidates)

    def expire_transcripts(self, *, retention_days: float, limit: int = 100) -> int:
        """Retention (`DG-11`): drop transcripts past the window, keep the row.

        The conversation row keeps `transcript_expired_at`, so the review can
        say the transcript expired *with its date* rather than showing an empty
        transcript that could equally be a conversation nobody said anything
        in (`AC-7`, `UC-13` E1). The store has had `expire_transcript` since
        US-006 and nothing ever called it (§8.28) — which made that state
        unreachable outside tests.
        """
        store = self.registry.store
        if store is None:
            return 0
        try:
            due = store.transcripts_due_for_expiry(
                older_than_days=retention_days, limit=limit
            )
        except Exception:  # noqa: BLE001 - retried next tick
            logger.exception("retention: could not read the due list")
            return 0
        expired = 0
        for conversation_id in due:
            try:
                store.expire_transcript(conversation_id)
                expired += 1
            except Exception:  # noqa: BLE001 - one bad row must not stop the sweep
                logger.exception("retention: expiring %s failed", conversation_id[:8])
        return expired

    def _lock_for(self, conversation_id: str) -> threading.RLock:
        with self._guard:
            if conversation_id not in self._locks:
                self._locks[conversation_id] = threading.RLock()
            return self._locks[conversation_id]

    def describe(self) -> dict:
        return {
            "conversations": self.registry.count(),
            "persisted": self.store.count_conversations() if self.store else 0,
            "idle_window_s": self.registry.idle_window_seconds,
            "intents": [intent.value for intent in Intent],
        }


class ConversationJanitor:
    """The periodic duties US-006 names: close idle conversations, drain replay.

    A thread rather than a scheduler dependency, mirroring `CapacityMonitor`
    (US-018). Both duties are idempotent, so a missed tick is a delay and never
    a duplicate: the sweep only closes what is still open, and the drain only
    writes what is still buffered.
    """

    def __init__(self, *, orchestrator: Orchestrator, interval_s: float = 60.0,
                 on_event=None, retention_days: float = 0.0) -> None:
        self.orchestrator = orchestrator
        self.interval_s = float(interval_s)
        self._on_event = on_event
        # `DG-11`: 0 disables retention, so a deployment that has not decided a
        # window keeps everything rather than silently deleting on a default.
        self.retention_days = float(retention_days)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.swept = 0
        self.replayed = 0
        self.resummarised = 0
        self.expired = 0

    def run_once(self) -> dict:
        swept = 0
        replayed = 0
        resummarised = 0
        expired = 0
        try:
            swept = self.orchestrator.sweep_idle()
        except Exception:  # noqa: BLE001 - a sweep failure is retried next tick
            logger.exception("janitor: idle sweep failed")
        recorder = self.orchestrator.recorder
        if recorder is not None:
            try:
                replayed = recorder.drain()
            except Exception:  # noqa: BLE001
                logger.exception("janitor: replay drain failed")
        # US-006 TAC-5: a summary lost to a transient store failure is retried
        # rather than lost for good (§8.28).
        try:
            resummarised = self.orchestrator.retry_summaries()
        except Exception:  # noqa: BLE001
            logger.exception("janitor: summary retry failed")
        # `DG-11`: retention runs only when a window is configured.
        if self.retention_days > 0:
            try:
                expired = self.orchestrator.expire_transcripts(
                    retention_days=self.retention_days
                )
            except Exception:  # noqa: BLE001
                logger.exception("janitor: retention sweep failed")
        self.swept += swept
        self.replayed += replayed
        self.resummarised += resummarised
        self.expired += expired
        if swept or replayed or resummarised or expired:
            self._emit("conversation.janitor", {
                "swept": swept, "replayed": replayed,
                "resummarised": resummarised, "expired": expired,
            })
        return {"swept": swept, "replayed": replayed,
                "resummarised": resummarised, "expired": expired}

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="anita-conversations", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            self.run_once()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def _emit(self, event: str, payload: dict) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event, payload)
        except Exception:  # noqa: BLE001
            pass
