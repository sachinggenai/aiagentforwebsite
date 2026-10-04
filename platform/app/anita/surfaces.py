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



class ChatSurface:
    """Web and mobile-web chat. Open to buyers, rate-limited (CV-03)."""

    def __init__(self, orchestrator, answers=None, retriever=None, leads=None, *,
                 sentiment=None, rate_limit=None) -> None:
        self.orchestrator = orchestrator
        # The answer tree owns `answers`, `retriever` and `leads` (see the
        # properties below). They are stored in exactly one place: while the
        # surface kept its own copies, an engine built from them could hold a
        # different corpus or retriever than the surface did, and the two would
        # disagree about what the product knows.
        self.engine = AnswerEngine(answers=answers, retriever=retriever, leads=leads)
        # MOD-07. Optional, and *observational*: scoring the buyer's turn never
        # changes the answer, and a failure in it never fails the turn. It
        # exists so a negative conversation reaches a human (UC-10), which was
        # the deferred capability this wiring closes.
        self.sentiment = sentiment
        from .channels import RateLimiter

        self.rate_limit = rate_limit or RateLimiter(limit=30, window_s=60.0)

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

    def _answer(self, intent, message: str, conversation) -> ChatReply:
        """Delegate to the shared engine (US-001 Phase C).

        The decision tree moved to :class:`answer_engine.AnswerEngine` so the
        voice path runs the same one instead of a second composition of its
        own. It stays a method here because the surface's callers and tests
        use it, and because the surface owns the engine it was built with.
        """
        return self.engine.answer(intent, message, conversation)



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

    def __init__(self, *, admin_token: str = "", voice=None, voice_sessions=None) -> None:
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
                 voice=None, voice_sessions=None) -> Router:
    """Wire the three surfaces into one router.

    ``gate_answerer`` is bound **here, on the server**, and never taken from the
    request. The release gate is only meaningful if the answers it judges come
    from the live answer path: a caller who could supply their own answerer
    could return "ok" for every case and force a pass, which is precisely the
    failure the gate exists to prevent.
    """
    router = Router(admin_token=admin_token, voice=voice, voice_sessions=voice_sessions)
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
