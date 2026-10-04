"""Client surfaces: the backend contract (MOD-10, TRD-45 … TRD-48).

The web chat, the browser-mic client and the admin console all talk to this
module. It is deliberately **framework-agnostic**: routes are declared as data
and handlers are plain functions, so the whole surface is testable without
starting a server. Wiring it to FastAPI later is a thin adapter, not a rewrite —
and the shape of the contract is decided here rather than by whichever framework
happens to be in use.

Two things it owns that nothing else can:

**The admin/buyer split from `CV-03`.** Authentication applies to the admin
console — channel health, conversation review, release validation. Buyer
surfaces stay open to buyers, bounded by rate limiting instead. Authenticating
buyers would block the traffic the product exists to serve; leaving the console
open would expose every conversation. This is the remaining half of `DG-06`.

**Honest unavailability.** When a metric cannot be read the console says
"unavailable", never `0`. A dashboard reporting zero during an outage is worse
than one reporting nothing.
"""

from __future__ import annotations

import hmac
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Protocol


logger = logging.getLogger("anita.surfaces")


class Surface(str, Enum):
    WEB_CHAT = "web_chat"
    BROWSER_MIC = "browser_mic"
    ADMIN_CONSOLE = "admin_console"


class Route(str, Enum):
    """Every endpoint the surfaces use. Declared, so the contract is greppable."""

    HEALTH = "GET /health"
    CHAT_MESSAGE = "POST /api/chat/message"
    CHAT_HISTORY = "GET /api/chat/history"
    VOICE_TICKET = "POST /api/voice/ticket"
    ADMIN_CHANNELS = "GET /api/admin/channels"
    ADMIN_CONVERSATIONS = "GET /api/admin/conversations"
    ADMIN_CONVERSATION = "GET /api/admin/conversation"
    ADMIN_METRICS = "GET /api/admin/metrics"
    ADMIN_GATE = "POST /api/admin/gate"


# Which surface each route serves, and whether it needs authentication.
# One table, so the buyer/admin boundary is reviewable at a glance rather than
# inferred from decorators scattered across a file.
ROUTE_TABLE: dict[Route, tuple[Surface, bool]] = {
    Route.HEALTH: (Surface.ADMIN_CONSOLE, False),
    # Buyer surfaces: open, rate-limited (CV-03).
    Route.CHAT_MESSAGE: (Surface.WEB_CHAT, False),
    Route.CHAT_HISTORY: (Surface.WEB_CHAT, False),
    Route.VOICE_TICKET: (Surface.BROWSER_MIC, False),
    # Admin surfaces: authenticated.
    Route.ADMIN_CHANNELS: (Surface.ADMIN_CONSOLE, True),
    Route.ADMIN_CONVERSATIONS: (Surface.ADMIN_CONSOLE, True),
    Route.ADMIN_CONVERSATION: (Surface.ADMIN_CONSOLE, True),
    Route.ADMIN_METRICS: (Surface.ADMIN_CONSOLE, True),
    Route.ADMIN_GATE: (Surface.ADMIN_CONSOLE, True),
}


class AuthError(PermissionError):
    """The caller is not permitted on this surface."""


class Response:
    """A handler result. Deliberately plain — no framework types leak in."""

    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.status = status
        self.body = body

    def as_dict(self) -> dict:
        return {"status": self.status, "body": self.body}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Response({self.status})"


def requires_auth(route: Route) -> bool:
    return ROUTE_TABLE.get(route, (None, True))[1]


def authenticate(route: Route, token: str | None, expected: str) -> None:
    """Enforce the admin/buyer split.

    Constant-time comparison: a timing-variable check on a bearer token is a
    real (if unfashionable) attack, and the cost of avoiding it is one function
    call.
    """
    if not requires_auth(route):
        return
    if not expected:
        # Failing closed matters more here than anywhere: an unset token must
        # refuse, never wave the request through.
        raise AuthError("no admin token configured; admin surfaces are closed")
    if not token:
        raise AuthError("missing bearer token")
    if not hmac.compare_digest(token, expected):
        raise AuthError("invalid bearer token")


# ---------------------------------------------------------------------------
# Buyer surfaces
# ---------------------------------------------------------------------------
@dataclass
class ChatReply:
    text: str
    conversation_id: str
    intent: str = ""
    grounded: bool = False
    escalation: bool = False
    source_url: str | None = None
    unavailable: bool = False
    # Whether this reply is a refusal. Stated by the surface rather than
    # inferred by the client from the absence of another flag: a greeting, a
    # capability statement and an offer to call back all carry grounded=False,
    # and a client that reads that as "declined" tells the buyer the assistant
    # refused when it answered.
    declined: bool = False

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "conversation_id": self.conversation_id,
            "intent": self.intent,
            "grounded": self.grounded,
            "escalation": self.escalation,
            "source_url": self.source_url,
            "unavailable": self.unavailable,
            "declined": self.declined,
        }


DECLINE_TEXT = (
    "I don't have that in our catalogue. Our team can confirm it for you — "
    "would you like us to get in touch?"
)
CLINICAL_TEXT = (
    "I can help with product and order questions, but I can't give medical "
    "advice. Please speak to a clinician for that."
)
OFFLINE_TEXT = (
    "I'm having trouble reaching our system right now. "
    "Please try again shortly, or call us directly."
)
README_SERVICE_UNAVAILABLE = "unavailable"

# Used when no lead service is wired. Identical to DECLINE_TEXT minus the offer
# of a callback the system could not act on.
DECLINE_TEXT_NO_OFFER = (
    "I don't have that in our catalogue. Our team can confirm it for you — "
    "please call us and we'll help."
)

# The exact question the assistant asks when it offers a callback. Consent is
# recognised against the assistant's *own* previous turn rather than a stored
# flag, so there is no second piece of state to fall out of step with what the
# buyer actually read on screen.
#
# Matched as the whole question, not the fragment "get in touch": HELP_TEXT also
# says the team "will need to get in touch", which is a statement about how
# quotations work, not an offer. Matching the fragment made every reply after
# the help text look like an outstanding offer.
OFFER_PHRASE = "would you like us to get in touch"

ASK_CONTACT_TEXT = (
    "Of course. What's the best number or email for you? "
    "Someone from our team will get back to you."
)
LEAD_CAPTURED_TEXT = (
    "Thank you — I've passed your details to our team and someone will be in "
    "touch. Is there anything else I can help with meanwhile?"
)

# A comparison asks about two things at once. Requires an explicit comparison
# word: "X and Y" alone is not enough, because "do you have X and Y?" is a
# stock question, not a comparison.
_COMPARE_RE = re.compile(
    r"\b(compare|comparison|difference|differences|differ|versus|vs\.?|"
    r"which is better|better than)\b", re.I,
)

def _named(product_name: str, text: str) -> str:
    """Prefix a verbatim chunk with the product it describes, once.

    Applied only when the text does not already name the product, so a spec
    list that begins "Apollo RT CT … APO 200 — Protocol Management: …" is left
    alone and a bare FAQ gains its subject.

    Where the name must appear: at the start. A rule of "skip if the name
    appears anywhere" is too lenient — "When is the best time to apply the gel
    during a medical procedure?" mentions the gel mid-sentence and still does
    not say what the answer is about.
    """
    if not product_name or (text or "").casefold().startswith(product_name.casefold()):
        return text
    return f"{product_name}: {text}"


def _is_comparison(message: str) -> bool:
    """Whether the question asks for a comparison of two products."""
    return bool(_COMPARE_RE.search(message or ""))


_THANKS_RE = re.compile(r"\b(thanks|thank you|thx|cheers|that'?s all|that is all)\b", re.I)
_BYE_RE = re.compile(r"\b(bye|goodbye|see you|good night)\b", re.I)


def greeting_text(name: str = "", *, message: str = "") -> str:
    """The reply to a social turn — greeting, thanks or sign-off.

    Kept short and never a product pitch: this is where a buyer decides whether
    they are talking to something that works.

    A greeting, a thank-you and a goodbye are three different moves and read
    badly when answered identically — replying "Hello Dr Pradeep, how can I
    help?" to "thanks" is the same one-reply-for-everything mistake as
    answering every decline with one sentence, just smaller.
    """
    who = f" {name}" if name else ""
    if _THANKS_RE.search(message or ""):
        return f"You're welcome{who}. Anything else I can look up for you?"
    if _BYE_RE.search(message or ""):
        return f"Goodbye{who} — do call us if anything else comes up."
    return (
        f"Hello{who} — how can I help? I can look up specifications, list "
        f"prices, minimum order quantities and delivery times for anything in "
        f"our catalogue."
    )


HELP_TEXT = (
    "I can help with our product catalogue — specifications, list prices, "
    "minimum order quantities and delivery times. I can also tell you about "
    "the company: our offices, phone number, GST and certifications. "
    "For medical advice please speak to a clinician, and for a firm quotation "
    "our team will need to get in touch."
)


class ChatSurface:
    """Web and mobile-web chat. Open to buyers, rate-limited (CV-03)."""

    def __init__(self, orchestrator, answers=None, retriever=None, leads=None, *,
                 sentiment=None, rate_limit=None) -> None:
        self.orchestrator = orchestrator
        self.answers = answers
        self.retriever = retriever
        # MOD-06. Optional: without it the chat still answers, it just cannot
        # take a lead — and it says so rather than promising a callback it has
        # nowhere to record.
        self.leads = leads
        # MOD-07. Optional, and *observational*: scoring the buyer's turn never
        # changes the answer, and a failure in it never fails the turn. It
        # exists so a negative conversation reaches a human (UC-10), which was
        # the deferred capability this wiring closes.
        self.sentiment = sentiment
        from .channels import RateLimiter

        self.rate_limit = rate_limit or RateLimiter(limit=30, window_s=60.0)

    def post_message(self, *, message: str, session_id: str) -> Response:
        if not (message or "").strip():
            return Response(400, {"error": "empty message"})

        try:
            self.rate_limit.check(session_id)
        except Exception as exc:  # noqa: BLE001 - rate limit is a refusal, not a crash
            return Response(429, {"error": str(exc)})

        from .channels import Channel, InboundEnvelope
        from .orchestrator import Intent

        envelope = InboundEnvelope(
            channel=Channel.WEB_CHAT, text=message, session_id=session_id
        )
        conversation, routing = self.orchestrator.begin_turn(envelope)

        reply = self._answer(routing.intent, message, conversation)
        self.orchestrator.complete_turn(conversation, reply.text, failed=reply.unavailable)
        self._observe_sentiment(message, conversation)
        return Response(200, reply.as_dict())

    def _observe_sentiment(self, message: str, conversation) -> None:
        """Score the buyer's turn and escalate if it warrants it.

        Deliberately after the reply and outside the answer path: a sentiment
        failure must not change what the buyer is told, and the buyer must not
        wait on it. Everything here is swallowed for that reason.
        """
        if self.sentiment is None:
            return
        try:
            state = self.sentiment.score_and_observe(
                message,
                lead_id=getattr(conversation, "lead_id", "") or conversation.conversation_id,
                conversation_id=conversation.conversation_id,
            )
            if state is not None:
                # Record it on the conversation so UC-13's review shows why a
                # handoff exists rather than only that one does.
                setattr(conversation, "sentiment", state)
        except Exception:  # noqa: BLE001 - observation never breaks a turn
            logger.exception("sentiment observation failed; the turn is unaffected")

    def get_history(self, *, session_id: str, limit: int = 50) -> Response:
        """The turns of this session's conversation, oldest first.

        Scoped by session id and nothing else: a caller cannot ask for another
        session's history, because the only key they can present is their own.
        An unknown session is an empty list with ``resumed: false`` — not a 404,
        since a first-time visitor asking for their history is normal.
        """
        if not (session_id or "").strip():
            return Response(400, {"error": "empty session_id"})

        conversation = self.orchestrator.registry.get(session_id)
        if conversation is None:
            return Response(200, {"resumed": False, "conversation_id": None, "turns": []})

        turns = [
            {"role": turn.role, "content": turn.content, "at": turn.at}
            for turn in conversation.turns[-limit:]
        ]
        return Response(200, {
            "resumed": True,
            "conversation_id": conversation.conversation_id,
            "state": conversation.state.value,
            "turns": turns,
        })

    def _decline(self) -> str:
        """The decline, with or without the offer of a callback.

        Without a lead service the assistant must not offer to get in touch:
        it has nowhere to record the answer, so it would ask and then fail —
        which is exactly the dead end that made "yes" look broken. A promise
        the system cannot keep is worse than no promise.
        """
        return DECLINE_TEXT if self.leads is not None else DECLINE_TEXT_NO_OFFER

    @staticmethod
    def _offered_callback(conversation) -> bool:
        """Whether the assistant's last turn offered to get in touch.

        Read from the transcript rather than a flag: the buyer consented to
        what they saw on screen, and the transcript is the only record of that.
        """
        for turn in reversed(conversation.turns):
            if turn.role == "assistant":
                return OFFER_PHRASE in (turn.content or "").lower()
        return False

    def _interest(self, conversation) -> list[str]:
        """Products mentioned anywhere in this conversation.

        A lead with no product interest is a name and a number; one that says
        which machine the buyer asked about is a call a rep can make.
        """
        if self.answers is None:
            return []
        found: list[str] = []
        for turn in conversation.turns:
            if turn.role != "user":
                continue
            resolution = self.answers.resolver.resolve(turn.content or "")
            if resolution.ok and resolution.product is not None:
                product_id = resolution.product.product_id
                if product_id not in found:
                    found.append(product_id)
        return found

    def _details(self, conversation, message: str) -> dict:
        """Lead details gathered from the **whole conversation**.

        Extracting from the current message alone loses anything the buyer said
        earlier: "I am Dr Pradeep" then, three turns later, "call me on 98200
        11122" produced a lead with a phone number and no name. Earlier turns
        are read first so the most recent statement wins on any field the buyer
        has since corrected.
        """
        from .leads import extract_lead_details

        merged: dict = {}
        for turn in conversation.turns:
            if turn.role == "user":
                merged.update({k: v for k, v in extract_lead_details(turn.content or "").items() if v})
        merged.update({k: v for k, v in extract_lead_details(message or "").items() if v})
        return merged

    def _known_name(self, conversation, message: str) -> str:
        """The buyer's name, from this message or any earlier one."""
        return str(self._details(conversation, message).get("name") or "").strip()

    def _lead(self, message: str, conversation, intent) -> ChatReply | None:
        """Consent, and contact details. Returns ``None`` when neither applies.

        Two distinct moments, and conflating them is why this was broken:

        * The buyer **agrees** to be contacted but has given no details yet.
          There is nothing to capture — ``LeadService.capture`` deliberately
          refuses to create a row without a phone, an email or a name, because
          empty leads make the pipeline unworkable. So the reply asks for a
          number, and the details arrive on a later turn.
        * The buyer **supplies** details. That is capturable, whether or not
          they were asked for.
        """
        if self.leads is None:
            return None

        details = self._details(conversation, message)
        contactable = bool(details.get("phone") or details.get("email"))

        if not contactable:
            # Two conditions, and both are required. An outstanding offer alone
            # is not consent: after the assistant asked "would you like us to
            # get in touch?", the *next* message was being read as agreement
            # whatever it said — so "who won the world cup?" was answered with
            # "Of course. What's the best number or email for you?" The buyer
            # must actually agree, and only the classifier can say whether they
            # did.
            from .orchestrator import Intent

            if intent is Intent.CONSENT and self._offered_callback(conversation):
                return ChatReply(
                    text=ASK_CONTACT_TEXT,
                    conversation_id=conversation.conversation_id,
                    intent="consent",
                )
            return None

        lead = self.leads.capture(
            message,
            channel="web_chat",
            conversation_id=conversation.conversation_id,
            products=self._interest(conversation),
            # Details gathered across the whole conversation, not just this
            # message. A buyer who says "I am Dr Pradeep" and three turns later
            # gives a number produced a lead with no name, because only the
            # current message was ever parsed.
            explicit=details,
        )
        if lead is None:
            # capture() found nothing usable — do not claim otherwise.
            return None

        return ChatReply(
            text=LEAD_CAPTURED_TEXT,
            conversation_id=conversation.conversation_id,
            intent="consent",
            escalation=lead.qualification.get("tier") in {"hot", "warm"},
        )

    def _commercial(self, message: str, conversation) -> ChatReply | None:
        """Answer a commercial question from the deterministic engine.

        Returns ``None`` to fall through to retrieval when the engine cannot
        help — an unrecognised fact, or a product it cannot identify. Falling
        through is deliberate: retrieval may still find the answer in prose, and
        a question the engine cannot parse is not a question to refuse.
        """
        from .answers import fact_kind_for

        kind = fact_kind_for(message)
        if kind is None:
            return None

        resolution = self.answers.resolver.resolve(message)
        base = ChatReply(text="", conversation_id=conversation.conversation_id,
                         intent="commercial")

        if resolution.kind.value == "ambiguous":
            # Two "Medical Dry Imager" products at different prices. Guessing
            # one would quote a real price for the wrong product.
            base.text = resolution.clarify()
            return base

        if not resolution.ok or resolution.product is None:
            # The fact was recognised — they want a price — but no product was.
            # Declining is the only honest answer, and falling through to
            # retrieval is actively worse: asked for "the price of the MRI
            # machine you sell", retrieval returned a cassette FAQ about X-ray
            # procedure. That tells the buyer we sell MRI machines and then
            # answers a question they did not ask. There is no correct price for
            # an unidentifiable product.
            base.text = DECLINE_TEXT
            base.declined = True
            return base

        answer = self.answers.commercial(kind, resolution.product)
        # render() carries the price caveat with the number, so it cannot be
        # separated from it here — the two are produced by the same call.
        base.text = answer.render()
        base.grounded = answer.found
        base.source_url = answer.source_url
        # An unpublished fact is an answer, not a failure: the engine says so in
        # words, and marking it unavailable keeps the turn out of the "answered
        # from the catalogue" count.
        base.unavailable = not answer.found
        return base

    def _answer(self, intent, message: str, conversation) -> ChatReply:
        from .orchestrator import Intent

        base = ChatReply(text="", conversation_id=conversation.conversation_id, intent=intent.value)

        if intent is Intent.CLINICAL:
            base.text = CLINICAL_TEXT
            return base

        # Consent and contact details, checked before the declines — "yes" must
        # not be answered with another decline.
        lead_reply = self._lead(message, conversation, intent)
        if lead_reply is not None:
            return lead_reply

        # Greetings, self-identification, thanks. Answered as a person would
        # answer them — by name where the buyer has given one — rather than
        # with a catalogue refusal.
        if intent is Intent.INTRODUCTION:
            name = self._known_name(conversation, message)
            base.text = greeting_text(name, message=message)
            return base

        # Consent that is not actionable — "yes please" with nothing offered.
        # Without this branch it fell through to retrieval, was gated out, and
        # answered with the catalogue decline: the same not-a-question hole as
        # the greetings, one branch further down.
        if intent is Intent.CONSENT:
            base.text = (
                "Happy to help — what would you like to know? You can ask about "
                "a product's specifications, price, minimum order quantity or "
                "delivery time."
            )
            return base

        if intent is Intent.OFF_TOPIC:
            # Ask the grounding gate before declining. The classifier is a
            # *router* — it decides which branch to try — but it is not the
            # authority on whether a question is about the catalogue, and
            # treating it as one made it a second refuser that ran before the
            # first could speak.
            #
            # "contrast media compatibility" is a specification field name that
            # appears verbatim in ANITA's corpus, and it matched no classifier
            # pattern, so it was declared off-topic and declined — while the
            # gate, whose whole job is this question, was never consulted. The
            # gate was fixed to admit spec names earlier; the fix was invisible
            # because this branch refused first.
            #
            # Retrieval now gets its turn. An off-topic question the gate also
            # refuses still declines, so "who won the world cup?" is unaffected.
            # To overrule the classifier the evidence must be *positive*: a term
            # from the catalogue's own vocabulary, not merely a vector that
            # happens to sit nearby.
            #
            # This distinction is measured. "contrast media compatibility" is a
            # specification field name and matches on `entity_match`. "do you
            # sell MRI machines?" scores 0.666 dense similarity against a
            # *Medical Dry Laser Imager* FAQ — above the 0.62 threshold, and
            # nothing to do with MRI. Acting on similarity alone answered an
            # off-topic question about a product the catalogue does not sell,
            # and in doing so skipped the decline that offers a callback, which
            # broke lead capture (caught by the gate's `lead:consent` case).
            if self.retriever is not None:
                probe = self.retriever.retrieve(message)
                if probe.ok and probe.decision.entity_matched:
                    base.text = _named(
                        probe.chunks[0].chunk.product_name or "", probe.chunks[0].chunk.text
                    )
                    base.grounded = True
                    base.source_url = probe.chunks[0].chunk.source_url
                    base.intent = Intent.CATALOGUE.value
                    return base
            base.text = DECLINE_TEXT
            base.declined = True
            return base

        if intent is Intent.HELP:
            base.text = HELP_TEXT
            return base

        # What we sell. Answered from the corpus rather than a canned list, so
        # it cannot drift from the catalogue the rest of the assistant answers
        # from — and it names categories, which is how a buyer navigates a
        # catalogue this size. A list of 24 product names would be a wall.
        if intent is Intent.LISTING and self.answers is not None:
            product_count = self.answers.product_count()
            categories: dict[str, int] = {}
            for product in self.answers.corpus.all_products():
                if product.category:
                    categories[product.category] = categories.get(product.category, 0) + 1
            if categories:
                listed = "; ".join(
                    f"{name} ({count})"
                    for name, count in sorted(categories.items(), key=lambda kv: -kv[1])
                )
                # The count sentence comes from the corpus, so it cannot drift.
                # It already ends in a full stop; the breakdown is appended to
                # it rather than wrapped around it.
                lead = product_count.text.rstrip().rstrip(".")
                base.text = (
                    f"{lead}, in {len(categories)} categories: {listed}. "
                    f"Ask about any one for its specifications, price or delivery time."
                )
                base.grounded = product_count.found
                return base
            base.text = product_count.render()
            base.grounded = product_count.found
            return base

        # Who we are. The corpus holds the statutory detail and the branches,
        # and none of it was reachable: every one of these questions was
        # classified OFF_TOPIC and declined.
        if intent is Intent.COMPANY and self.answers is not None:
            from .answers import company_kind_for

            kind = company_kind_for(message)
            if kind is not None:
                answer = self.answers.company_fact(kind)
                base.text = answer.render()
                base.grounded = answer.found
                base.source_url = answer.source_url
                base.unavailable = not answer.found
                return base
            # No specific kind — "what brands do you deal in?" lives in the
            # company profile prose, so retrieval gets its turn.
            if self.retriever is not None:
                result = self.retriever.retrieve(message)
                if result.ok and result.chunks:
                    base.text = result.chunks[0].chunk.text
                    base.grounded = True
                    base.source_url = result.chunks[0].chunk.source_url
                    return base
            base.text = DECLINE_TEXT
            base.declined = True
            return base

        # A commercial question goes to the deterministic engine, not to
        # retrieval. The intent exists precisely because a price is a fact in a
        # column, and the answer path has to honour that: asked for a price, a
        # chunk-ranked FAQ is a wrong answer even though every word of it is
        # verbatim from the catalogue.
        if intent is Intent.COMMERCIAL and self.answers is not None:
            commercial = self._commercial(message, conversation)
            if commercial is not None:
                return commercial

        if self.retriever is None:
            # No retrieval wired: decline rather than let a model improvise.
            base.text = self._decline()
            base.unavailable = True
            base.declined = True
            return base

        result = self.retriever.retrieve(message)
        if not result.ok:
            base.text = DECLINE_TEXT
            base.declined = True
            return base

        base.grounded = True
        top = result.chunks[0]
        base.source_url = top.chunk.source_url

        # Comparison. Checked before the single-product path, which would
        # report two matches as ambiguous and ask the buyer to choose between
        # the two products they just asked us to compare.
        if self.answers is not None and _is_comparison(message):
            pair = self.answers.resolver.resolve_pair(message)
            if pair is not None:
                answer = self.answers.compare(*pair)
                base.text = answer.render()
                base.grounded = answer.found
                base.source_url = answer.source_url
                base.unavailable = not answer.found
                return base

        if intent is Intent.CATALOGUE and self.answers is not None:
            # The product the *question* names wins over the product retrieval
            # ranked first. Those disagree more often than not: asked about the
            # X-Ray Cassette, retrieval returned an Intensifying Screen chunk —
            # a different product, answered confidently. Retrieval is a good
            # way to find the relevant page and a poor way to decide which
            # product was asked about, which is a thing a name can settle.
            from .answers import FactKind, ResolutionKind

            resolution = self.answers.resolver.resolve(message)
            if resolution.kind is ResolutionKind.AMBIGUOUS:
                base.text = resolution.clarify()
                base.grounded = False
                return base

            product = resolution.product if resolution.ok else None
            if product is None and top.chunk.product_id and result.decision.entity_matched:
                # Nothing named in the question, but the question does contain a
                # term from the catalogue's own vocabulary — so it is about the
                # catalogue and retrieval's top hit is a fair reading of it.
                #
                # The entity match is required, and the requirement is measured.
                # Without it, "do you sell MRI machines?" reached this line:
                # no product resolves, but a *Medical Dry Laser Imager* FAQ sits
                # 0.666 similar, clears the 0.62 threshold, and the buyer is
                # answered about a device the catalogue does not sell and they
                # did not ask about. The same question previously declined and
                # offered a callback, so the wrong answer also silently removed
                # the lead-capture path — which is how the release gate caught
                # it.
                product = self.answers.corpus.get(top.chunk.product_id)

            if product is not None:
                # A published FAQ about the same product is exact and answers
                # the question as the site itself words it — but only when the
                # question is not asking for the specification list. The FAQ
                # chunks outrank the spec chunks on almost every query, so
                # without this guard "tell me about the X-Ray Cassette
                # specifications" was answered with a FAQ about X-ray
                # procedure: right product, wrong question.
                from .answers import fact_kind_for

                spec_seeking = fact_kind_for(message) is FactKind.SPEC_LIST
                if (not spec_seeking
                        and top.chunk.product_id == product.product_id
                        and top.chunk.chunk_type == "product_faq"):
                    # Name the subject. A published FAQ is verbatim from the
                    # site and reads "What is the typical process…? During an
                    # X-ray…" — it never says which product it describes, and
                    # this catalogue holds two products with the same name. An
                    # answer a buyer cannot attribute is an answer they cannot
                    # use, and a rep cannot check.
                    base.text = _named(product.name, top.chunk.text)
                    return base
                base.text = self.answers.spec_list(product).text
                base.source_url = product.url or base.source_url
                return base

            # A catalogue-labelled question that names no catalogue product and
            # contains no catalogue term is not a catalogue question, whatever
            # the router called it. Answering it from the nearest chunk is how
            # "do you sell MRI machines?" was answered with a Medical Dry Laser
            # Imager FAQ — a device ANITA does not sell, that the buyer did not
            # ask about.
            #
            # Declining also restores the offer of a callback, which is what
            # makes this a lead rather than a wrong answer.
            if not result.decision.entity_matched:
                base.text = self._decline()
                base.declined = True
                return base

        base.text = top.chunk.text
        if top.chunk.product_name:
            base.text = _named(top.chunk.product_name, base.text)
        return base


class BrowserMicSurface:
    """Hands out a voice session ticket. The audio itself runs on the WS path."""

    def __init__(self, admission) -> None:
        self.admission = admission

    def post_ticket(self, *, session_id: str) -> Response:
        import uuid

        decision = self.admission.admit(f"browser-{session_id}")
        if not decision.admitted:
            # A refusal is a designed outcome, delivered with what to do next.
            return Response(503, {
                "admitted": False,
                "message": decision.message,
                "reason": decision.reason,
            })
        return Response(200, {
            "admitted": True,
            "ticket": str(uuid.uuid4()),
            "level": decision.level.value,
        })


# ---------------------------------------------------------------------------
# Admin console
# ---------------------------------------------------------------------------
@dataclass
class ChannelStatus:
    name: str
    healthy: bool
    detail: str = ""
    unavailable: bool = False

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "healthy": None if self.unavailable else self.healthy,
            "detail": self.detail,
            "unavailable": self.unavailable,
        }


class AdminSurface:
    """Channel health, conversation review, metrics, and the release gate.

    Every read returns *something usable* when its source is unavailable: an
    explicit ``unavailable`` rather than an empty list or a zero. An operator
    looking at an empty conversation list cannot tell "nobody called" from
    "the database is down", and those demand opposite responses.
    """

    def __init__(self, *, registry=None, gateway=None, admission=None,
                 metrics=None, gate=None, traces=None) -> None:
        self.registry = registry
        self.gateway = gateway
        self.admission = admission
        self.metrics = metrics
        self.gate = gate
        self.traces = traces

    # -- health ------------------------------------------------------------
    def channels(self) -> Response:
        if self.gateway is None:
            return Response(503, {
                "unavailable": True,
                "reason": "channel gateway not wired",
                "channels": [],
            })
        described = self.gateway.describe()
        statuses = [
            ChannelStatus(name=name, healthy=True, detail="registered").as_dict()
            for name in described["channels"]
        ]
        return Response(200, {
            "channels": statuses,
            "signature_validation": described["signature_validation"],
            "rate_limit": described["rate_limit"],
        })

    # -- conversations -----------------------------------------------------
    def conversations(self, *, limit: int = 50) -> Response:
        if self.registry is None:
            return Response(503, {"unavailable": True, "reason": "conversation registry not wired",
                                  "conversations": []})
        conversations = list(self.registry._by_key.values())[:limit]  # noqa: SLF001
        return Response(200, {
            "count": len(conversations),
            "conversations": [conversation.as_dict() for conversation in conversations],
        })

    def conversation(self, *, conversation_id: str) -> Response:
        if self.registry is None:
            return Response(503, {"unavailable": True, "reason": "registry not wired"})
        for conversation in self.registry._by_key.values():  # noqa: SLF001
            if conversation.conversation_id == conversation_id:
                return Response(200, {
                    "conversation_id": conversation_id,
                    "state": conversation.state.value,
                    "channels_seen": conversation.channels_seen,
                    "turns": [
                        {
                            "role": turn.role,
                            "content": turn.content,
                            "intent": turn.intent.value if turn.intent else None,
                            "failed": turn.failed,
                            "at": turn.at,
                        }
                        for turn in conversation.turns
                    ],
                })
        return Response(404, {"error": "no such conversation"})

    # -- metrics -----------------------------------------------------------
    def metrics_view(self) -> Response:
        if self.metrics is None:
            return Response(503, {"unavailable": True, "reason": "metrics not wired",
                                  "metrics": []})
        return Response(200, {"metrics": self.metrics.render_all()})

    # -- release gate ------------------------------------------------------
    def run_gate(self, answerer: Callable[[Any], str]) -> Response:
        """Run the frozen evaluation set. Requires authentication."""
        if self.gate is None:
            return Response(503, {"unavailable": True, "reason": "quality gate not wired"})
        report = self.gate.evaluate(answerer)
        return Response(200, report.as_dict())


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
class Router:
    """Dispatches a route to its handler, enforcing the auth boundary.

    The auth check happens here, once, for every route — so a new admin endpoint
    cannot be added without one by forgetting a decorator. The table decides.
    """

    def __init__(self, *, admin_token: str = "") -> None:
        self.admin_token = admin_token
        self._handlers: dict[Route, Callable[..., Response]] = {}
        self._lock = threading.Lock()
        self.calls: list[dict] = []

    def register(self, route: Route, handler: Callable[..., Response]) -> None:
        with self._lock:
            self._handlers[route] = handler

    def dispatch(self, route: Route, *, token: str | None = None, **kwargs) -> Response:
        try:
            authenticate(route, token, self.admin_token)
        except AuthError as exc:
            self.calls.append({"route": route.value, "status": 401})
            return Response(401, {"error": str(exc)})

        handler = self._handlers.get(route)
        if handler is None:
            self.calls.append({"route": route.value, "status": 404})
            return Response(404, {"error": f"no handler for {route.value}"})

        try:
            response = handler(**kwargs)
        except TypeError as exc:
            self.calls.append({"route": route.value, "status": 400})
            return Response(400, {"error": f"bad request: {exc}"})
        except Exception as exc:  # noqa: BLE001 - a surface must not 500 silently
            self.calls.append({"route": route.value, "status": 500})
            return Response(500, {"error": f"{type(exc).__name__}"})

        # A handler that returns something other than a Response is a wiring
        # bug, and it must surface as a 500 rather than as an AttributeError
        # raised while *logging* the call — which would escape the router
        # entirely and reach the client as an unhandled failure.
        if not isinstance(response, Response):
            self.calls.append({"route": route.value, "status": 500})
            return Response(500, {
                "error": f"handler for {route.value} returned "
                         f"{type(response).__name__}, expected Response"
            })

        self.calls.append({"route": route.value, "status": response.status})
        return response

    def contract(self) -> dict:
        """The route table, for review and for the console's own documentation."""
        return {
            route.value: {
                "surface": surface.value,
                "authenticated": needs_auth,
            }
            for route, (surface, needs_auth) in ROUTE_TABLE.items()
        }


def build_router(*, chat: ChatSurface, mic: BrowserMicSurface, admin: AdminSurface,
                 admin_token: str = "", gate_answerer: Callable[[Any], str] | None = None) -> Router:
    """Wire the three surfaces into one router.

    ``gate_answerer`` is bound **here, on the server**, and never taken from the
    request. The release gate is only meaningful if the answers it judges come
    from the live answer path: a caller who could supply their own answerer
    could return "ok" for every case and force a pass, which is precisely the
    failure the gate exists to prevent.
    """
    router = Router(admin_token=admin_token)
    router.register(Route.HEALTH, lambda: Response(200, {"status": "ok"}))
    router.register(Route.CHAT_MESSAGE, chat.post_message)
    router.register(Route.CHAT_HISTORY, chat.get_history)
    router.register(Route.VOICE_TICKET, mic.post_ticket)
    router.register(Route.ADMIN_CHANNELS, admin.channels)
    router.register(Route.ADMIN_CONVERSATIONS, admin.conversations)
    router.register(Route.ADMIN_CONVERSATION, admin.conversation)
    router.register(Route.ADMIN_METRICS, admin.metrics_view)

    if gate_answerer is None:
        # No live answer path bound: say so rather than run the gate against
        # something improvised, which would produce a green report that means
        # nothing.
        router.register(Route.ADMIN_GATE, lambda: Response(503, {
            "unavailable": True,
            "reason": "no answerer bound to the release gate",
        }))
    else:
        router.register(Route.ADMIN_GATE, lambda: admin.run_gate(gate_answerer))

    return router
