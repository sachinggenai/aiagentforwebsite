"""Browser-mic voice — US-003 transport, up to and including the served socket.

The relay-level tests drive `BrowserVoiceSession` with the exact frames
US-008's client sends and assert the exact frames it receives; the socket tests
then drive the real ASGI route, which is what catches a transport that exists
but is not registered — the state US-003 was in before this work.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.browser_voice import (  # noqa: E402
    CLOSE_ADMISSION,
    CLOSE_NORMAL,
    CLOSE_RATE_LIMITED,
    CLOSE_TOKEN,
    BrowserVoiceSession,
    SessionTokens,
    TokenError,
    _dialogue_lines,
    pcm16_to_ulaw,
)
from anita.channels import RateLimiter  # noqa: E402
from anita.speech import AdmissionController  # noqa: E402


# ---------------------------------------------------------------------------
# The token (TAC-2)
# ---------------------------------------------------------------------------
class TestSessionTokens:
    def test_a_minted_token_verifies_and_names_its_session(self):
        tokens = SessionTokens("s3cret", ttl_seconds=60, clock=lambda: 1000.0)
        claims = tokens.verify(tokens.mint("sess-9"))
        assert claims.session_id == "sess-9"
        assert claims.expires_at == 1060.0

    def test_a_tampered_token_is_refused(self):
        tokens = SessionTokens("s3cret", clock=lambda: 1000.0)
        token = tokens.mint("sess-9")
        with pytest.raises(TokenError):
            tokens.verify(token[:-1] + ("A" if token[-1] != "A" else "B"))

    def test_a_token_signed_with_another_secret_is_refused(self):
        """The property the old UUID ticket never had: anything that can reach
        the socket cannot mint its way in."""
        minted = SessionTokens("one", clock=lambda: 1000.0).mint("sess-9")
        with pytest.raises(TokenError):
            SessionTokens("two", clock=lambda: 1000.0).verify(minted)

    def test_an_expired_token_is_refused_with_a_named_reason(self):
        clock = {"now": 1000.0}
        tokens = SessionTokens("s3cret", ttl_seconds=60, clock=lambda: clock["now"])
        token = tokens.mint("sess-9")
        clock["now"] = 1061.0
        with pytest.raises(TokenError) as excinfo:
            tokens.verify(token)
        assert excinfo.value.reason == "expired"

    def test_an_empty_secret_signs_nothing(self):
        with pytest.raises(TokenError):
            SessionTokens("").mint("sess-9")


# ---------------------------------------------------------------------------
# PCM16 16 kHz → µ-law 8 kHz
# ---------------------------------------------------------------------------
class TestAudioConversion:
    def test_sixteen_kilohertz_pcm_becomes_eight_kilohertz_ulaw(self):
        """320 samples at 16 kHz is 20 ms, which is 160 µ-law bytes at 8 kHz —
        the engine's frame size on both transports."""
        frame = (b"\x10\x00" * 320)  # 320 samples, little-endian int16
        converted = pcm16_to_ulaw(frame)
        assert len(converted) == 160

    def test_silence_encodes_as_ulaw_silence(self):
        assert pcm16_to_ulaw(b"\x00\x00" * 320) == b"\xff" * 160

    def test_a_partial_bucket_is_left_for_the_next_chunk(self):
        # 161 samples is 80 whole pairs plus one orphan: the output is 80
        # µ-law bytes, and the odd sample is dropped rather than paired with
        # the first sample of the next chunk (which would skew it).
        assert len(pcm16_to_ulaw(b"\x00\x00" * 161)) == 80

    def test_empty_input_is_empty_output(self):
        assert pcm16_to_ulaw(b"") == b""


# ---------------------------------------------------------------------------
# The session protocol
# ---------------------------------------------------------------------------
class FakeEngineSession:
    """The engine boundary, faked: the relay's job is the protocol, not STT."""

    def __init__(self, *, end_after=2, chunks=None, dialogue="Caller: hi\nAssistant: Hello."):
        self.end_after = end_after
        self.frames_seen = 0
        self.barge_in_calls = 0
        self.turns = 0
        self._turn = 0
        self._chunks = chunks if chunks is not None else [b"\x22" * 160]
        self._dialogue = dialogue
        self.traces = []
        self.call_id = ""
        self.mark_speaking_calls = 0
        self.note_playback_calls = 0
        self.end_turn_calls = 0

    def feed_audio(self, frame: bytes) -> bool:
        self.frames_seen += 1
        return self.frames_seen >= self.end_after

    def admit_inbound_frame(self, frame: bytes) -> str:
        return "listening"

    def request_interruption(self) -> None:
        self.barge_in_calls += 1

    async def process_utterance(self, chunk_sink=None):
        self.turns += 1
        self._turn = self.turns
        if chunk_sink is not None and self._chunks:
            await chunk_sink(self._chunks)
        return self._chunks, self._dialogue, False

    def finish_trace(self, *, outcome="answered"):
        self.traces.append(outcome)

    def note_answer_truncated(self):
        pass

    # §8.30: the relay arms the engine's SPEAKING window through these. Recorded
    # here so the wiring is visible at all — with no caller for `mark_speaking`
    # the barge-in gate answers FRAME_LISTENING for every frame.
    def mark_speaking(self, chunks=None):
        self.mark_speaking_calls += 1

    def note_playback(self, frames=None):
        self.note_playback_calls += 1

    def end_turn(self):
        self.end_turn_calls += 1


def _relay(session=None, admission=None, store=None, limiter=None):
    sent: list[dict] = []

    async def send(frame):
        sent.append(frame)

    relay = BrowserVoiceSession(
        session=session or FakeEngineSession(),
        send=send,
        admission=admission,
        store=store,
        call_id="browser-s1",
        session_key="s1",
        turn_limiter=limiter,
    )
    return relay, sent


def _audio_frame(payload: bytes = b"\x00\x00" * 320) -> str:
    return json.dumps({"type": "audio", "pcm16": base64.b64encode(payload).decode()})


class TestTheRelay:
    def test_start_answers_ready(self):
        relay, sent = _relay()
        asyncio.run(relay.handle_message(json.dumps({"type": "start"})))
        assert sent[0]["type"] == "ready"
        assert sent[0]["session_id"] == "s1"

    def test_duplicate_start_frames_are_ignored(self):
        relay, sent = _relay()
        asyncio.run(relay.handle_message(json.dumps({"type": "start"})))
        asyncio.run(relay.handle_message(json.dumps({"type": "start"})))
        assert [frame["type"] for frame in sent].count("ready") == 1

    def test_audio_reaches_the_engine_and_a_turn_answers(self):
        session = FakeEngineSession(end_after=1)
        relay, sent = _relay(session)
        asyncio.run(relay.handle_message(json.dumps({"type": "start"})))
        asyncio.run(relay.handle_message(_audio_frame()))

        async def settle():
            await asyncio.sleep(0)
            if relay._turn is not None:
                await relay._turn

        asyncio.run(settle())

        kinds = [frame["type"] for frame in sent]
        assert session.frames_seen >= 1
        assert "transcript" in kinds and "audio" in kinds and "turn_complete" in kinds
        audio = next(frame for frame in sent if frame["type"] == "audio")
        assert base64.b64decode(audio["ulaw_b64"]) == b"\x22" * 160
        roles = [frame["role"] for frame in sent if frame["type"] == "transcript"]
        assert roles == ["caller", "assistant"]

    def test_a_turn_arms_and_closes_the_engines_speaking_window(self):
        """§8.30 on the browser path: the page's playback queue is the analogue
        of Twilio's playout buffer, so the engine must be told it is speaking
        here too. The `flush` on a barge-in and the truncation mark both hang
        off the verdict this window makes reachable."""
        session = FakeEngineSession(end_after=1)
        relay, _ = _relay(session)
        asyncio.run(relay.handle_message(json.dumps({"type": "start"})))
        asyncio.run(relay.handle_message(_audio_frame()))

        async def settle():
            await asyncio.sleep(0)
            if relay._turn is not None:
                await relay._turn

        asyncio.run(settle())

        assert session.mark_speaking_calls == 1, "the gate was never armed"
        assert session.end_turn_calls == 1, "the window was left open"
        assert relay._playback_open is False

    def test_commit_forces_a_turn_without_a_vad_boundary(self):
        """The MediaRecorder fallback sends the whole utterance at once; there
        is no trailing silence for the engine's VAD to find."""
        session = FakeEngineSession(end_after=99)
        relay, sent = _relay(session)
        asyncio.run(relay.handle_message(json.dumps({"type": "start"})))
        asyncio.run(relay.handle_message(_audio_frame()))
        asyncio.run(relay.handle_message(json.dumps({"type": "commit"})))

        async def settle():
            await asyncio.sleep(0)
            if relay._turn is not None:
                await relay._turn

        asyncio.run(settle())
        assert session.turns == 1

    def test_stop_speaking_asks_the_engine_to_interrupt(self):
        session = FakeEngineSession(end_after=99)
        relay, _ = _relay(session)
        asyncio.run(relay.handle_message(json.dumps({"type": "start"})))
        asyncio.run(relay.handle_message(json.dumps({"type": "stop_speaking"})))
        # While silent it is a no-op; while speaking it reaches the engine.
        assert session.barge_in_calls == 0
        relay._speaking = True
        asyncio.run(relay.handle_message(json.dumps({"type": "stop_speaking"})))
        assert session.barge_in_calls == 1

    def test_the_turn_rate_limit_closes_with_4008(self):
        session = FakeEngineSession(end_after=1)
        relay, sent = _relay(session, limiter=RateLimiter(limit=1, window_s=60))
        asyncio.run(relay.handle_message(json.dumps({"type": "start"})))
        asyncio.run(relay.handle_message(_audio_frame()))  # first turn: allowed
        relay._turn = None  # pretend the first turn finished
        asyncio.run(relay.handle_message(_audio_frame()))  # second: over the limit

        async def settle():
            await asyncio.sleep(0)
            if relay._turn is not None:
                await relay._turn

        asyncio.run(settle())
        assert any(frame.get("code") == "rate_limited" for frame in sent)
        assert relay.close_requested and relay.close_code == CLOSE_RATE_LIMITED

    def test_close_releases_the_slot_and_is_idempotent(self):
        admission = AdmissionController(max_concurrent_calls=2)
        admission.admit("browser-s1")
        relay, _ = _relay(admission=admission)
        asyncio.run(relay.handle_message(json.dumps({"type": "start"})))
        asyncio.run(relay.close(reason="client_stop"))
        asyncio.run(relay.close(reason="socket_error"))
        assert admission.active_calls() == 0
        assert relay.close_reason == "client_stop"

    def test_no_audio_is_sent_after_close(self):
        session = FakeEngineSession(end_after=1)
        relay, sent = _relay(session)
        asyncio.run(relay.close(reason="client_stop"))
        before = len(sent)
        asyncio.run(relay.send_frames([b"\x11" * 160]))
        assert len(sent) == before

    def test_unparseable_frames_are_dropped_not_fatal(self):
        relay, sent = _relay()
        asyncio.run(relay.handle_message("not json"))
        asyncio.run(relay.handle_message(json.dumps(["not", "an", "object"])))
        assert sent == []


class TestTheDialogueSplit:
    def test_both_roles_are_sent_to_the_page(self):
        lines = _dialogue_lines("Caller: what does it weigh?\nAssistant: 4.2 kilograms.")
        assert lines == [("caller", "what does it weigh?"),
                         ("assistant", "4.2 kilograms.")]

    def test_a_multiline_answer_stays_with_its_role(self):
        lines = _dialogue_lines("Caller: hi\nAssistant: first line\nsecond line")
        assert lines == [("caller", "hi"), ("assistant", "first line\nsecond line")]


# ---------------------------------------------------------------------------
# The served socket
# ---------------------------------------------------------------------------
class TestTheSocketRoute:
    def _client(self, *, tokens=None, admission=None, store=None, limiter=None):
        from fastapi.testclient import TestClient

        from anita.server import create_app
        from anita.surfaces import Router
        from anita.voice_webhook import VoiceWebhook

        webhook = VoiceWebhook(
            auth_token="t", public_base_url="https://a.example.com",
            admission=admission, store=store, callback_number="+911234567890",
        )
        session = FakeEngineSession(end_after=2)
        router = Router(
            voice=webhook,
            voice_tokens=tokens or SessionTokens("test-secret", ttl_seconds=60),
            browser_voice=lambda _session_id: session,
            voice_sessions=lambda _call_id: session,
        )
        return TestClient(create_app(router)), session

    def test_the_handshake_answers_ready(self):
        client, _ = self._client()
        tokens = SessionTokens("test-secret", ttl_seconds=60)
        with client.websocket_connect("/voice/browser") as ws:
            ws.send_text(json.dumps({"type": "auth", "token": tokens.mint("s1")}))
            ws.send_text(json.dumps({"type": "start"}))
            ready = ws.receive_json()
        assert ready["type"] == "ready"

    def test_an_unsigned_handshake_closes_4001(self):
        from starlette.websockets import WebSocketDisconnect

        client, _ = self._client()
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/voice/browser") as ws:
                ws.send_text(json.dumps({"type": "auth", "token": "forged"}))
                ws.receive_json()
        assert excinfo.value.code == CLOSE_TOKEN

    def test_an_expired_handshake_closes_4001(self):
        from starlette.websockets import WebSocketDisconnect

        clock = {"now": 1000.0}
        tokens = SessionTokens("test-secret", ttl_seconds=60, clock=lambda: clock["now"])
        token = tokens.mint("s1")
        clock["now"] = 2000.0
        client, _ = self._client(tokens=tokens)
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/voice/browser") as ws:
                ws.send_text(json.dumps({"type": "auth", "token": token}))
                ws.receive_json()
        assert excinfo.value.code == CLOSE_TOKEN

    def test_admission_refused_sends_busy_then_closes_4002(self):
        from starlette.websockets import WebSocketDisconnect

        admission = AdmissionController(max_concurrent_calls=1)
        admission.admit("someone-else")
        client, _ = self._client(admission=admission)
        tokens = SessionTokens("test-secret", ttl_seconds=60)
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/voice/browser") as ws:
                ws.send_text(json.dumps({"type": "auth", "token": tokens.mint("s1")}))
                busy = ws.receive_json()
                assert busy["type"] == "busy"
                assert busy["callback_number"] == "+911234567890"
                ws.receive_json()
        assert excinfo.value.code == CLOSE_ADMISSION

    def test_audio_over_the_socket_is_answered(self):
        client, session = self._client()
        tokens = SessionTokens("test-secret", ttl_seconds=60)
        with client.websocket_connect("/voice/browser") as ws:
            ws.send_text(json.dumps({"type": "auth", "token": tokens.mint("s1")}))
            ws.send_text(json.dumps({"type": "start"}))
            ws.receive_json()  # ready
            ws.send_text(_audio_frame())
            ws.send_text(_audio_frame())
            frames = [ws.receive_json(), ws.receive_json(), ws.receive_json()]
        assert session.frames_seen >= 2
        assert {frame["type"] for frame in frames} >= {"transcript", "audio"}

    def test_a_closed_socket_releases_the_voice_slot(self):
        admission = AdmissionController(max_concurrent_calls=2)
        client, _ = self._client(admission=admission)
        tokens = SessionTokens("test-secret", ttl_seconds=60)
        with client.websocket_connect("/voice/browser") as ws:
            ws.send_text(json.dumps({"type": "auth", "token": tokens.mint("s1")}))
            ws.send_text(json.dumps({"type": "start"}))
            ws.receive_json()
            assert admission.active_calls() == 1
        assert admission.active_calls() == 0


# ---------------------------------------------------------------------------
# The channel tables US-003 adds
# ---------------------------------------------------------------------------
class TestStoreAdditions:
    @staticmethod
    def _store(tmp_path=None):
        from anita.channel_store import ChannelStore

        return ChannelStore(tmp_path / "channels.sqlite" if tmp_path else ":memory:")

    def test_rejections_are_counted_by_class_never_by_body(self):
        store = self._store()
        store.record_rejection(route="/twilio/voice", reason="signature")
        store.record_rejection(route="/twilio/voice", reason="signature")
        store.record_rejection(route="/channels/x/inbound", reason="hmac")
        assert store.rejection_counts() == {"signature": 2, "hmac": 1}

    def test_a_rate_limit_event_stores_a_hash_not_the_identity(self):
        store = self._store()
        store.record_rate_limit(key="+919812345678", limit_class="turns_per_identity")
        events = store.rate_limit_events()
        assert events and events[0]["limit_class"] == "turns_per_identity"
        assert "+919812345678" not in events[0]["bucket_key"]

    def test_a_session_records_why_it_closed(self):
        store = self._store()
        store.open_session(channel_kind="browser_mic", session_key="s1")
        store.close_session(channel_kind="browser_mic", session_key="s1", reason="idle")
        row = store.find_session(channel_kind="browser_mic", session_key="s1")
        assert row["close_reason"] == "idle"

    def test_the_first_close_reason_wins(self):
        store = self._store()
        store.open_session(channel_kind="browser_mic", session_key="s1")
        store.close_session(channel_kind="browser_mic", session_key="s1", reason="idle")
        store.close_session(channel_kind="browser_mic", session_key="s1", reason="socket_error")
        row = store.find_session(channel_kind="browser_mic", session_key="s1")
        assert row["close_reason"] == "idle"

    def test_the_migration_is_idempotent_on_an_existing_database(self, tmp_path):
        """A second boot must not raise on an already-migrated table."""
        first = self._store(tmp_path)
        first.record_rejection(route="/x", reason="signature")
        second = self._store(tmp_path)  # re-opening runs SCHEMA + _migrate again
        assert second.rejection_counts() == {"signature": 1}
