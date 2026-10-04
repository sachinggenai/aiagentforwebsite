"""MOD-10 tests — the surface contract, the auth boundary, honest unavailability.

Run:  .venv\\Scripts\\python -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.channels import default_gateway  # noqa: E402
from anita.observability import MetricsRegistry, QualityGate, EvalCase  # noqa: E402
from anita.orchestrator import Orchestrator  # noqa: E402
from anita.speech import AdmissionController  # noqa: E402
from anita.surfaces import (  # noqa: E402
    CLINICAL_TEXT,
    DECLINE_TEXT,
    ROUTE_TABLE,
    AdminSurface,
    AuthError,
    BrowserMicSurface,
    ChatSurface,
    Route,
    Router,
    Surface,
    authenticate,
    build_router,
    requires_auth,
)

TOKEN = "secret-admin-token"


class FakeRetriever:
    """Returns a grounded chunk, or nothing, so both gate outcomes are testable."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok

    def retrieve(self, query):
        from anita.retrieval import Chunk, GateDecision, RetrievalResult, ScoredChunk

        if not self.ok:
            return RetrievalResult(
                decision=GateDecision(False, "below_threshold", 0.2), chunks=[], query=query
            )
        chunk = Chunk(
            chunk_id="c1", text="X-Ray Cassette specification: Condition New",
            product_id="270691", product_name="X-Ray Cassette", chunk_type="product_specs",
            source_url="https://www.anitamedicalsystems.com/x-ray-cassette-270691.html",
        )
        return RetrievalResult(
            decision=GateDecision(True, "similarity", 0.9),
            chunks=[ScoredChunk(chunk=chunk, fused_score=0.5)],
            query=query,
        )


@pytest.fixture()
def chat() -> ChatSurface:
    return ChatSurface(Orchestrator(), retriever=FakeRetriever())


@pytest.fixture()
def admin() -> AdminSurface:
    from anita.orchestrator import ConversationRegistry

    return AdminSurface(
        registry=ConversationRegistry(),
        gateway=default_gateway(twilio_auth_token="t"),
        admission=AdmissionController(),
        metrics=MetricsRegistry(),
        gate=QualityGate([EvalCase("c1", "q", must_contain=("ok",))]),
    )


@pytest.fixture()
def router(chat, admin) -> Router:
    return build_router(
        chat=chat, mic=BrowserMicSurface(AdmissionController()), admin=admin, admin_token=TOKEN
    )


# ---------------------------------------------------------------------------
# The auth boundary — CV-03 / DG-06
# ---------------------------------------------------------------------------
class TestAuthBoundary:
    def test_every_route_declares_its_surface_and_auth(self):
        """One table, so the boundary is reviewable at a glance rather than
        inferred from decorators scattered across a file."""
        for route in Route:
            assert route in ROUTE_TABLE, f"{route} is not declared in the route table"

    def test_buyer_routes_are_open(self):
        """Authenticating buyers would block the traffic the product exists to
        serve."""
        for route in (Route.CHAT_MESSAGE, Route.CHAT_HISTORY, Route.VOICE_TICKET):
            assert not requires_auth(route), route

    def test_admin_routes_are_authenticated(self):
        for route in (Route.ADMIN_CHANNELS, Route.ADMIN_CONVERSATIONS,
                      Route.ADMIN_CONVERSATION, Route.ADMIN_METRICS, Route.ADMIN_GATE):
            assert requires_auth(route), route

    def test_a_valid_token_passes(self):
        authenticate(Route.ADMIN_CHANNELS, TOKEN, TOKEN)

    def test_a_wrong_token_is_refused(self):
        with pytest.raises(AuthError, match="invalid"):
            authenticate(Route.ADMIN_CHANNELS, "wrong", TOKEN)

    def test_a_missing_token_is_refused(self):
        with pytest.raises(AuthError, match="missing"):
            authenticate(Route.ADMIN_CHANNELS, None, TOKEN)

    def test_an_unset_admin_token_fails_closed(self):
        """An unconfigured token must refuse, never wave the request through —
        this is the failure that would expose every conversation."""
        with pytest.raises(AuthError, match="closed"):
            authenticate(Route.ADMIN_CHANNELS, "anything", "")

    def test_buyer_routes_ignore_the_token(self):
        authenticate(Route.CHAT_MESSAGE, None, "")

    def test_the_router_enforces_the_table(self, router):
        assert router.dispatch(Route.ADMIN_CHANNELS).status == 401
        assert router.dispatch(Route.ADMIN_CHANNELS, token=TOKEN).status == 200

    def test_a_new_admin_route_cannot_skip_auth_by_forgetting_a_decorator(self, router):
        """Auth is decided by the table, once, for every route."""
        for route in Route:
            if requires_auth(route):
                assert router.dispatch(route).status == 401

    def test_contract_is_publishable(self, router):
        contract = router.contract()
        assert contract["POST /api/chat/message"]["authenticated"] is False
        assert contract["GET /api/admin/metrics"]["authenticated"] is True
        assert contract["GET /api/admin/metrics"]["surface"] == Surface.ADMIN_CONSOLE.value

    def test_every_declared_route_has_a_handler(self, router):
        """A route in the published contract that no handler serves is a lie:
        ``contract()`` says it exists, and every caller gets a 404. Dispatching
        with the wrong arguments is fine — a 400 proves a handler was reached;
        a 404 with 'no handler' does not.
        """
        for route in Route:
            response = router.dispatch(route, token=TOKEN)
            assert response.status != 404 or "no handler" not in str(response.body), \
                f"{route.value} is declared but has no handler"


# ---------------------------------------------------------------------------
# Buyer chat
# ---------------------------------------------------------------------------
class TestChatSurface:
    def test_a_grounded_question_is_answered(self, chat):
        response = chat.post_message(message="what is the cassette condition?", session_id="s1")
        assert response.status == 200
        assert response.body["grounded"] is True
        assert response.body["source_url"]

    def test_an_empty_message_is_rejected(self, chat):
        assert chat.post_message(message="   ", session_id="s1").status == 400

    def test_an_unanswerable_question_declines(self):
        chat = ChatSurface(Orchestrator(), retriever=FakeRetriever(ok=False))
        body = chat.post_message(message="tell me about the cassette", session_id="s1").body
        assert DECLINE_TEXT in body["text"]
        assert DECLINE_TEXT in body["text"]
        assert "specification" not in body["text"].lower(), "a decline must not leak context"

    def test_a_clinical_question_is_refused(self, chat):
        body = chat.post_message(message="is this safe for a pacemaker patient?", session_id="s1").body
        assert body["text"] == CLINICAL_TEXT

    def test_an_off_topic_question_declines(self, chat):
        body = chat.post_message(message="who won the world cup?", session_id="s1").body
        assert body["text"] == DECLINE_TEXT

    def test_without_retrieval_the_surface_declines_rather_than_improvising(self):
        """No retriever means no grounded answer — so it declines. Wiring a
        model in here would be how a fabrication reaches a buyer.

        With no lead service either, the decline carries no offer of a callback
        — see `test_without_a_lead_service_the_offer_is_not_made`."""
        from anita.surfaces import DECLINE_TEXT_NO_OFFER

        chat = ChatSurface(Orchestrator(), retriever=None)
        body = chat.post_message(message="what is the price of the cassette?", session_id="s1").body
        assert DECLINE_TEXT_NO_OFFER in body["text"]
        assert body["unavailable"] is True

    def test_rate_limiting_refuses_a_flood(self):
        from anita.channels import RateLimiter

        chat = ChatSurface(Orchestrator(), retriever=FakeRetriever(),
                           rate_limit=RateLimiter(limit=2, window_s=60))
        assert chat.post_message(message="hi", session_id="s1").status == 200
        assert chat.post_message(message="hi", session_id="s1").status == 200
        assert chat.post_message(message="hi", session_id="s1").status == 429

    def test_sessions_are_independent(self, chat):
        a = chat.post_message(message="what is the cassette condition?", session_id="s1").body
        b = chat.post_message(message="what is the cassette condition?", session_id="s2").body
        assert a["conversation_id"] != b["conversation_id"]


# ---------------------------------------------------------------------------
# Chat history
# ---------------------------------------------------------------------------
class TestChatHistory:
    def test_history_returns_the_sessions_own_turns(self, chat):
        chat.post_message(message="what is the cassette condition?", session_id="s1")
        body = chat.get_history(session_id="s1").body
        assert body["resumed"] is True
        assert body["turns"][0]["content"] == "what is the cassette condition?"

    def test_a_known_session_cannot_read_another_sessions_history(self, chat):
        """The session id is the only key a caller can present, so scoping by it
        is the whole of the authorisation — this asserts it is actually applied."""
        chat.post_message(message="my secret order", session_id="s1")
        body = chat.get_history(session_id="s2").body
        assert body["resumed"] is False
        assert body["turns"] == []

    def test_an_unknown_session_is_empty_not_an_error(self, chat):
        """A first-time visitor asking for their history is normal."""
        assert chat.get_history(session_id="never-seen").status == 200

    def test_an_empty_session_id_is_rejected(self, chat):
        assert chat.get_history(session_id="  ").status == 400


# ---------------------------------------------------------------------------
# Browser mic
# ---------------------------------------------------------------------------
class TestBrowserMic:
    def test_a_ticket_is_issued_when_admitted(self):
        mic = BrowserMicSurface(AdmissionController(max_concurrent_calls=1))
        response = mic.post_ticket(session_id="s1")
        assert response.status == 200
        assert response.body["ticket"]

    def test_a_refusal_explains_what_to_do(self):
        """A refusal is a designed outcome, delivered with the next step."""
        admission = AdmissionController(max_concurrent_calls=1)
        mic = BrowserMicSurface(admission)
        mic.post_ticket(session_id="s1")
        response = mic.post_ticket(session_id="s2")
        assert response.status == 503
        assert response.body["admitted"] is False
        assert "call you back" in response.body["message"]

    def test_voice_is_refused_when_degraded(self):
        admission = AdmissionController()
        admission.degrade("gpu pressure")
        response = BrowserMicSurface(admission).post_ticket(session_id="s1")
        assert response.status == 503
        assert "voice shed" in response.body["reason"]


# ---------------------------------------------------------------------------
# Admin console
# ---------------------------------------------------------------------------
class TestAdminConsole:
    def test_channels_are_listed(self, admin):
        body = admin.channels().body
        assert "whatsapp" in [c["name"] for c in body["channels"]]
        assert body["signature_validation"] is True

    def test_an_unwired_gateway_says_unavailable(self):
        body = AdminSurface().channels().body
        assert body["unavailable"] is True
        assert body["channels"] == []

    def test_conversations_are_listed(self, admin, chat):
        chat.post_message(message="what is the cassette condition?", session_id="s1")
        admin.registry = chat.orchestrator.registry
        body = admin.conversations().body
        assert body["count"] >= 1

    def test_a_conversation_can_be_reviewed(self, admin, chat):
        posted = chat.post_message(message="what is the cassette condition?", session_id="s1")
        admin.registry = chat.orchestrator.registry
        body = admin.conversation(conversation_id=posted.body["conversation_id"]).body
        assert body["turns"][0]["role"] == "user"
        assert body["turns"][0]["intent"]

    def test_an_unknown_conversation_is_404(self, admin):
        assert admin.conversation(conversation_id="nope").status == 404

    def test_an_unwired_registry_says_unavailable_not_empty(self, admin):
        """An empty list cannot distinguish 'nobody called' from 'the database
        is down' — and those demand opposite responses."""
        body = AdminSurface().conversations().body
        assert body["unavailable"] is True

    def test_metrics_render_absence_distinctly(self, admin):
        admin.metrics.set("queue_depth", 0)
        admin.metrics.mark_unavailable("gpu_free_mib")
        rendered = admin.metrics_view().body["metrics"]
        assert any("queue_depth: 0" in line for line in rendered)
        assert any("unavailable" in line for line in rendered)

    def test_unwired_metrics_say_unavailable(self):
        assert AdminSurface().metrics_view().body["unavailable"] is True

    def test_the_release_gate_runs(self, admin):
        body = admin.run_gate(lambda case: "ok").body
        assert body["passed"] is True
        assert body["total"] == 1

    def test_an_unwired_gate_says_unavailable(self):
        assert AdminSurface().run_gate(lambda case: "x").body["unavailable"] is True

    def test_the_gate_reports_failed_case_ids(self, admin):
        body = admin.run_gate(lambda case: "wrong").body
        assert body["passed"] is False
        assert body["failed"] == ["c1"]

    def test_an_empty_evaluation_set_does_not_pass(self):
        """A gate with no cases is not a gate, and must never report success."""
        gate = QualityGate([])
        assert gate.evaluate(lambda case: "ok").passed is False


# ---------------------------------------------------------------------------
# The release gate over HTTP — the answerer is bound server-side
# ---------------------------------------------------------------------------
class TestGateBinding:
    def _router(self, answerer):
        return build_router(
            chat=ChatSurface(Orchestrator(), retriever=FakeRetriever()),
            mic=BrowserMicSurface(AdmissionController()),
            admin=AdminSurface(gate=QualityGate([EvalCase("c1", "q", must_contain=("ok",))])),
            admin_token=TOKEN,
            gate_answerer=answerer,
        )

    def test_a_bound_answerer_runs_the_gate_over_http(self):
        router = self._router(lambda case: "ok")
        response = router.dispatch(Route.ADMIN_GATE, token=TOKEN)
        assert response.status == 200
        assert response.body["passed"] is True

    def test_an_unbound_answerer_says_unavailable_rather_than_400(self):
        """Without a live answer path the route must explain itself. Dispatching
        with no answerer used to raise TypeError and surface as 'bad request',
        which reads as the console's fault rather than a missing binding."""
        router = self._router(None)
        response = router.dispatch(Route.ADMIN_GATE, token=TOKEN)
        assert response.status == 503
        assert response.body["unavailable"] is True

    def test_a_caller_cannot_supply_their_own_answerer(self):
        """The whole gate is worthless if the caller picks what answers. A kwarg
        named answerer must not reach the handler — it is bound at build time."""
        router = self._router(lambda case: "wrong")
        response = router.dispatch(
            Route.ADMIN_GATE, token=TOKEN, answerer=lambda case: "ok"
        )
        # Either a 400 (the kwarg found no home) or a real run — but never a
        # pass produced by the caller's own answerer.
        assert response.status == 400 or response.body.get("passed") is False

    def test_the_gate_is_still_authenticated(self):
        router = self._router(lambda case: "ok")
        assert router.dispatch(Route.ADMIN_GATE).status == 401


# ---------------------------------------------------------------------------
# Router behaviour
# ---------------------------------------------------------------------------
class TestRouter:
    def test_a_healthy_route_responds(self, router):
        assert router.dispatch(Route.HEALTH).status == 200

    def test_a_handler_error_becomes_a_500_not_a_crash(self):
        """A surface must not propagate an exception to the client."""
        router = Router(admin_token=TOKEN)

        def explode():
            raise RuntimeError("boom")

        router.register(Route.ADMIN_CHANNELS, explode)
        response = router.dispatch(Route.ADMIN_CHANNELS, token=TOKEN)
        assert response.status == 500
        assert "RuntimeError" in response.body["error"]

    def test_bad_arguments_become_a_400(self):
        from anita.surfaces import Response

        router = Router(admin_token=TOKEN)
        # A handler with a fixed signature, so an unexpected kwarg is a TypeError.
        router.register(Route.ADMIN_CHANNELS, lambda: Response(200, {}))
        assert router.dispatch(Route.ADMIN_CHANNELS, token=TOKEN, surprise=1).status == 400

    def test_calls_are_recorded_for_diagnosis(self, router):
        router.dispatch(Route.ADMIN_CHANNELS, token=TOKEN)
        assert router.calls[-1]["route"] == Route.ADMIN_CHANNELS.value

    def test_a_handler_returning_none_is_a_500_not_a_client_crash(self):
        """A wiring bug must surface as a server error, not as an AttributeError
        raised while logging the call — which would escape the router entirely."""
        router = Router(admin_token=TOKEN)
        router.register(Route.ADMIN_CHANNELS, lambda **kw: None)
        response = router.dispatch(Route.ADMIN_CHANNELS, token=TOKEN)
        assert response.status == 500
        assert "expected Response" in response.body["error"]

    def test_a_handler_returning_the_wrong_type_is_a_500(self):
        router = Router(admin_token=TOKEN)
        router.register(Route.ADMIN_CHANNELS, lambda **kw: {"not": "a response"})
        assert router.dispatch(Route.ADMIN_CHANNELS, token=TOKEN).status == 500


# ---------------------------------------------------------------------------
# A commercial question must reach the deterministic engine
#
# Found by asking the live instance for a price. The intent was classified
# COMMERCIAL, the fact registry had PRICE registered, and nothing joined them —
# so the answer was whichever chunk ranked first, once a FAQ about diagnostic
# suitability. Every word of it verbatim from the catalogue, and still the
# wrong answer to the question asked.
# ---------------------------------------------------------------------------
class GroundingRetriever:
    """Returns a chunk for a *different* product than the one asked about.

    That is what the live retriever did, so the fixtures must do it too —
    otherwise the tests pass for a reason production does not share.
    """

    def retrieve(self, query):
        from anita.retrieval import Chunk, GateDecision, RetrievalResult, ScoredChunk

        chunk = Chunk(
            chunk_id="c1", text="Xray Intensifying Screen — Condition: New",
            product_id="999999", product_name="Xray Intensifying Screen",
            chunk_type="product_specs",
            source_url="https://www.anitamedicalsystems.com/xray-intensifying-screen-999999.html",
        )
        return RetrievalResult(
            decision=GateDecision(True, "similarity", 0.9),
            chunks=[ScoredChunk(chunk=chunk, fused_score=0.5)],
            query=query,
        )


@pytest.fixture()
def grounded():
    """A chat surface with the real answer engine over the real corpus."""
    import pytest as _pytest

    from anita.answers import AnitaAnswers
    from anita.corpus import Corpus

    corpus_dir = Path(__file__).resolve().parents[2] / "data" / "out"
    if not (corpus_dir / "ams.sqlite").exists():
        _pytest.skip("corpus not built")
    return ChatSurface(
        Orchestrator(), answers=AnitaAnswers(Corpus(corpus_dir)),
        retriever=GroundingRetriever(),
    )


class TestCommercialRouting:
    def test_a_price_question_is_answered_with_the_price(self, grounded):
        """Not with a chunk about something else, however verbatim."""
        body = grounded.post_message(
            message="what is the price of the ultrasound gel?", session_id="s1"
        ).body
        assert "60" in body["text"]
        assert "Intensifying Screen" not in body["text"]

    def test_a_price_answer_carries_its_caveat(self, grounded):
        """A list price without the caveat is a quotation the company did not
        make."""
        body = grounded.post_message(
            message="what is the price of the ultrasound gel?", session_id="s1"
        ).body
        assert grounded.answers  # the engine produced the number
        assert "quotation" in body["text"].lower()

    def test_an_moq_question_is_answered_with_the_moq(self, grounded):
        body = grounded.post_message(
            message="what is the minimum order quantity for the ultrasound gel?",
            session_id="s1",
        ).body
        assert "10" in body["text"]

    def test_a_named_product_beats_the_best_ranked_chunk(self, grounded):
        """Retrieval is a good way to find the relevant page and a poor way to
        decide which product was asked about."""
        body = grounded.post_message(
            message="give me the X-Ray Cassette specification", session_id="s1"
        ).body
        assert "X-Ray Cassette" in body["text"] or "Xray" not in body["text"]

    def test_two_products_sharing_a_name_prompt_a_question_not_a_guess(self, grounded):
        """Quoting a real price for the wrong machine is worse than asking."""
        body = grounded.post_message(
            message="what is the price of the medical dry imager?", session_id="s1"
        ).body
        assert "more than one match" in body["text"].lower() or "which one" in body["text"].lower()

    def test_a_price_question_with_no_identifiable_product_declines(self, grounded):
        """It must not fall through to retrieval. Asked for "the price of the
        MRI machine you sell", retrieval returned a cassette FAQ — which both
        implies we sell MRI machines and answers a question nobody asked. There
        is no correct price for a product that cannot be identified."""
        body = grounded.post_message(
            message="what is the price of the MRI machine you sell?", session_id="s1"
        ).body
        assert "don't have that in our catalogue" in body["text"]
        assert "cassette" not in body["text"].lower()

    def test_an_unresolved_price_question_does_not_name_a_product(self, grounded):
        """The decline text is generic on purpose: naming the product it found
        instead would be a claim about the catalogue."""
        body = grounded.post_message(
            message="what is the price of a CT scanner?", session_id="s1"
        ).body
        assert "Intensifying" not in body["text"]
        assert "X-Ray" not in body["text"]

    def test_the_conversation_id_is_carried_through_the_commercial_path(self, grounded):
        """The commercial path builds its own reply; forgetting the id would
        silently start a new conversation on every price question."""
        first = grounded.post_message(
            message="what is the price of the ultrasound gel?", session_id="s1"
        ).body
        assert first["conversation_id"]
        history = grounded.get_history(session_id="s1").body
        assert history["conversation_id"] == first["conversation_id"]


# ---------------------------------------------------------------------------
# Consent → lead capture (MOD-06)
#
# The assistant offered "would you like us to get in touch?" and then answered
# the buyer's "yes" with the same decline. LeadService, MockCrmStore, the
# qualification rules and the outbound queue were all built and tested and
# referenced by nothing, so the product's commercial purpose was unreachable
# from the only surface a buyer uses.
# ---------------------------------------------------------------------------
@pytest.fixture()
def with_leads():
    from anita.crm import MockCrmStore
    from anita.leads import LeadService

    service = LeadService(MockCrmStore(":memory:"))
    return ChatSurface(Orchestrator(), retriever=None, leads=service), service


class TestConsentAndLeadCapture:
    def test_yes_after_an_offer_asks_for_contact_details(self, with_leads):
        chat, _ = with_leads
        chat.post_message(message="do you sell MRI machines?", session_id="s1")
        body = chat.post_message(message="yes", session_id="s1").body
        assert "number or email" in body["text"]

    def test_yes_without_an_offer_is_not_a_lead(self, with_leads):
        """An unprompted "yes" has nothing to consent to."""
        chat, _ = with_leads
        body = chat.post_message(message="yes", session_id="s1").body
        assert "number or email" not in body["text"]

    def test_contact_details_are_captured(self, with_leads):
        chat, service = with_leads
        chat.post_message(message="do you sell MRI machines?", session_id="s1")
        chat.post_message(message="yes", session_id="s1")
        chat.post_message(message="call me on 98200 11122", session_id="s1")
        assert service.store.find_lead(phone="+919820011122") is not None

    def test_details_are_captured_even_without_a_prior_offer(self, with_leads):
        """A buyer who volunteers a number wants a call back whether or not
        they were asked."""
        chat, service = with_leads
        body = chat.post_message(message="my number is 98200 11122", session_id="s1").body
        assert "passed your details" in body["text"]
        assert service.store.find_lead(phone="+919820011122") is not None

    def test_the_lead_carries_the_product_asked_about(self, with_leads):
        """A lead with no product interest is a name and a number; one that
        says which machine is a call a rep can make."""
        import pytest as _pytest

        from anita.answers import AnitaAnswers
        from anita.corpus import Corpus

        corpus_dir = Path(__file__).resolve().parents[2] / "data" / "out"
        if not (corpus_dir / "ams.sqlite").exists():
            _pytest.skip("corpus not built")

        chat, service = with_leads
        chat.answers = AnitaAnswers(Corpus(corpus_dir))
        chat.post_message(message="what is the price of the ultrasound gel?", session_id="s1")
        chat.post_message(message="call me on 98200 11122", session_id="s1")
        lead = service.store.find_lead(phone="+919820011122")
        assert "270694" in lead.product_interest

    def test_the_same_buyer_is_not_duplicated(self, with_leads):
        """Deduplication is a database guarantee, not a hope — two contacts
        from one buyer must produce one lead."""
        chat, service = with_leads
        chat.post_message(message="my number is 98200 11122", session_id="s1")
        chat.post_message(message="also my number is 98200 11122", session_id="s2")
        assert len(service.store.list_leads()) == 1

    def test_a_volunteered_name_is_not_captured_with_a_connective(self, with_leads):
        chat, service = with_leads
        chat.post_message(
            message="i am Dr Rao from Apollo Hospital, call me on 98200 11122",
            session_id="s1",
        )
        lead = service.store.find_lead(phone="+919820011122")
        assert lead.name == "Dr Rao"
        assert lead.company == "Apollo Hospital"
        assert lead.buyer_type == "Hospital"

    def test_without_a_lead_service_the_offer_is_not_made(self):
        """A promise the system cannot keep is worse than no promise: with
        nowhere to record a consent, the decline must not ask for one."""
        chat = ChatSurface(Orchestrator(), retriever=None)
        body = chat.post_message(message="do you sell MRI machines?", session_id="s1").body
        assert "get in touch?" not in body["text"]

    def test_with_a_lead_service_the_offer_is_made(self, with_leads):
        chat, _ = with_leads
        body = chat.post_message(message="do you sell MRI machines?", session_id="s1").body
        assert "get in touch?" in body["text"]
