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
    ADMIN_HANDOFFS = "GET /api/admin/handoffs"
    # US-002: the outbound send queue. A buyer whose answer the carrier would
    # not carry is an operator's problem, and this is where they see it.
    ADMIN_SENDS = "GET /api/admin/sends"


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
    Route.ADMIN_HANDOFFS: (Surface.ADMIN_CONSOLE, True),
    Route.ADMIN_SENDS: (Surface.ADMIN_CONSOLE, True),
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
# ---------------------------------------------------------------------------
# The reply shape, the canned texts and the answer tree now live in
# `answer_engine`, because the voice path needs the same ones (US-001 Phase C).
# They are re-exported here: this module is where callers and tests have always
# imported them from, and moving the definition should not move the import.
# ---------------------------------------------------------------------------
from .answer_engine import (  # noqa: F401
    ASK_CONTACT_TEXT,
    CLINICAL_TEXT,
    DECLINE_TEXT,
    DECLINE_TEXT_NO_OFFER,
    HELP_TEXT,
    LEAD_CAPTURED_TEXT,
    OFFLINE_TEXT,
    OFFER_PHRASE,
    README_SERVICE_UNAVAILABLE,
    AnswerEngine,
    ChatReply,
    greeting_text,
)
from .orchestrator import QueueFull  # noqa: F401 - re-exported for callers



class ChatSurface:
    """Web and mobile-web chat. Open to buyers, rate-limited (CV-03)."""

    def __init__(self, orchestrator, answers=None, retriever=None, leads=None, *,
                 sentiment=None, rate_limit=None, traces=None, llm=None,
                 probe=None) -> None:
        self.orchestrator = orchestrator
        # The answer tree owns `answers`, `retriever` and `leads` (see the
        # properties below). They are stored in exactly one place: while the
        # surface kept its own copies, an engine built from them could hold a
        # different corpus or retriever than the surface did, and the two would
        # disagree about what the product knows.
        # US-014/US-015: the prose backend, when one is configured. Injected
        # rather than constructed here, so a chat-only or test instance gets
        # exactly the deterministic engine it had before.
        # US-012: the coverage probe, when a corpus is available. With it,
        # "not published" is answered as such instead of declined.
        self.engine = AnswerEngine(
            answers=answers, retriever=retriever, leads=leads, llm=llm, probe=probe
        )
        # MOD-07. Optional, and *observational*: scoring the buyer's turn never
        # changes the answer, and a failure in it never fails the turn. It
        # exists so a negative conversation reaches a human (UC-10), which was
        # the deferred capability this wiring closes.
        self.sentiment = sentiment
        from .channels import RateLimiter

        self.rate_limit = rate_limit or RateLimiter(limit=30, window_s=60.0)
        # MOD-08 / US-025. Optional and best-effort: a turn with no writer, or
        # a writer that raises, behaves exactly as it did before tracing.
        self.traces = traces

    # -- the answer engine's dependencies, under the names this surface has
    #    always used. Assigning `chat.answers = ...` rebinds the engine's, so
    #    there is no second copy to fall out of step.
    @property
    def answers(self):
        return self.engine.answers

    @answers.setter
    def answers(self, value) -> None:
        self.engine.answers = value

    @property
    def retriever(self):
        return self.engine.retriever

    @retriever.setter
    def retriever(self, value) -> None:
        self.engine.retriever = value

    @property
    def leads(self):
        return self.engine.leads

    @leads.setter
    def leads(self, value) -> None:
        self.engine.leads = value

    def post_message(self, *, message: str, session_id: str) -> Response:
        if not (message or "").strip():
            return Response(400, {"error": "empty message"})

        try:
            self.rate_limit.check(session_id)
        except Exception as exc:  # noqa: BLE001 - rate limit is a refusal, not a crash
            return Response(429, {"error": str(exc)})

        from .channels import Channel, InboundEnvelope

        envelope = InboundEnvelope(
            channel=Channel.WEB_CHAT, text=message, session_id=session_id
        )

        # US-006 AC-3. The whole turn — record, classify, answer, persist —
        # runs under the conversation's single slot. This is the fix for the
        # audit's reproduction (`[user A, user B, assistant B, assistant A]`):
        # the answer used to run outside the lock, so two messages arriving
        # together interleaved their answers into an incoherent transcript.
        conversation = self.orchestrator.registry.resolve(envelope)
        try:
            return self.orchestrator.submit_turn(
                conversation.conversation_id, lambda: self._run_turn(envelope)
            )
        except QueueFull:
            # Edge case 3: the buyer is re-prompted, never silently dropped.
            # `submit` already counted the overflow.
            return Response(429, {
                "error": "still working on your previous question — please wait a moment.",
                "retry_after_s": 1,
            })

    def _run_turn(self, envelope) -> Response:
        """One turn, start to finish, inside the conversation's slot."""
        import time as _time

        started = _time.monotonic()
        conversation, routing = self.orchestrator.begin_turn(envelope)
        trace = self._begin_trace(conversation)
        reply = self._answer(routing.intent, envelope.text, conversation)
        # US-012: the turn stores what the answer was grounded in — the chunk
        # ids and source URLs — so the review can render the grounding for that
        # turn rather than a single URL that happens to be the first chunk's.
        refs = tuple(
            str(citation.get("chunk_id") or citation.get("source_url"))
            for citation in reply.grounded_on
            if citation.get("chunk_id") or citation.get("source_url")
        )
        self.orchestrator.complete_turn(
            conversation, reply.text, failed=reply.unavailable,
            grounding_refs=refs,
            latency_ms=int((_time.monotonic() - started) * 1000),
        )
        self._observe_sentiment(envelope.text, conversation)
        self._emit_trace(trace, conversation, reply)
        return Response(200, reply.as_dict())

    # -- per-turn trace (US-025) -------------------------------------------
    def _begin_trace(self, conversation):
        """Open a trace, or None when tracing is off. Never raises."""
        if self.traces is None:
            return None
        try:
            trace = self.traces.start(
                conversation_id=conversation.conversation_id,
                channel="web_chat",
                turn_no=len(getattr(conversation, "turns", ()) or ()),
            )
            trace.mark("turn_received")
            return trace
        except Exception:  # noqa: BLE001 - telemetry must never fail a turn
            return None

    def _emit_trace(self, trace, conversation, reply) -> None:
        """Close and write the turn's trace. Never raises (TAC-1/TAC-4).

        Stages are marked only where they actually happened: a greeting runs
        no retrieval, so no retrieval stage appears in its record — a missing
        stage and a zero-millisecond stage are different facts (TAC-3).
        """
        if trace is None:
            return
        try:
            trace.mark("answer_composed")
            trace.intent = reply.intent
            trace.grounded = reply.grounded
            trace.failed = bool(reply.unavailable)
            # US-015: the breaker's state belongs on every turn's record. "The
            # answer was verbatim" has two very different causes — a
            # deterministic path, or a model that is down — and only the trace
            # can tell them apart after the fact.
            llm = getattr(self.engine, "llm", None)
            trace.note(
                answer_chars=len(reply.text),
                declined=bool(reply.declined),
                escalation=bool(reply.escalation),
                llm_configured=llm is not None,
                llm_available=bool(getattr(llm, "available", False)),
            )
            state = getattr(conversation, "sentiment", None)
            if state is not None:
                trace.mark("sentiment_scored")
                trace.sentiment_category = str(getattr(state, "category", "") or "")
            trace.mark("outcome")
            trace.outcome = "failed" if reply.unavailable else "answered"
            self.traces.emit(trace)
        except Exception:  # noqa: BLE001 - telemetry must never fail a turn
            logger.warning("chat trace failed", exc_info=True)

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
                # US-022: the *real* lead id, empty when the buyer gave no
                # contact details. The conversation id used to be substituted
                # here, which made a handoff look linked to a lead row that did
                # not exist; the conversation travels on its own field now.
                lead_id=getattr(conversation, "lead_id", "") or "",
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

        US-004 (`AC-2`, `T-13`): when the registry has no memory of the session
        — a restart, or a page that reconnected — the transcript is recovered
        from the store inside the idle window rather than being reported gone.
        """
        if not (session_id or "").strip():
            return Response(400, {"error": "empty session_id"})

        conversation = self.orchestrator.registry.get(session_id)
        if conversation is None:
            conversation = self.orchestrator.registry.recover(session_id)
        if conversation is None:
            return Response(200, {"resumed": False, "conversation_id": None, "turns": []})

        turns = None
        store = getattr(self.orchestrator, "store", None)
        if store is not None:
            try:
                total = store.turn_count(conversation.conversation_id)
                records = store.page_turns(
                    conversation.conversation_id,
                    after_seq=max(0, total - int(limit)), limit=int(limit),
                )
                turns = [
                    {"role": record.role, "content": record.text, "at": record.created_at,
                     "seq": record.seq}
                    for record in records
                ]
            except Exception:  # noqa: BLE001 - fall back to the in-memory transcript
                turns = None
        if turns is None:
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

    def _answer(self, intent, message: str, conversation) -> ChatReply:
        """Delegate to the shared engine (US-001 Phase C).

        The decision tree moved to :class:`answer_engine.AnswerEngine` so the
        voice path runs the same one instead of a second composition of its
        own. It stays a method here because the surface's callers and tests
        use it, and because the surface owns the engine it was built with.
        """
        return self.engine.answer(intent, message, conversation)



class BrowserMicSurface:
    """Hands out a voice session ticket. The audio itself runs on the WS path.

    US-003: the ticket is a **signed** token with a TTL, not the opaque UUID it
    used to be. The UUID proved nothing — anything that could reach the socket
    was admitted to it. The token is what the WebSocket verifies before a
    single audio frame is read (`TAC-2`); `expires_in` is returned so the
    client can mint a fresh one rather than guess when its own expires.
    """

    def __init__(self, admission, tokens=None, callback_number: str = "") -> None:
        self.admission = admission
        self.tokens = tokens
        # Shown in the degraded and busy states — a buyer who cannot use voice
        # still needs a route that works (`CS-4`). Empty is honest: the client
        # says "or call us" only when there is a number to give.
        self.callback_number = callback_number

    def post_ticket(self, *, session_id: str) -> Response:
        session_id = (session_id or "").strip()
        if not session_id:
            return Response(400, {"error": "empty session_id"})

        # `peek`, not `admit`: see the note on AdmissionController.peek. A
        # ticket that reserved the slot would leak it on every denied
        # microphone permission.
        decide = getattr(self.admission, "peek", None) or self.admission.admit
        decision = decide(f"browser-{session_id}")
        if not decision.admitted:
            # A refusal is a designed outcome, delivered with what to do next.
            return Response(503, {
                "admitted": False,
                "message": decision.message,
                "reason": decision.reason,
                "callback_number": self.callback_number,
            })

        if self.tokens is None:
            # No signer wired: say so rather than handing out a ticket the
            # voice socket cannot verify. The client degrades to text.
            return Response(503, {
                "admitted": False,
                "message": "Voice isn't available right now — please type your question.",
                "reason": "no token signer configured",
            })

        try:
            ticket = self.tokens.mint(session_id)
        except Exception:  # noqa: BLE001 - a mint failure is a refusal, not a 500
            logger.exception("voice ticket: could not mint a session token")
            return Response(503, {
                "admitted": False,
                "message": "Voice isn't available right now — please type your question.",
                "reason": "token minting failed",
            })

        return Response(200, {
            "admitted": True,
            "ticket": ticket,
            "expires_in": self.tokens.ttl_seconds,
            "level": decision.level.value,
            "callback_number": self.callback_number,
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
                 metrics=None, gate=None, traces=None, crm=None, outbox=None,
                 store=None) -> None:
        self.registry = registry
        self.gateway = gateway
        # US-022: the escalation queue. The audit found handoffs were written
        # and readable by nothing — an operator could not see who was waiting
        # for a callback.
        self.crm = crm
        # US-002: the outbound send queue. `FAILED` rows are buyers whose
        # answer the carrier would not carry; without this the queue would be
        # the same write-only table the handoffs were.
        self.outbox = outbox
        # US-006: the conversation/turn store. When wired, review reads the
        # *persisted* transcript rather than the in-memory one — the difference
        # between a review that survives a restart and one that quietly shows
        # nothing.
        self.store = store
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
    def conversations(self, *, limit: int = 50, offset: int = 0) -> Response:
        """The review list — persisted conversations when a store is wired.

        US-006 (`AC-6`): paged, because a busy instance has more conversations
        than a screen, and a read model that dumps them all is one that gets
        slower every day it works.
        """
        if self.store is not None:
            try:
                records = self.store.list_conversations(limit=limit, offset=offset)
                conversations = []
                for record in records:
                    payload = record.as_dict()
                    payload["state"] = record.state.lower()
                    payload["channels_seen"] = [record.channel_kind]
                    payload["turns"] = self.store.turn_count(record.id)
                    payload["sentiment_category"] = None
                    conversations.append(payload)
                return Response(200, {
                    "count": self.store.count_conversations(),
                    "offset": offset,
                    "limit": limit,
                    "conversations": conversations,
                })
            except Exception as exc:  # noqa: BLE001 - a read must not 500 the console
                return Response(503, {"unavailable": True,
                                      "reason": f"{type(exc).__name__}: {exc}",
                                      "conversations": []})

        if self.registry is None:
            return Response(503, {"unavailable": True, "reason": "conversation registry not wired",
                                  "conversations": []})
        conversations = list(self.registry._by_key.values())[:limit]  # noqa: SLF001
        return Response(200, {
            "count": len(conversations),
            "conversations": [conversation.as_dict() for conversation in conversations],
        })

    def conversation(self, *, conversation_id: str, after_seq: int = 0,
                     limit: int = 100) -> Response:
        """One conversation's transcript, paged, with honest absence.

        Three absences are distinguished rather than collapsed into "empty":
        a conversation with no turns yet, one whose transcript **expired**
        under retention (shown with its date, `AC-7`), and an unknown id.
        """
        if self.store is not None:
            try:
                record = self.store.get_conversation(conversation_id)
                if record is None:
                    return Response(404, {"error": "no such conversation"})
                transcript_state = self.store.transcript_state(conversation_id)
                turns = [
                    turn.as_dict()
                    for turn in self.store.page_turns(
                        conversation_id, after_seq=after_seq, limit=limit
                    )
                ]
                handoffs = self._handoffs_for(conversation_id)
                return Response(200, {
                    "conversation_id": conversation_id,
                    "state": record.state.lower(),
                    "channel": record.channel_kind,
                    "identity_type": record.identity_type,
                    "lead_id": record.lead_id,
                    "parent_conversation_id": record.parent_conversation_id,
                    "summary": record.summary,
                    "transcript_state": transcript_state,
                    "transcript_expired_at": record.transcript_expired_at,
                    "turn_count": self.store.turn_count(conversation_id),
                    "next_after_seq": turns[-1]["seq"] if turns else after_seq,
                    "turns": turns,
                    "handoffs": handoffs,
                    "sentiment": self._sentiment_for(conversation_id),
                })
            except Exception as exc:  # noqa: BLE001 - a read must not 500 the console
                return Response(503, {"unavailable": True,
                                      "reason": f"{type(exc).__name__}: {exc}"})

        if self.registry is None:
            return Response(503, {"unavailable": True, "reason": "registry not wired"})
        for conversation in self.registry._by_key.values():  # noqa: SLF001
            if conversation.conversation_id == conversation_id:
                return Response(200, {
                    "conversation_id": conversation_id,
                    "state": conversation.state.value,
                    "channels_seen": conversation.channels_seen,
                    "turn_count": conversation.turn_count,
                    "transcript_state": "present" if conversation.turns else "empty",
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

    def _handoffs_for(self, conversation_id: str) -> list[dict]:
        if self.crm is None:
            return []
        try:
            return [
                handoff.as_dict() for handoff in self.crm.list_handoffs()
                if getattr(handoff, "conversation_id", None) == conversation_id
            ]
        except Exception:  # noqa: BLE001
            return []

    def _sentiment_for(self, conversation_id: str):
        """The sentiment trail, when the conversation is still in memory.

        `None` is honest: an expired or restarted conversation has no live
        sentiment object, and rendering a fabricated "neutral" would be worse
        than rendering nothing (`T-20`).
        """
        if self.registry is None:
            return None
        for conversation in self.registry._by_key.values():  # noqa: SLF001
            if conversation.conversation_id == conversation_id:
                state = getattr(conversation, "sentiment", None)
                return state.as_dict() if hasattr(state, "as_dict") else None
        return None

    # -- metrics -----------------------------------------------------------
    def metrics_view(self) -> Response:
        if self.metrics is None:
            return Response(503, {"unavailable": True, "reason": "metrics not wired",
                                  "metrics": []})
        return Response(200, {"metrics": self.metrics.render_all()})

    # -- escalations (US-022) ----------------------------------------------
    def handoffs(self) -> Response:
        """The escalation queue — who is waiting for a person.

        `open` is the number nobody has been told about yet, and it is stated
        separately from the list because that is the number an operator acts on.
        """
        if self.crm is None:
            return Response(503, {"unavailable": True, "reason": "no CRM attached",
                                  "handoffs": []})
        try:
            records = [handoff.as_dict() for handoff in self.crm.list_handoffs()]
        except Exception as exc:  # noqa: BLE001 - a read must not 500 the console
            return Response(503, {"unavailable": True,
                                  "reason": f"{type(exc).__name__}: {exc}",
                                  "handoffs": []})
        return Response(200, {
            "handoffs": records,
            "open": sum(1 for record in records if record.get("state") == "open"),
        })

    # -- outbound sends (US-002) -------------------------------------------
    def sends(self, *, limit: int = 50, status: str = "") -> Response:
        """The send queue, worst first.

        `FAILED` rows are the ones an operator can act on — a buyer asked
        something and the carrier would not carry the answer (`AC-5`). Counts
        come from the store, so a status with no rows is absent from the
        mapping rather than a fabricated zero.
        """
        if self.outbox is None:
            return Response(503, {"unavailable": True, "reason": "no send queue attached",
                                  "sends": [], "counts": {}})
        try:
            counts = self.outbox.counts()
            if status:
                rows = [send.as_dict() for send in self.outbox.by_status(status, limit=limit)]
            else:
                rows = [
                    send.as_dict()
                    for state in ("FAILED", "RETRY", "QUEUED", "SENT")
                    for send in self.outbox.by_status(state, limit=limit)
                ][:limit]
        except Exception as exc:  # noqa: BLE001 - a read must not 500 the console
            return Response(503, {"unavailable": True,
                                  "reason": f"{type(exc).__name__}: {exc}",
                                  "sends": [], "counts": {}})
        return Response(200, {"sends": rows, "counts": counts})

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

    def __init__(self, *, admin_token: str = "", voice=None, voice_sessions=None,
                 voice_tokens=None, browser_voice=None, messaging=None,
                 extra_channel=None) -> None:
        self.admin_token = admin_token
        self._handlers: dict[Route, Callable[..., Response]] = {}
        self._lock = threading.Lock()
        self.calls: list[dict] = []
        # Builds the engine session for one call. A factory rather than an
        # instance: a `VoiceCallSession` carries the audio buffer, the VAD
        # counter and the conversation history of a single call, so sharing one
        # across calls would mix two callers' audio into one transcript.
        self.voice_sessions = voice_sessions
        # The Twilio ingress (US-001). It is not a `Route`: those are JSON
        # endpoints dispatched through `dispatch`, while a Twilio webhook takes
        # a form-encoded body and answers in TwiML. It rides on the router
        # anyway so the whole surface has one seam, and `server.create_app`
        # registers its two paths from here rather than from a global.
        self.voice = voice
        # US-003. The token signer the ticket route mints with and the voice
        # socket verifies with — one object, because two would eventually
        # disagree about the secret and every ticket would fail closed.
        self.voice_tokens = voice_tokens
        # Builds the engine session for one browser-mic session. Same reasoning
        # as `voice_sessions`: per-session state must not be shared.
        self.browser_voice = browser_voice
        # US-002. The messaging webhook (WhatsApp) and the extra-channel
        # ingress. Neither is a `Route` for the same reason as `voice`: both
        # take raw bodies, one authenticates by Twilio signature and the other
        # by HMAC, and both reply outside the JSON envelope.
        self.messaging = messaging
        self.extra_channel = extra_channel

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
                 admin_token: str = "", gate_answerer: Callable[[Any], str] | None = None,
                 voice=None, voice_sessions=None, voice_tokens=None,
                 browser_voice=None, messaging=None, extra_channel=None) -> Router:
    """Wire the three surfaces into one router.

    ``gate_answerer`` is bound **here, on the server**, and never taken from the
    request. The release gate is only meaningful if the answers it judges come
    from the live answer path: a caller who could supply their own answerer
    could return "ok" for every case and force a pass, which is precisely the
    failure the gate exists to prevent.
    """
    router = Router(admin_token=admin_token, voice=voice, voice_sessions=voice_sessions,
                    voice_tokens=voice_tokens, browser_voice=browser_voice,
                    messaging=messaging, extra_channel=extra_channel)
    router.register(Route.HEALTH, lambda: Response(200, {"status": "ok"}))
    router.register(Route.CHAT_MESSAGE, chat.post_message)
    router.register(Route.CHAT_HISTORY, chat.get_history)
    router.register(Route.VOICE_TICKET, mic.post_ticket)
    router.register(Route.ADMIN_CHANNELS, admin.channels)
    router.register(Route.ADMIN_CONVERSATIONS, admin.conversations)
    router.register(Route.ADMIN_CONVERSATION, admin.conversation)
    router.register(Route.ADMIN_METRICS, admin.metrics_view)
    router.register(Route.ADMIN_HANDOFFS, admin.handoffs)
    router.register(Route.ADMIN_SENDS, admin.sends)

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
