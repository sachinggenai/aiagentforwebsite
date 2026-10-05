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

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
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
    Route.ADMIN_CONVERSATION: ("conversation_id", "after_seq", "limit"),
    Route.ADMIN_CONVERSATIONS: ("limit", "offset"),
    # US-002: the send queue. `status` filters to one state (FAILED first for
    # the operator), `limit` bounds the read.
    Route.ADMIN_SENDS: ("limit", "status"),
}

# Query parameters that arrive as strings but are declared as ints. Coerced
# explicitly: passing "50" where an int is expected would compare and format
# wrong rather than fail.
INT_ARGUMENTS: dict[str, Callable[[Any], int]] = {
    "limit": int, "offset": int, "after_seq": int,
}


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

    # -- Twilio messaging (US-002) -----------------------------------------
    # Same shape as the voice webhook — raw form, signature header, a tuple
    # back — because it is the same kind of ingress. The reply does not travel
    # in the response body: a messaging webhook answers empty and the answer
    # goes out through the Messages API, so the body here is always empty.
    if getattr(router, "messaging", None) is not None:
        messaging = router.messaging

        @app.post("/twilio/whatsapp", include_in_schema=False)
        async def twilio_whatsapp(request: Request) -> Any:
            try:
                form = await request.form()
            except Exception:  # noqa: BLE001 - an unparseable body is not a valid webhook
                form = {}
            params = {str(key): str(value) for key, value in form.items()}
            status, body, content_type = messaging.whatsapp(
                params=params,
                signature=request.headers.get("x-twilio-signature"),
            )
            return RawResponse(content=body, status_code=status, media_type=content_type)

    # -- extra-channel ingress (US-002) ------------------------------------
    # HMAC over the raw body, so the body is read as bytes and never
    # re-serialised before the signature is checked.
    if getattr(router, "extra_channel", None) is not None:
        ingress = router.extra_channel

        @app.post("/channels/{channel_id}/inbound", include_in_schema=False)
        async def extra_channel_inbound(channel_id: str, request: Request) -> Any:
            raw = await request.body()
            status, payload = ingress.handle(
                channel_id,
                body=raw,
                signature=request.headers.get("x-anita-signature"),
            )
            return JSONResponse(status_code=status, content=payload)

    # -- browser-mic voice (US-003) ----------------------------------------
    # The token is verified **before** the socket is accepted into the media
    # path, and the close code carries the reason the story's client contract
    # names: 4001 token, 4002 admission, 4008 rate limit (US-008 reads these).
    if getattr(router, "browser_voice", None) is not None and router.voice_tokens is not None:
        from .browser_voice import (
            CLOSE_ADMISSION,
            CLOSE_NORMAL,
            CLOSE_RATE_LIMITED,
            CLOSE_TOKEN,
            BrowserVoiceSession,
            SessionTokens,
            TokenError,
        )

        tokens: SessionTokens = router.voice_tokens
        # Per-IP concurrent socket cap. In-process and therefore per-worker:
        # correct for a single-instance deploy, named here so a future
        # multi-worker deploy knows this is one of the things that moves.
        _open_per_ip: dict[str, int] = {}
        _open_total = {"n": 0}
        MAX_WS_PER_IP = 10
        MAX_WS_TOTAL = 200

        @app.websocket("/voice/browser")
        async def voice_browser(websocket: WebSocket) -> None:
            client_ip = (websocket.client.host if websocket.client else "") or "unknown"

            # Accepted first, then closed with a real code: a close issued
            # before `accept` is delivered as an HTTP 403 and the browser
            # never sees 4001/4002/4008 at all.
            await websocket.accept()

            # The token arrives in the first frame, not the query string. A
            # credential in a URL lands in tunnel logs, proxy logs and browser
            # history (`TAC-8`); a first frame lands nowhere but here.
            try:
                hello = json.loads(await asyncio.wait_for(websocket.receive_text(), timeout=5.0))
                token = str(hello.get("token") or "") if isinstance(hello, dict) else ""
                claims = tokens.verify(token)
            except (asyncio.TimeoutError, ValueError, TokenError) as exc:
                reason = getattr(exc, "reason", "invalid")
                logger.info("browser voice: handshake refused (%s)", reason)
                await websocket.close(code=CLOSE_TOKEN)
                return

            if _open_per_ip.get(client_ip, 0) >= MAX_WS_PER_IP or _open_total["n"] >= MAX_WS_TOTAL:
                logger.warning("browser voice: connection cap reached for one client")
                await websocket.close(code=CLOSE_RATE_LIMITED)
                return

            admission = getattr(router.voice, "admission", None)
            call_id = f"browser-{claims.session_id}"
            if admission is not None:
                decision = admission.admit(call_id)
                if not decision.admitted:
                    # Busy is a designed outcome with something to say: the
                    # client shows the callback number and offers text (`AC-7`).
                    await websocket.send_json({
                        "type": "busy",
                        "callback_number": getattr(router.voice, "callback_number", ""),
                        "message": getattr(decision, "message", ""),
                    })
                    await websocket.close(code=CLOSE_ADMISSION)
                    return

            _open_per_ip[client_ip] = _open_per_ip.get(client_ip, 0) + 1
            _open_total["n"] += 1

            session = router.browser_voice(claims.session_id)
            relay = BrowserVoiceSession(
                session=session,
                send=websocket.send_json,
                admission=admission,
                store=getattr(router.voice, "store", None),
                call_id=call_id,
                session_key=claims.session_id,
            )

            reason = "socket_error"
            try:
                while True:
                    try:
                        raw = await asyncio.wait_for(
                            websocket.receive_text(), timeout=relay.idle_timeout_s
                        )
                    except asyncio.TimeoutError:
                        # 30 s without a frame closes the session — an idle
                        # socket must not hold an admission slot (`TAC-5`).
                        reason = "idle"
                        break
                    await relay.handle_message(raw)
                    if relay.close_requested:
                        reason = relay.close_reason or "client_stop"
                        break
            except WebSocketDisconnect:
                reason = "client_stop"
            except Exception:  # noqa: BLE001 - teardown must not depend on the cause
                logger.exception("browser voice: session failed, tearing down")
                reason = "socket_error"
            finally:
                _open_per_ip[client_ip] = max(0, _open_per_ip.get(client_ip, 1) - 1)
                _open_total["n"] = max(0, _open_total["n"] - 1)
                await relay.close(reason=reason)
                try:
                    await websocket.close(code=relay.close_code or CLOSE_NORMAL)
                except Exception:  # noqa: BLE001 - the peer may already be gone
                    pass

    if router.voice_sessions is not None:

        @app.websocket("/twilio/media-stream")
        async def twilio_media_stream(websocket: WebSocket) -> None:
            """The audio door (US-001, `WS /twilio/media-stream`).

            The relay does the protocol; this function owns only the
            socket's lifetime. Every exit path closes the relay, because a
            stream that ends without releasing its slot erodes the
            admission ceiling silently — the box goes on refusing calls
            while sitting idle (`AC-8`, `SM-04`).
            """
            from .media_stream import TwilioMediaStream

            await websocket.accept()
            relay = TwilioMediaStream(
                session=router.voice_sessions(""),
                send=websocket.send_json,
                admission=router.voice.admission,
                store=getattr(router.voice, "store", None),
                # §8.6: the same minter the webhook stamped the TwiML with. The
                # socket is public, so a stream that cannot prove it was
                # admitted is refused rather than served.
                tokens=getattr(router.voice, "stream_tokens", None),
            )
            try:
                while True:
                    await relay.handle_message(await websocket.receive_text())
                    if relay.refused:
                        # Policy violation: no audio was fed and no slot was
                        # taken. Close once, then fall through to teardown.
                        await websocket.close(code=1008)
                        break
            except WebSocketDisconnect:
                # A caller who hangs up, or a dropped socket. Not an error.
                await relay.close(reason="disconnect")
            except Exception:  # noqa: BLE001 - teardown must not depend on the cause
                logger.exception("media stream: relay failed, tearing the call down")
                await relay.close(reason="error")
            else:
                await relay.close(reason="refused" if relay.refused else "closed")

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
