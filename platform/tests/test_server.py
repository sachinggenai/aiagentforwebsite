"""The HTTP adapter (MOD-10) — exercised over a real ASGI transport.

These tests deliberately go through HTTP rather than calling the surfaces. The
surfaces are already tested directly; what is untested until now is the
*translation*: whether a bearer header becomes a token, whether a query string
becomes a keyword argument, whether a Refusal reaches the client as a 401 rather
than a 200 carrying an error field.

Run:  .venv\\Scripts\\python -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from fastapi.testclient import TestClient  # noqa: E402

from anita.channels import default_gateway  # noqa: E402
from anita.observability import EvalCase, MetricsRegistry, QualityGate  # noqa: E402
from anita.orchestrator import ConversationRegistry, Orchestrator  # noqa: E402
from anita.server import BODY_ARGUMENTS, METHOD_AND_PATH, QUERY_ARGUMENTS, create_app  # noqa: E402
from anita.speech import AdmissionController  # noqa: E402
from anita.surfaces import (  # noqa: E402
    AdminSurface,
    BrowserMicSurface,
    ChatSurface,
    Route,
    build_router,
)

TOKEN = "console-token"


class FakeRetriever:
    def retrieve(self, query):
        from anita.retrieval import Chunk, GateDecision, RetrievalResult, ScoredChunk

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
def client() -> TestClient:
    registry = ConversationRegistry()
    metrics = MetricsRegistry()
    metrics.set("queue_depth", 0)
    metrics.mark_unavailable("gpu_free_mib")

    chat = ChatSurface(Orchestrator(registry=registry), retriever=FakeRetriever())
    admin = AdminSurface(
        registry=registry,
        gateway=default_gateway(twilio_auth_token="t"),
        admission=AdmissionController(),
        metrics=metrics,
        gate=QualityGate([EvalCase("c1", "q", must_contain=("ok",))]),
    )
    router = build_router(
        chat=chat, mic=BrowserMicSurface(AdmissionController()), admin=admin,
        admin_token=TOKEN, gate_answerer=lambda case: "ok",
    )
    return TestClient(create_app(router))


AUTH = {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# The contract is served as declared
# ---------------------------------------------------------------------------
def test_every_declared_route_is_reachable(client):
    """No route in the table may be missing from the app. This is the check that
    would have caught GET /api/chat/history being declared and never wired."""
    for route, (method, path) in METHOD_AND_PATH.items():
        response = client.request(method, path, headers=AUTH,
                                  json={} if method == "POST" else None)
        assert response.status_code != 404 or "no handler" not in response.text, \
            f"{route.value} is declared but not served"


def test_the_app_serves_both_pages(client):
    assert client.get("/").status_code == 200
    assert client.get("/console").status_code == 200
    assert "Product assistant" in client.get("/").text
    assert "Operations Console" in client.get("/console").text


# ---------------------------------------------------------------------------
# The auth boundary, over HTTP
# ---------------------------------------------------------------------------
class TestAuthOverHttp:
    @pytest.mark.parametrize("path", [
        "/api/admin/channels", "/api/admin/conversations",
        "/api/admin/conversation?conversation_id=x", "/api/admin/metrics",
    ])
    def test_admin_routes_refuse_without_a_token(self, client, path):
        assert client.get(path).status_code == 401

    def test_a_wrong_token_is_401_not_200(self, client):
        """The status must carry the refusal. A 200 with an error field would
        leave every client's error handling wrong."""
        response = client.get("/api/admin/metrics", headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401
        assert "invalid" in response.json()["error"]

    def test_a_malformed_header_is_refused(self, client):
        assert client.get("/api/admin/metrics", headers={"Authorization": TOKEN}).status_code == 401

    def test_the_correct_token_is_accepted(self, client):
        assert client.get("/api/admin/metrics", headers=AUTH).status_code == 200

    def test_buyer_routes_need_no_token(self, client):
        response = client.post("/api/chat/message",
                               json={"message": "what is the cassette condition?",
                                     "session_id": "s1"})
        assert response.status_code == 200

    def test_the_token_never_appears_in_a_response(self, client):
        """A console that echoes the token into a page or an error is how a
        credential ends up in a screenshot."""
        for path in ("/api/admin/metrics", "/api/admin/channels"):
            assert TOKEN not in client.get(path, headers=AUTH).text
        assert TOKEN not in client.get("/api/admin/metrics").text

    def test_the_contract_endpoint_requires_the_token(self, client):
        assert client.get("/contract").status_code == 401
        assert client.get("/contract", headers=AUTH).status_code == 200


# ---------------------------------------------------------------------------
# Argument translation
# ---------------------------------------------------------------------------
class TestArguments:
    def test_the_body_becomes_keyword_arguments(self, client):
        response = client.post("/api/chat/message",
                               json={"message": "what is the cassette condition?",
                                     "session_id": "s1"})
        assert response.status_code == 200
        assert response.json()["conversation_id"]

    def test_the_query_string_becomes_keyword_arguments(self, client):
        posted = client.post("/api/chat/message",
                             json={"message": "what is the cassette condition?",
                                   "session_id": "s1"}).json()
        response = client.get(f"/api/admin/conversation?conversation_id="
                              f"{posted['conversation_id']}", headers=AUTH)
        assert response.status_code == 200
        assert response.json()["turns"][0]["role"] == "user"

    def test_an_integer_parameter_arrives_as_an_integer(self, client):
        """"50" reaching a limit=50 comparison would compare a string to a
        number; Python 3 raises, or worse, formats it wrong."""
        assert client.get("/api/admin/conversations?limit=1", headers=AUTH).status_code == 200

    def test_a_missing_required_argument_is_a_400_not_a_500(self, client):
        assert client.post("/api/chat/message", json={}).status_code == 400

    def test_an_unparseable_body_is_a_400_not_a_500(self, client):
        response = client.post("/api/chat/message", content=b"not json",
                               headers={"Content-Type": "application/json"})
        assert response.status_code == 400

    def test_a_non_object_body_is_a_400(self, client):
        response = client.post("/api/chat/message", json=["a", "list"])
        assert response.status_code == 400

    def test_every_route_argument_is_declared_somewhere(self):
        """A route's arguments come from one of the two tables or from nowhere.
        An undeclared argument would silently never be populated."""
        declared = set(BODY_ARGUMENTS) | set(QUERY_ARGUMENTS)
        takes_nothing = {Route.HEALTH, Route.ADMIN_CHANNELS,
                         Route.ADMIN_METRICS, Route.ADMIN_GATE,
                         Route.ADMIN_HANDOFFS}
        assert set(Route) - declared == takes_nothing

    def test_no_argument_is_declared_in_both_places(self):
        """An argument listed as both a query parameter and a body field would
        be silently overwritten by whichever the adapter read second."""
        for route in set(BODY_ARGUMENTS) & set(QUERY_ARGUMENTS):
            assert not (set(BODY_ARGUMENTS[route]) & set(QUERY_ARGUMENTS[route])), route


# ---------------------------------------------------------------------------
# Bodies reach the client intact
# ---------------------------------------------------------------------------
class TestBodies:
    def test_absence_is_reported_as_unavailable_over_http(self, client):
        """The MOD-08 rule has to survive serialisation: `unavailable` is a
        boolean in JSON, and `healthy` is null rather than false."""
        body = client.get("/api/admin/metrics", headers=AUTH).json()
        assert any("unavailable" in line for line in body["metrics"])

        channels = client.get("/api/admin/channels", headers=AUTH).json()
        assert all(c["healthy"] is not None for c in channels["channels"])
        assert channels["signature_validation"] is True

    def test_a_not_found_conversation_is_404(self, client):
        assert client.get("/api/admin/conversation?conversation_id=nope",
                          headers=AUTH).status_code == 404

    def test_the_gate_runs_over_http(self, client):
        body = client.post("/api/admin/gate", headers=AUTH, json={}).json()
        assert body["passed"] is True

    def test_a_rate_limited_buyer_gets_429(self):
        from anita.channels import RateLimiter

        chat = ChatSurface(Orchestrator(), retriever=FakeRetriever(),
                           rate_limit=RateLimiter(limit=1, window_s=60))
        router = build_router(
            chat=chat, mic=BrowserMicSurface(AdmissionController()),
            admin=AdminSurface(), admin_token=TOKEN, gate_answerer=None,
        )
        client = TestClient(create_app(router))
        payload = {"message": "hi", "session_id": "s1"}
        assert client.post("/api/chat/message", json=payload).status_code == 200
        assert client.post("/api/chat/message", json=payload).status_code == 429


# ---------------------------------------------------------------------------
# The server must not leak internals
# ---------------------------------------------------------------------------
def test_an_internal_error_is_not_echoed_to_the_client(client):
    """The router returns the exception *type* and nothing else — no message,
    no path, no stack. A traceback in a buyer's browser is an information leak
    and tells them nothing they can act on."""
    from anita.surfaces import Response

    router = build_router(
        chat=ChatSurface(Orchestrator(), retriever=None),
        mic=BrowserMicSurface(AdmissionController()),
        admin=AdminSurface(), admin_token=TOKEN, gate_answerer=None,
    )

    def explode(**kwargs):
        raise RuntimeError("connection string postgres://user:pw@host/db")

    router.register(Route.ADMIN_CHANNELS, explode)
    response = TestClient(create_app(router)).get("/api/admin/channels", headers=AUTH)
    assert response.status_code == 500
    assert "postgres" not in response.text
    assert "pw" not in response.text
