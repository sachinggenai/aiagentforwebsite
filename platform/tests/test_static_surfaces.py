"""Static surfaces — the pages and the route table must not drift apart.

The HTML pages are hand-written and have no build step, so nothing at runtime
would notice a page calling a route that no longer exists, or a route added to
the table that no surface uses. These tests are that notice.

They parse the pages as text. That is deliberate: the pages must stay readable
and dependency-free, so there is no template engine to introspect and no
bundler output to inspect.

Run:  .venv\\Scripts\\python -m pytest tests -q
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.surfaces import ROUTE_TABLE, Route, Surface, requires_auth  # noqa: E402

STATIC = PLATFORM / "app" / "static"
CHAT = STATIC / "chat.html"
CONSOLE = STATIC / "console.html"

# Every "/api/..." or "/health" literal a page fetches or posts to. The query
# string is dropped — "/api/admin/conversation?conversation_id=..." is the same
# route as the declared "/api/admin/conversation".
PATH_RE = re.compile(r"""["'](/(?:api/[a-z_/]+|health))(?:\?[^"']*)?["']""")


def paths_in(page: Path) -> set[str]:
    return set(PATH_RE.findall(page.read_text(encoding="utf-8")))


def declared_paths(surface: Surface | None = None) -> set[str]:
    """Route paths, optionally restricted to one surface."""
    out = set()
    for route, (route_surface, _) in ROUTE_TABLE.items():
        if surface is None or route_surface is surface:
            out.add(route.value.split(" ", 1)[1])  # "GET /api/x" -> "/api/x"
    return out


# ---------------------------------------------------------------------------
# The pages exist
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("page", [CHAT, CONSOLE])
def test_the_page_exists_and_is_self_contained(page: Path):
    """No build step, no external dependency: the page has to work from a phone
    on a bad connection, so everything is inline."""
    assert page.exists(), f"{page.name} has not been built"
    html = page.read_text(encoding="utf-8")
    assert len(html) > 2000, f"{page.name} looks truncated"
    for pattern in (r'<script[^>]+src=', r'<link[^>]+rel=["\']stylesheet'):
        assert not re.search(pattern, html, re.I), \
            f"{page.name} loads an external asset; the surfaces must be self-contained"


@pytest.mark.parametrize("page", [CHAT, CONSOLE])
def test_the_page_is_responsive(page: Path):
    html = page.read_text(encoding="utf-8")
    assert "width=device-width" in html, f"{page.name} is not mobile-usable"
    assert "@media" in html, f"{page.name} has no responsive breakpoint"


# ---------------------------------------------------------------------------
# The pages and the route table agree
# ---------------------------------------------------------------------------
def test_the_console_calls_every_admin_route():
    """An admin route no page calls is dead weight; a page calling a route that
    is not declared would 404 in production and never in a test."""
    assert paths_in(CONSOLE) == declared_paths(Surface.ADMIN_CONSOLE) - {"/health"}


def test_the_chat_page_calls_only_buyer_routes():
    called = paths_in(CHAT)
    assert "/api/chat/message" in called
    assert "/api/voice/ticket" in called
    assert called <= declared_paths(), f"chat.html calls undeclared routes: {called}"


def test_the_console_calls_only_declared_routes():
    called = paths_in(CONSOLE)
    assert called <= declared_paths(), f"console.html calls undeclared routes: {called}"


def test_no_page_reaches_an_admin_route_it_should_not():
    """The buyer page must never call an admin route — that is CV-03's boundary,
    and a page is exactly where it would be crossed by accident. /health is
    exempt: it is declared on the admin surface because the console reads it,
    but it is unauthenticated and a buyer page may use it as a liveness probe."""
    crossed = paths_in(CHAT) & (declared_paths(Surface.ADMIN_CONSOLE) - {"/health"})
    assert not crossed, f"chat.html reaches admin routes: {crossed}"


def test_every_admin_route_is_authenticated():
    """Belt and braces with the surfaces test: stated here because this file is
    where a *page* could quietly start relying on an open admin route."""
    for route, (surface, _) in ROUTE_TABLE.items():
        if surface is Surface.ADMIN_CONSOLE and route is not Route.HEALTH:
            assert requires_auth(route), route


# ---------------------------------------------------------------------------
# The console's auth boundary — DG-06's UI half
# ---------------------------------------------------------------------------
def test_the_console_sends_the_bearer_token():
    html = CONSOLE.read_text(encoding="utf-8")
    assert '"Authorization": "Bearer " + token' in html


def test_the_console_does_not_persist_the_token_across_sessions():
    """sessionStorage, not localStorage: this console is opened deliberately and
    should not leave a credential on a shared workstation."""
    html = CONSOLE.read_text(encoding="utf-8")
    # The calls, not the word — the page's comment explains why it avoids
    # localStorage, and naming it must not fail the test.
    assert not re.search(r"localStorage\s*\.", html)
    assert re.search(r"sessionStorage\s*\.", html)


def test_the_console_never_puts_the_token_in_a_url():
    html = CONSOLE.read_text(encoding="utf-8")
    assert "?token=" not in html
    assert "&token=" not in html


def test_the_console_clears_the_token_when_disconnected():
    html = CONSOLE.read_text(encoding="utf-8")
    assert "sessionStorage.removeItem" in html


# ---------------------------------------------------------------------------
# Absence is not zero — the MOD-08 rule, in the UI
# ---------------------------------------------------------------------------
def test_the_console_distinguishes_absent_from_zero():
    """A dashboard reporting zero during an outage is worse than one reporting
    nothing. The page must have a distinct rendering for each."""
    html = CONSOLE.read_text(encoding="utf-8")
    assert "function unavailable(" in html
    assert "unavailable" in html and "unavailable</span>" in html
    # Zero is styled differently from a real measurement and from absence.
    assert re.search(r"\.unavailable\s*\{", html)
    assert re.search(r"\.zero\s*\{", html)


def test_the_console_renders_an_unmeasured_channel_as_unknown():
    """healthy === null is unmeasured. Rendering it green would be a lie, and
    rendering it red would page someone for nothing."""
    html = CONSOLE.read_text(encoding="utf-8")
    assert "c.healthy === null" in html


def test_the_console_does_not_poll_with_sse():
    """REC-16: the tunnel returns SSE headers but never the body, so a stream
    that looks connected would silently stop updating. The console polls."""
    html = CONSOLE.read_text(encoding="utf-8")
    assert "EventSource" not in html
    assert "setInterval" in html


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------
def test_the_console_escapes_server_data():
    """The console builds HTML from strings, so every value that reached it from
    the server must go through the escaper — turn content and conversation ids
    are corpus- and buyer-derived."""
    html = CONSOLE.read_text(encoding="utf-8")
    assert "function esc(" in html

    # The rule that is actually checkable: a server-supplied value is never
    # string-concatenated into markup. It is either passed straight to esc(),
    # or collected into a local that is escaped as a whole when interpolated.
    # A bare `+ t.content +` would be the XSS, and it cannot appear.
    server_values = ("t.content", "t.intent", "t.role", "c.channels_seen",
                     "c.conversation_id", "c.name", "c.state", "body.reason", "line")
    for value in server_values:
        for pattern in (r"\+\s*" + re.escape(value) + r"\b(?!\s*\|\|)",
                        r"\b" + re.escape(value) + r"\s*\+\s*['\"]"):
            assert not re.search(pattern, html), \
                f"{value} is concatenated into markup: {pattern}"

    # ...and the aggregate built from those values is escaped where it lands.
    assert re.search(r"esc\(meta\)", html)


def test_the_chat_page_never_writes_corpus_text_as_markup():
    """The buyer page has exactly one place corpus data becomes markup: the
    source link. Everything else is textContent, and the link is built with
    createElement so a URL carrying a quote cannot close the attribute."""
    html = CHAT.read_text(encoding="utf-8")
    assert "innerHTML" not in html, \
        "chat.html writes innerHTML; buyer and corpus text must use textContent"
    assert "createElement" in html
    assert "function esc(" in html
    assert "function safeUrl(" in html


def test_the_chat_page_refuses_non_http_source_links():
    """A javascript: or data: URL in the corpus must not become a clickable
    link in a buyer's browser."""
    html = CHAT.read_text(encoding="utf-8")
    assert 'parsed.protocol === "http:"' in html
    assert 'parsed.protocol === "https:"' in html
    assert 'rel = "noopener noreferrer"' in html


# ---------------------------------------------------------------------------
# The browser voice client (US-008)
# ---------------------------------------------------------------------------
# The client is hand-written vanilla JS with no test framework, so the contract
# is asserted the way it is written: by reading the page. These are the
# properties whose absence is a defect a browser test would only *sometimes*
# catch — a state with no sentence, a token written to storage, a dead control.
WORKLET = STATIC / "pcm-worklet.js"


def test_the_capture_worklet_exists_and_emits_pcm16():
    assert WORKLET.exists(), "the AudioWorklet capture module has not been built"
    source = WORKLET.read_text(encoding="utf-8")
    assert "registerProcessor" in source
    assert "Int16Array" in source, "the worklet must emit PCM16, not float"
    assert "16000" in source


def test_the_chat_page_loads_the_worklet_and_has_a_fallback():
    """TAC-2: AudioWorklet first, MediaRecorder when it is unavailable — and
    both paths must exist, because the fallback is what a Safari on an older
    tablet gets."""
    html = CHAT.read_text(encoding="utf-8")
    assert "/static/pcm-worklet.js" in html
    assert "audioWorklet" in html
    assert "MediaRecorder" in html


def test_every_voice_state_has_a_sentence_in_a_status_region():
    """TAC-6: the voice path is never the only path, and a state a sighted
    user reads from a colour must also be text for everyone else."""
    html = CHAT.read_text(encoding="utf-8")
    assert 'role="status"' in html and "aria-live" in html
    for state in ('"idle"', '"mic_requested"', '"connecting"', '"listening"',
                  '"thinking"', '"speaking"', '"ended"', '"degraded"'):
        assert state in html, f"voice state {state} is not in the client"
    assert "voiceSentence" in html
    assert "setVoiceState" in html


def test_the_buyer_page_never_persists_a_voice_session():
    """TAC-5: no localStorage / sessionStorage / IndexedDB on the buyer page.
    The token lives in a closure; a closed tab leaves nothing behind."""
    html = CHAT.read_text(encoding="utf-8")
    assert not re.search(r"localStorage\s*\.", html)
    assert not re.search(r"sessionStorage\s*\.", html)
    assert "indexedDB" not in html


def test_the_voice_token_never_travels_in_a_url():
    """A credential in a query string lands in tunnel logs and history
    (TAC-8). It is sent in the first socket frame instead."""
    html = CHAT.read_text(encoding="utf-8")
    assert '"/voice/browser"' in html
    assert "?token=" not in html
    assert '"auth"' in html and "token)" in html


def test_the_client_can_interrupt_and_flush_playback():
    """AC-2 / TAC-4: the interrupt sends stop_speaking and clears the ordered
    queue; the server's `flush` frame does the same on a server-side barge-in."""
    html = CHAT.read_text(encoding="utf-8")
    assert "stop_speaking" in html
    assert "flushPlayback" in html
    assert "playCursor" in html, "playback must be scheduled as one ordered queue"


def test_the_client_decodes_ulaw_itself():
    """The server sends the engine's own frames (8 kHz µ-law); the client
    decodes them — no dependency, and no second TTS stack."""
    html = CHAT.read_text(encoding="utf-8")
    assert "ULAW_TABLE" in html
    assert "createBuffer" in html


def test_every_voice_failure_lands_in_a_state_with_the_text_path_open():
    """AC-3/4/5/6/7 and CS-4: permission denied, no device, busy, dropped and
    rate-limited all reach DEGRADED with text chat still usable — and the
    phone number is shown whenever the server gave one."""
    html = CHAT.read_text(encoding="utf-8")
    assert "function degrade(" in html
    for reason in ('"denied"', '"unsupported"', '"busy"', '"dropped"', '"rate"'):
        assert reason in html, f"no degraded copy for {reason}"
    assert "callbackNumber" in html
