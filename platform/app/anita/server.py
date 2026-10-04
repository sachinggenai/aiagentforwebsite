"""The HTTP adapter (MOD-10).

Thin by design. Every route, every auth decision and every response body is
decided in :mod:`anita.surfaces`; this module only translates HTTP into that
vocabulary and back. Nothing here is allowed to make a product decision — if
this file starts containing business logic, the logic is in the wrong place,
because none of it would be reachable from a test that does not start a server.

The route table in ``surfaces.ROUTE_TABLE`` is the single source of truth. The
paths registered below are read from it, so a route added there is served here
automatically or fails loudly at import.

**Argument mapping.** Each route declares which parameters come from the query
string and which from the JSON body. That mapping lives in one table because it
is the only part of the contract that HTTP adds — and getting it wrong is how a
route silently ignores a caller's input.

**The admin token** is read per-request from the ``Authorization: Bearer``
header and passed to the router, which compares it in constant time. The
adapter never compares it itself, and never logs it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi import Response as RawResponse
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .surfaces import Route, Response, Router

logger = logging.getLogger("anita.server")

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"

# HTTP method and path for each route, derived from the route's own value so the
# two can never disagree: "GET /api/chat/history" -> ("GET", "/api/chat/history").
METHOD_AND_PATH: dict[Route, tuple[str, str]] = {
    route: tuple(route.value.split(" ", 1)) for route in Route  # type: ignore[arg-type]
}

# Where each route's handler arguments come from. Anything not listed is taken
# from the query string; the body form is used for the two POST routes that
# carry a payload. Listing them explicitly means a new route with a body fails
# here rather than silently reading an empty query parameter.
BODY_ARGUMENTS: dict[Route, tuple[str, ...]] = {
    Route.CHAT_MESSAGE: ("message", "session_id"),
    Route.VOICE_TICKET: ("session_id",),
}
QUERY_ARGUMENTS: dict[Route, tuple[str, ...]] = {
    Route.CHAT_HISTORY: ("session_id",),
    Route.ADMIN_CONVERSATION: ("conversation_id",),
    Route.ADMIN_CONVERSATIONS: ("limit",),
}

# Query parameters that arrive as strings but are declared as ints. Coerced
# explicitly: passing "50" where an int is expected would compare and format
# wrong rather than fail.
INT_ARGUMENTS: dict[str, Callable[[Any], int]] = {"limit": int}


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    prefix = "bearer "
    if header.lower().startswith(prefix):
        return header[len(prefix):].strip()
    return None


async def _arguments(request: Request, route: Route) -> dict[str, Any]:
    """Collect this route's declared arguments from the request."""
    arguments: dict[str, Any] = {}

    for name in QUERY_ARGUMENTS.get(route, ()):
        value = request.query_params.get(name)
        if value is None:
            continue
        arguments[name] = INT_ARGUMENTS.get(name, str)(value)

    body_fields = BODY_ARGUMENTS.get(route)
    if body_fields:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - an unparseable body is a bad request
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        for name in body_fields:
            if name in payload:
                arguments[name] = payload[name]

    return arguments


def _as_json(response: Response) -> JSONResponse:
    """Translate a surface Response. The status is preserved exactly — a 401
    from the auth boundary must reach the caller as a 401, not as a 200 with an
    error field, or every client's error handling is wrong."""
    body = response.body
    if not isinstance(body, (dict, list)):
        body = {"detail": body}
    return JSONResponse(status_code=response.status, content=body)


def create_app(router: Router, *, title: str = "ANITA MEDICAL SYSTEMS") -> FastAPI:
    """Build the ASGI app around an already-wired :class:`Router`.

    Taking a built router rather than building one keeps the boot sequence
    (config, isolation, corpus, index) outside the web layer, where it can run
    and fail before a socket is ever opened.
    """
    app = FastAPI(title=title, docs_url=None, redoc_url=None, openapi_url=None)

    # Register every route from the table. The paths are not written out here —
    # they come from Route itself, so this loop cannot drift from the contract.
    for route, (method, path) in METHOD_AND_PATH.items():
        app.add_api_route(
            path,
            _make_handler(router, route),
            methods=[method],
            name=route.name.lower(),
            include_in_schema=False,
        )

    # -- Twilio ingress (US-001) -------------------------------------------
    # Registered here rather than through the route table: `Route` values are
    # JSON endpoints dispatched through `Router.dispatch`, and a Twilio webhook
    # is neither. It takes a form-encoded body, must be allowed to answer 403
    # with an empty body, and replies in TwiML.
    if router.voice is not None:
        webhook = router.voice

        async def _handle_twilio(request: Request, kind: str) -> Any:
            """Read the form and the signature, then let the webhook decide.

            The **raw form fields** are what gets validated, because that is
            what Twilio signs — sorted key/value pairs, not a parsed subset. A
            handler that validated `request.json()` or a hand-picked field
            would accept bodies Twilio never signed.
            """
            try:
                form = await request.form()
            except Exception:  # noqa: BLE001 - an unparseable body is not a valid webhook
                form = {}
            params = {str(key): str(value) for key, value in form.items()}
            signature = request.headers.get("x-twilio-signature")

            handler = webhook.voice if kind == "voice" else webhook.status
            status, body, content_type = handler(params=params, signature=signature)
            return RawResponse(
                content=body, status_code=status, media_type=content_type
            )

        @app.post("/twilio/voice", include_in_schema=False)
        async def twilio_voice(request: Request) -> Any:
            return await _handle_twilio(request, "voice")

        @app.post("/twilio/status", include_in_schema=False)
        async def twilio_status(request: Request) -> Any:
            return await _handle_twilio(request, "status")

    @app.get("/", include_in_schema=False)
    async def chat_page() -> Any:
        return _page("chat.html")

    @app.get("/console", include_in_schema=False)
    async def console_page() -> Any:
        return _page("console.html")

    @app.get("/contract", include_in_schema=False)
    async def contract(request: Request) -> Any:
        """The route table, for review. Authenticated with the same token as the
        admin routes: it is a map of the surface, which is not buyer information.
        """
        from .surfaces import authenticate, AuthError

        try:
            authenticate(Route.ADMIN_CHANNELS, _bearer(request), router.admin_token)
        except AuthError as exc:
            return JSONResponse(status_code=401, content={"error": str(exc)})
        return JSONResponse(content=router.contract())

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


def _page(name: str) -> Any:
    path = STATIC_DIR / name
    if not path.exists():
        return PlainTextResponse(f"{name} is not built", status_code=503)
    return FileResponse(path, media_type="text/html; charset=utf-8")


def _make_handler(router: Router, route: Route) -> Callable[[Request], Any]:
    """One handler closure per route, dispatching through the surface router.

    Every one of these is identical, which is the point: the differences between
    routes live in the route table and in the surfaces, not here.
    """

    async def handler(request: Request) -> Any:
        arguments = await _arguments(request, route)
        try:
            response = router.dispatch(route, token=_bearer(request), **arguments)
        except Exception:  # noqa: BLE001 - the adapter must not 500 silently
            logger.exception("unhandled error dispatching %s", route.value)
            return JSONResponse(status_code=500, content={"error": "internal error"})

        if route is Route.HEALTH and response.status == 200:
            return _as_json(response)
        return _as_json(response)

    handler.__name__ = f"handler_{route.name.lower()}"
    return handler


def serve(router: Router, *, host: str = "127.0.0.1", port: int = 8100) -> None:
    """Run the app. Import of uvicorn is deferred so the module can be imported
    (and tested) without a server dependency present."""
    import uvicorn

    uvicorn.run(create_app(router), host=host, port=port, log_level="info")


def app_from_environment() -> FastAPI:
    """Assemble from ``ANITA_`` settings, then build the app around the router.

    This previously passed the whole ``Assembly`` where ``create_app`` expects a
    ``Router``, so the first request would have raised ``AttributeError`` on
    ``Assembly.dispatch``. It was never reached: the live entry point
    (``scripts/serve.py``) builds the app from ``built.router`` directly.
    """
    from .assembly import assemble

    built = assemble()
    app = create_app(built.router)
    # Kept for scripts that want the rest of the assembly from the app handle.
    app.state.assembly = built
    return app


__all__ = ["create_app", "serve", "app_from_environment", "METHOD_AND_PATH",
           "BODY_ARGUMENTS", "QUERY_ARGUMENTS", "STATIC_DIR"]


# A JSON round trip is the cheapest way to prove a body is serialisable before
# a socket is involved; used by the tests' response helper, not at runtime.
def _serialisable(payload: Any) -> bool:
    try:
        json.dumps(payload, default=str)
        return True
    except (TypeError, ValueError):
        return False
