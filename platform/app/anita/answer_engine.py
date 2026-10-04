"""The answer decision tree, shared by every channel that answers a buyer.

Extracted from `ChatSurface._answer` (US-001 Phase C). It lived inside the web
chat surface, which meant the voice path could not reach it: `voice.py` was
copied from the source engine with its own LLM-first composition, so a caller
and a chat user asking the identical question would have been answered by two
different decision trees. The voice adapters did retrieval only, and returned
raw chunk text where chat returned a rendered answer.

Two things make the extraction worth the churn:

* **One source of truth.** Greetings, declines, the clinical refusal, the
  price caveat and the ambiguity question are all product behaviour, not chat
  behaviour. Duplicating them for voice means they drift, and the drift is
  invisible until a caller hears something a web buyer never would.
* **The evaluation set covers voice for free.** The frozen 70-case set and the
  50 browser scenarios drive this tree. Reusing it is what makes the voice
  path's answers testable at all — a separate voice composition would need its
  own eval set before it could be trusted with a real phone call.

`ChatSurface` keeps a thin `_answer` that delegates here, so the surface's
callers and tests are unaffected.

The LLM is deliberately *not* on this path. `AnswerEngine` answers from the
corpus and the deterministic engine, and returns a decline where it cannot.
That is `BRD-04` (no context, no answer) and `BRD-05` (no generated text about
price) holding structurally rather than by prompt discipline.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("anita.answer_engine")


# ---------------------------------------------------------------------------
# The reply shape
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
    # US-012: every catalogue fact the answer used, as citations
    # (`chunk_id` / `source_url` / `chunk_type` / content hash where a chunk is
    # involved, the record id where a corpus row is). The reply that states a
    # figure without one is a composition defect, so the count is part of the
    # payload rather than a claim in a docstring.
    grounded_on: list[dict] = field(default_factory=list)
    #: True when a citation could not be completed (a chunk with no
    #: `source_url`). Counted rather than swallowed — the release gate fails on
    #: a non-zero count over the frozen set (`TAC-1`).
    citation_incomplete: bool = False

    def cite(self, citation: dict | None) -> None:
        """Attach one citation, flagging an incomplete one.

        A *record* citation (`company:branches`, `product:270694`) is complete
        by construction — the record id is what resolves it, and the story asks
        for exactly that for statutory facts (`TAC-6`). The incomplete flag is
        for a **chunk** citation with no `source_url`: the answer was composed
        from a retrieved passage whose provenance is a dead end, which is the
        condition the release gate must fail on (`TAC-1`, edge 10).
        """
        if not citation:
            return
        self.grounded_on.append(citation)
        if str(citation.get("chunk_type") or "") in ("product_record", "company_record"):
            return
        if not citation.get("source_url"):
            self.citation_incomplete = True

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
            "grounded_on": self.grounded_on,
            "citation_incomplete": self.citation_incomplete,
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


#: Words that ask a commercial question without naming anything to ask it
#: about. A message made only of these (plus stopwords) is a follow-up —
#: "and the price?", "what about the MOQ?" — and may be answered from the
#: product the conversation was already about. The list is deliberately
#: generous: an unrecognised word here costs a wrong answer about the wrong
#: product, while omitting one only costs a decline.
_FOLLOW_UP_WORDS = frozenset("""
what whats whatever and the for of is are was were it that this these those about again
please tell give show me my your you we us i do does did done how much many price pricing
cost rate quote quotation discount moq minimum order ordering quantity delivery lead time
shipping supply ability sample available availability payment invoice unit units piece
pieces box boxes pack packs buy purchase sell selling have has had any there on in to a an
can could would should will give get need want more also same one that's whats and/or
""".split())
_WORD_RE = re.compile(r"[a-z0-9]+")

# What a question is asking about, for the absence check. Specific first: a
# question that mentions both ("tell me about the specifications") is a
# specification question.
_SPEC_ASK = re.compile(
    r"\b(specificat\w*|specs?|technical (details|data|specs?)|"
    r"what (does|do) (it|they|the .*?) weigh|weight|dimensions?|"
    r"materials?|size|colou?r|models?|features?)\b", re.I)
_ABOUT_ASK = re.compile(
    r"\b(tell me about|about the|more about|describe|description|overview|"
    r"what (is|are) (the )?.*\bused for|used for|what does .* do)\b", re.I)


def _asked_about(message: str) -> str | None:
    """``"specs"``, ``"about"``, or None when the question asks for neither."""
    text = message or ""
    if _SPEC_ASK.search(text):
        return "specs"
    if _ABOUT_ASK.search(text):
        return "about"
    return None


def _names_no_product(message: str) -> bool:
    """Whether a message asks a fact question without naming anything.

    Used to decide whether a commercial follow-up may inherit the product the
    conversation was already about. Any content word outside the fact/filler
    vocabulary counts as an attempt to name a product — so "the price of the
    MRI machine you sell" does *not* inherit, and declines as it always did.
    """
    words = {word for word in _WORD_RE.findall((message or "").casefold())
             if len(word) >= 3}
    return not (words - _FOLLOW_UP_WORDS)


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


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------
class AnswerEngine:
    """Resolves a question into a reply, from the corpus and the deterministic
    engine. Holds no transport and no channel state.

    Every dependency is optional and injected. `answers` is the deterministic
    fact engine (MOD-04), `retriever` is the grounded retrieval path (MOD-03),
    and `leads` is MOD-06. With all three absent the engine declines rather
    than improvising — the same rule as with one absent.
    """

    def __init__(self, answers=None, retriever=None, leads=None, llm=None,
                 probe=None) -> None:
        self.answers = answers
        self.retriever = retriever
        # US-014/US-015: the prose backend, when one is configured. It can only
        # *add* an answer — every failure path below returns None and leaves
        # the deterministic or verbatim reply exactly as it was.
        self.llm = llm
        # MOD-06. Optional: without it the assistant still answers, it just
        # cannot take a lead — and it says so rather than promising a callback
        # it has nowhere to record.
        self.leads = leads
        # US-012: what the catalogue has published, per product. Optional so an
        # engine built without a corpus (tests, the voice probe) behaves as it
        # did; when present it turns "not in our catalogue" into the honest
        # "not published" for the seven products the source site leaves blank.
        self.probe = probe

    # -- public ------------------------------------------------------------
    def answer(self, intent, message: str, conversation) -> ChatReply:
        """The decision tree. Order is behaviour, not style.

        Consent and contact details are checked before the declines, greetings
        before retrieval, and the deterministic engine before the ranked
        chunks — each ordering exists because the reverse produced a specific,
        observed wrong answer, and the comment at each branch says which.
        """
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
                    self._cite_chunks(base, probe)
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
                # US-012: the count and the breakdown are catalogue facts, so
                # they cite the catalogue. The frozen-set citation check caught
                # this branch as the one grounded answer with no provenance.
                if base.grounded:
                    base.cite({
                        "chunk_id": "catalogue:products",
                        "chunk_type": "product_record",
                        "product_id": None,
                        "product_name": None,
                        "source_url": product_count.source_url or None,
                        "content_hash": "",
                    })
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
                if answer.found:
                    base.cite(self._company_citation(answer, kind))
                return base
            # No specific kind — "what brands do you deal in?" lives in the
            # company profile prose, so retrieval gets its turn.
            if self.retriever is not None:
                result = self.retriever.retrieve(message)
                if result.ok and result.chunks:
                    base.text = result.chunks[0].chunk.text
                    base.grounded = True
                    base.source_url = result.chunks[0].chunk.source_url
                    self._cite_chunks(base, result)
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

        # US-012. Checked *before* retrieval, because the honest answer here is
        # a fact about the catalogue's coverage rather than anything a ranked
        # chunk could supply — and because retrieval on a product with no
        # published specifications finds nothing and declines, telling the
        # buyer we do not stock what we do stock. Composed from the probe, so
        # the LLM invocation count for this turn is 0 (`TAC-4`).
        if intent is Intent.CATALOGUE:
            absence = self._not_published(message, base)
            if absence is not None:
                return absence

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
        self._cite_chunks(base, result)

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
                for product in pair:
                    base.cite(self._product_citation(product))
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
                base.grounded = True
                base.cite(self._product_citation(product))
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

        # US-014: the one place generated prose is allowed — an open question
        # whose answer would otherwise be the top chunk verbatim. Everything
        # above this line is deterministic or name-resolved; nothing here can
        # replace a figure or a caveat, because the prose is rejected unless
        # it passes the numeric-grounding check against the same chunks.
        prose = self._compose_prose(message, result.chunks, conversation)
        if prose is not None:
            base.text = prose
            return base

        base.text = top.chunk.text
        if top.chunk.product_name:
            base.text = _named(top.chunk.product_name, base.text)
        return base

    # -- helpers -----------------------------------------------------------
    def _compose_prose(self, question: str, chunks, conversation) -> str | None:
        """Generated prose for an open question, or None (US-014 / US-015).

        Deterministic-first is the rule of record: this is called only where
        the reply is otherwise the top chunk verbatim, and **every** failure —
        no backend, the breaker open, a timeout, an empty generation, or a
        grounding rejection — returns None and leaves the caller with exactly
        what it would have said before. The model can add an answer; it can
        never remove one.
        """
        if self.llm is None:
            return None
        try:
            from .prompts import build_prompt, verify_answer

            context = "\n\n".join(chunk.chunk.text for chunk in chunks[:3])
            history = [
                {"role": turn.role, "content": turn.content}
                for turn in list(getattr(conversation, "turns", ()))[-6:]
            ]
            bundle = build_prompt(question, context, history=history)
            result = self.llm.generate(bundle.messages(), stream=False)
            text = (getattr(result, "text", "") or "").strip()
            if not text:
                return None

            # The check is on the OUTPUT, against the same context the prompt
            # carried: a prompt that says "only use the context" is an
            # instruction a model can drift from, and this is the control that
            # stands between a drifted figure and a buyer (BRD-05/BRD-06).
            check = verify_answer(text, context, question=question, require_caveat=True)
            if not check.grounded:
                logger.warning(
                    "prose rejected by the grounding check: %s", check.violations
                )
                return None
            return text
        except Exception as exc:  # noqa: BLE001 - the verbatim answer still stands
            logger.info("prose unavailable (%s); answering verbatim", type(exc).__name__)
            return None
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

    def _not_published(self, message: str, base: ChatReply) -> ChatReply | None:
        """The honest absence, when the asked-about block is not published.

        Returns ``None`` — leaving the normal path to run — when there is no
        probe, when the question names no product, when the product publishes
        what was asked for, or when two same-named products *disagree*. That
        last case is the point of the guard: reporting one sibling's absence
        for a question about the other would be the inference this story
        exists to prevent, so the buyer is asked which one they mean instead.
        """
        from .coverage import Availability, NOT_PUBLISHED_ABOUT, NOT_PUBLISHED_SPECS
        from .coverage import UnknownProduct
        from .orchestrator import Intent

        if self.probe is None or self.answers is None:
            return None
        wanted = _asked_about(message)
        if wanted is None:
            return None

        resolution = self.answers.resolver.resolve(message)
        candidates = []
        if resolution.ok and resolution.product is not None:
            candidates = [resolution.product]
        elif resolution.kind.value == "ambiguous":
            candidates = list(resolution.candidates)
        if not candidates:
            return None

        def unpublished(product) -> bool:
            try:
                availability = self.probe.for_product(product.product_id)
            except UnknownProduct:
                return False
            aspect = getattr(availability, wanted)
            return aspect is Availability.NOT_PUBLISHED

        if not all(unpublished(product) for product in candidates):
            return None

        product = candidates[0]
        template = NOT_PUBLISHED_SPECS if wanted == "specs" else NOT_PUBLISHED_ABOUT
        # The plain name, deliberately: with two same-named products the
        # disambiguator would pick one of them, and neither is the answer —
        # the statement is true of both, and naming one would be the sibling
        # selection this branch exists to avoid.
        base.text = template.format(product=product.label())
        base.intent = Intent.CATALOGUE.value
        # An absence is a statement about the catalogue, so it is grounded —
        # in the product record that is missing the data, which is checkable.
        base.grounded = True
        base.cite(self._product_citation(product))
        return base

    @staticmethod
    def _product_citation(product, *, source_url: str | None = None) -> dict:
        """A citation for an answer that came from a corpus row, not a chunk."""
        return {
            "chunk_id": f"product:{product.product_id}",
            "chunk_type": "product_record",
            "product_id": product.product_id,
            "product_name": product.name,
            "source_url": source_url or product.url or None,
            "content_hash": "",
        }

    @staticmethod
    def _company_citation(answer, kind: str) -> dict:
        """A citation for a company fact — the collected record it came from.

        `Answer.grounded_on` names the corpus table and field the value was read
        from (`company_ids[phone]`, `branches`, …), which is the checkable
        source for a statutory claim (`TAC-6`).
        """
        record = str(getattr(answer, "grounded_on", "") or f"company[{kind}]")
        return {
            "chunk_id": f"company:{record}",
            "chunk_type": "company_record",
            "product_id": None,
            "product_name": None,
            "source_url": getattr(answer, "source_url", None) or None,
            "content_hash": "",
            "record": record,
        }

    @staticmethod
    def _cite_chunks(base: ChatReply, result) -> None:
        """Attach every chunk the answer was composed from (`TAC-1`).

        Prefers the result's own `provenance` (the TRD-16 payload) and derives
        the same shape from the chunks when a retriever-shaped object does not
        provide it — the engine duck-types its retriever deliberately, so a
        citation must not be the one thing that requires a concrete class.
        """
        provenance = getattr(result, "provenance", None)
        if provenance is None:
            provenance = []
            for scored in getattr(result, "chunks", ()) or ():
                chunk = getattr(scored, "chunk", scored)
                provenance.append({
                    "chunk_id": getattr(chunk, "chunk_id", "") or "",
                    "chunk_type": getattr(chunk, "chunk_type", "") or "",
                    "product_id": getattr(chunk, "product_id", None),
                    "product_name": getattr(chunk, "product_name", None),
                    "source_url": getattr(chunk, "source_url", None),
                    "content_hash": getattr(chunk, "content_hash", "") or "",
                })
        for citation in provenance:
            base.cite(citation)

    def _follow_up_product(self, conversation):
        """The last product this conversation was actually about.

        Scans the buyer's turns newest-first and returns the first product the
        resolver can identify. Cross-channel context turns are included on
        purpose: a buyer who asked about a machine on web chat and then calls
        is exactly the case this exists for (`US-004 AC-1`).
        """
        if self.answers is None:
            return None
        for turn in reversed(getattr(conversation, "turns", ()) or ()):
            if turn.role != "user":
                continue
            resolution = self.answers.resolver.resolve(turn.content or "")
            if resolution.ok and resolution.product is not None:
                return resolution.product
        return None

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

        **Turns seeded as cross-channel context are skipped** (US-004). They
        belong to another conversation and are carried in so the buyer does not
        have to repeat which product they meant — not so their details are
        captured a second time. Without this, a buyer who gave a number on web
        chat and then asked a *product* question by phone was answered with
        "Thank you — I've passed your details", because the context turn still
        contained the number.
        """
        from .leads import extract_lead_details

        merged: dict = {}
        for turn in conversation.turns:
            if turn.role == "user" and not getattr(turn, "external", False):
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

        # US-004: remember the lead on the conversation. The orchestrator reads
        # this to write the identity link, so a later call from the number the
        # buyer just gave resolves to the same lead (`BRD-02`, `AC-1`).
        try:
            conversation.lead_id = lead.lead_id
            conversation.lead_phone = lead.phone
            conversation.lead_email = lead.email
        except Exception:  # noqa: BLE001 - a read-only conversation is not a failure
            pass

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
        from .answers import Resolution, ResolutionKind, fact_kind_for

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
            # US-002 AC-2 / US-004 AC-1: a follow-up fact question — "and the
            # price?", "what about the MOQ?" — names no product because the
            # product was named a moment ago, possibly in another channel.
            # Resolving it from the conversation is what a human does; the
            # alternative, refusing, is what made a working assistant look
            # broken on the second question of every conversation.
            #
            # The guard matters as much as the fallback: a message that names
            # a product we cannot match ("the MRI machine you sell") must still
            # decline rather than answer about whatever was discussed earlier.
            # The fallback therefore fires only when the message names nothing
            # beyond fact words and filler.
            follow_up = (
                self._follow_up_product(conversation)
                if _names_no_product(message) else None
            )
            if follow_up is None:
                # The fact was recognised — they want a price — but no product
                # was. Declining is the only honest answer, and falling through
                # to retrieval is actively worse: asked for "the price of the
                # MRI machine you sell", retrieval returned a cassette FAQ
                # about X-ray procedure. That tells the buyer we sell MRI
                # machines and then answers a question they did not ask. There
                # is no correct price for an unidentifiable product.
                base.text = DECLINE_TEXT
                base.declined = True
                return base
            resolution = Resolution(ResolutionKind.RESOLVED, product=follow_up,
                                    query=message)

        answer = self.answers.commercial(kind, resolution.product)
        # render() carries the price caveat with the number, so it cannot be
        # separated from it here — the two are produced by the same call.
        base.text = answer.render()
        base.grounded = answer.found
        base.source_url = answer.source_url
        if answer.found:
            base.cite(self._product_citation(resolution.product,
                                             source_url=answer.source_url))
        # An unpublished fact is an answer, not a failure: the engine says so in
        # words, and marking it unavailable keeps the turn out of the "answered
        # from the catalogue" count.
        base.unavailable = not answer.found
        return base
