"""US-001 — the Twilio media-stream relay.

The relay is driven with the exact JSON frames Twilio sends and a fake engine
session, so these run without a socket, a carrier, or a loaded STT/TTS model.
What is under test is the protocol handling and the teardown discipline — the
two things that are wrong in a way nobody notices until a real call.

The real audio round trip (Whisper -> answer engine -> Kokoro -> µ-law) is
proven separately: the answer path in `voice_smoke` terms and the models by
`scripts/voice_e2e.py`.
"""
import asyncio
import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from anita.media_stream import MARK_EVERY_N_FRAMES, TwilioMediaStream  # noqa: E402
from anita.speech import AdmissionController  # noqa: E402
from anita.voice import (  # noqa: E402
    FRAME_ECHO,
    FRAME_INTERRUPTION,
    FRAME_LISTENING,
    FRAME_MUTED,
)

CALL_SID = "CA1234567890abcdef"
STREAM_SID = "MZ9876543210"


def _audio_frame(byte=0xFF) -> str:
    return base64.b64encode(bytes([byte]) * 160).decode("ascii")


def _start(**params) -> dict:
    custom = {"call_id": "call-1"}
    custom.update(params.pop("customParameters", {}))
    return {
        "event": "start",
        "start": {
            "streamSid": STREAM_SID,
            "callSid": CALL_SID,
            "from": params.pop("from", "+15551234567"),
            "customParameters": custom,
        },
    }


def _media(payload=None) -> dict:
    return {
        "event": "media",
        "media": {"payload": payload if payload is not None else _audio_frame()},
    }


class FakeSession:
    """Stands in for VoiceCallSession. Records what it was asked to do."""

    def __init__(self, *, end_after=1, verdict=FRAME_LISTENING, audio=None,
                 chunk_sink_used=False, transcript="caller asked about the gel"):
        self.call_id = ""
        self.end_after = end_after
        self.verdict = verdict
        self.audio = audio if audio is not None else [b"\x7f" * 160] * 2
        self.chunk_sink_used = chunk_sink_used
        self.transcript = transcript
        self.frames_seen = 0
        self.admitted = 0
        self.processed = 0
        self.truncated_marks = 0

    def note_answer_truncated(self) -> None:
        self.truncated_marks += 1

    def feed_audio(self, frame: bytes) -> bool:
        self.frames_seen += 1
        return self.frames_seen >= self.end_after

    def admit_inbound_frame(self, frame: bytes) -> str:
        self.admitted += 1
        return self.verdict

    async def process_utterance(self, chunk_sink=None):
        self.processed += 1
        if chunk_sink is not None and self.chunk_sink_used:
            await chunk_sink(self.audio)
        return self.audio, self.transcript, False


def _relay(session=None, admission=None):
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    relay = TwilioMediaStream(
        session=session or FakeSession(), send=send, admission=admission
    )
    return relay, sent


async def _drive(relay, messages) -> None:
    for message in messages:
        await relay.handle_message(json.dumps(message))
    if relay._turn is not None:
        await relay._turn


def _media_events(sent) -> list[dict]:
    return [m for m in sent if m["event"] == "media"]


class TestProtocol:
    def test_start_captures_the_call_identity(self):
        relay, _ = _relay()
        asyncio.run(relay.handle_message(json.dumps(_start())))
        assert relay.stream_sid == STREAM_SID
        assert relay.call_id == "call-1"
        assert relay.caller == "+15551234567"

    def test_the_engine_is_given_the_call_id(self):
        """The engine keys its trace and conversation on this."""
        session = FakeSession()
        relay, _ = _relay(session)
        asyncio.run(relay.handle_message(json.dumps(_start())))
        assert session.call_id == "call-1"

    def test_a_duplicate_start_does_not_restart_the_conversation(self):
        """UC-03 duplicate row: the second `start` is ignored."""
        session = FakeSession()
        relay, _ = _relay(session)
        asyncio.run(relay.handle_message(json.dumps(_start())))
        asyncio.run(relay.handle_message(json.dumps(_start())))
        assert relay.stream_sid == STREAM_SID

    def test_a_malformed_frame_is_dropped_not_raised(self):
        relay, _ = _relay()
        asyncio.run(relay.handle_message("this is not json"))
        asyncio.run(relay.handle_message(json.dumps([1, 2, 3])))
        asyncio.run(relay.handle_message(json.dumps({"event": "something-new"})))

    def test_an_undecodable_payload_is_dropped(self):
        relay, _ = _relay()
        asyncio.run(relay.handle_message(json.dumps(_start())))
        asyncio.run(relay.handle_message(json.dumps(_media(payload="!!!not base64!!!"))))


class TestAudioIn:
    def test_frames_reach_the_engine(self):
        session = FakeSession(end_after=99)
        relay, _ = _relay(session)
        asyncio.run(_drive(relay, [_start(), _media(), _media()]))
        assert session.frames_seen == 2

    def test_the_payload_is_base64_decoded_before_the_engine_sees_it(self):
        """Twilio sends base64; the engine works in raw µ-law bytes."""
        seen: list[bytes] = []

        class Recorder(FakeSession):
            def feed_audio(self, frame):
                seen.append(frame)
                return False

        relay, _ = _relay(Recorder())
        asyncio.run(_drive(relay, [_start(), _media()]))
        assert len(seen) == 1
        assert len(seen[0]) == 160, "one 20 ms frame at 8 kHz is 160 bytes"
        assert seen[0] == bytes([0xFF]) * 160


class TestAudioOut:
    def test_end_of_speech_sends_the_answer_back_as_media(self):
        session = FakeSession(audio=[b"\x11" * 160, b"\x22" * 160])
        relay, sent = _relay(session)
        asyncio.run(_drive(relay, [_start(), _media()]))

        events = _media_events(sent)
        assert len(events) == 2
        assert events[0]["streamSid"] == STREAM_SID
        assert base64.b64decode(events[0]["media"]["payload"]) == b"\x11" * 160

    def test_audio_streamed_through_the_sink_is_not_sent_twice(self):
        """`process_utterance` returns the same frames it streamed.

        That is its batch-path contract. Sending the returned list as well
        would make every clause of every answer play twice.
        """
        session = FakeSession(audio=[b"\x11" * 160] * 3, chunk_sink_used=True)
        relay, sent = _relay(session)
        asyncio.run(_drive(relay, [_start(), _media()]))
        assert len(_media_events(sent)) == 3, "each streamed frame exactly once"

    def test_a_mark_is_sent_for_flow_control(self):
        session = FakeSession(audio=[b"\x11" * 160] * (MARK_EVERY_N_FRAMES + 1))
        relay, sent = _relay(session)
        asyncio.run(_drive(relay, [_start(), _media()]))
        marks = [m for m in sent if m["event"] == "mark"]
        assert len(marks) == 1
        assert marks[0]["streamSid"] == STREAM_SID

    def test_no_mark_before_the_interval(self):
        session = FakeSession(audio=[b"\x11" * 160] * 2)
        relay, sent = _relay(session)
        asyncio.run(_drive(relay, [_start(), _media()]))
        assert not [m for m in sent if m["event"] == "mark"]

    def test_the_transcript_is_retained(self):
        relay, _ = _relay()
        asyncio.run(_drive(relay, [_start(), _media()]))
        assert relay.transcript == ["caller asked about the gel"]


class TestBargeIn:
    def test_frames_while_speaking_go_to_the_barge_in_gate(self):
        """AC-3: the engine decides, the relay does not.

        The echo gate must compare the frame against what is playing out, which
        only the engine knows.
        """
        session = FakeSession(end_after=1)
        relay, _ = _relay(session)
        asyncio.run(relay.handle_message(json.dumps(_start())))
        asyncio.run(relay.handle_message(json.dumps(_media())))

        relay._speaking = True  # as it is while the answer plays
        asyncio.run(relay.handle_message(json.dumps(_media())))
        assert session.admitted == 1

    @pytest.mark.parametrize("verdict", [FRAME_ECHO, FRAME_MUTED])
    def test_echo_and_muted_frames_are_not_fed(self, verdict):
        """The agent's own voice returning through the caller's mic is not
        speech, and feeding it would make the assistant answer itself."""
        session = FakeSession(end_after=99, verdict=verdict)
        relay, _ = _relay(session)
        asyncio.run(relay.handle_message(json.dumps(_start())))
        relay._speaking = True
        asyncio.run(relay.handle_message(json.dumps(_media())))
        assert session.frames_seen == 0

    @pytest.mark.parametrize("verdict", [FRAME_LISTENING, FRAME_INTERRUPTION])
    def test_an_admitted_frame_is_fed(self, verdict):
        session = FakeSession(end_after=99, verdict=verdict)
        relay, _ = _relay(session)
        asyncio.run(relay.handle_message(json.dumps(_start())))
        relay._speaking = True
        asyncio.run(relay.handle_message(json.dumps(_media())))
        assert session.frames_seen == 1

    def test_clear_playout_tells_twilio_to_drop_buffered_audio(self):
        """Without it, the audio the caller just interrupted keeps playing out
        of Twilio's buffer — which sounds exactly like barge-in not working."""
        relay, sent = _relay()
        asyncio.run(relay.handle_message(json.dumps(_start())))
        asyncio.run(relay.clear_playout())
        assert any(m["event"] == "clear" for m in sent)


class TestTeardown:
    def test_stop_releases_the_admission_slot(self):
        admission = AdmissionController(max_concurrent_calls=1)
        admission.admit("call-1")
        assert admission.active_calls() == 1

        relay, _ = _relay(admission=admission)
        asyncio.run(_drive(relay, [_start(), {"event": "stop", "stop": {}}]))

        assert admission.active_calls() == 0

    def test_an_abnormal_close_still_releases_the_slot(self):
        """AC-8: a socket that drops without a Twilio `stop` must not leak
        capacity, or the box refuses calls while sitting idle."""
        admission = AdmissionController(max_concurrent_calls=1)
        admission.admit("call-1")

        relay, _ = _relay(admission=admission)
        asyncio.run(relay.handle_message(json.dumps(_start())))
        asyncio.run(relay.close(reason="disconnect"))

        assert admission.active_calls() == 0

    def test_close_is_idempotent(self):
        """Closing twice must not release two slots.

        The relay's call (`call-1`) is admitted alongside an unrelated call, so
        a second release would be visible as the unrelated slot disappearing.
        """
        admission = AdmissionController(max_concurrent_calls=4)
        admission.admit("call-1")
        admission.admit("someone-else")

        relay, _ = _relay(admission=admission)
        asyncio.run(relay.handle_message(json.dumps(_start())))
        asyncio.run(relay.close(reason="stop"))
        asyncio.run(relay.close(reason="disconnect"))

        assert admission.active_calls() == 1
        assert "someone-else" in admission._active, (
            "a second close released a slot belonging to another call"
        )

    def test_no_audio_is_sent_after_close(self):
        relay, sent = _relay()
        asyncio.run(relay.handle_message(json.dumps(_start())))
        asyncio.run(relay.close(reason="stop"))
        before = len(sent)
        asyncio.run(relay.send_frames([b"\x11" * 160]))
        assert len(sent) == before

    def test_a_closed_stream_ignores_further_frames(self):
        session = FakeSession(end_after=99)
        relay, _ = _relay(session)
        asyncio.run(relay.handle_message(json.dumps(_start())))
        asyncio.run(relay.close(reason="stop"))
        asyncio.run(relay.handle_message(json.dumps(_media())))
        assert session.frames_seen == 0


class TestTheSocketRoute:
    """The relay as an object is not the same as the relay being served.

    Everything above drives `TwilioMediaStream` directly. This drives the real
    ASGI WebSocket route, which is what catches a route that is written but
    never registered — the state the HTTP webhook was in until it was wired.
    """

    def _client(self, session, admission=None, tokens=None):
        from fastapi.testclient import TestClient

        from anita.server import create_app
        from anita.surfaces import Router
        from anita.voice_webhook import VoiceWebhook

        webhook = VoiceWebhook(
            auth_token="t", public_base_url="https://a.example.com",
            admission=admission, stream_tokens=tokens,
        )
        router = Router(voice=webhook, voice_sessions=lambda _call_id: session)
        return TestClient(create_app(router))

    def test_the_route_serves_an_admitted_call_that_presents_its_token(self):
        """§8.6 over the real socket: the carrier's stream, which carries the
        parameter the webhook minted, is served as before."""
        from anita.browser_voice import SessionTokens

        tokens = SessionTokens("test-secret")
        admission = AdmissionController(max_concurrent_calls=2)
        admission.admit("call-1")
        session = FakeSession(end_after=99)
        client = self._client(session, admission=admission, tokens=tokens)

        with client.websocket_connect("/twilio/media-stream") as ws:
            ws.send_text(json.dumps(_start(
                customParameters={"token": tokens.mint("call-1")}
            )))
            ws.send_text(json.dumps(_media()))

        assert session.frames_seen == 1
        assert session.call_id == "call-1"

    def test_the_route_refuses_a_stream_that_was_never_admitted(self):
        """A direct `wss://` connection with no token is served nothing and,
        at teardown, releases nothing."""
        from anita.browser_voice import SessionTokens

        tokens = SessionTokens("test-secret")
        admission = AdmissionController(max_concurrent_calls=2)
        admission.admit("call-1")          # a live call the forgery would name
        session = FakeSession()
        client = self._client(session, admission=admission, tokens=tokens)

        with client.websocket_connect("/twilio/media-stream") as ws:
            ws.send_text(json.dumps(_start()))
            ws.send_text(json.dumps(_media()))

        assert session.frames_seen == 0, "a refused stream must not reach the engine"
        assert session.call_id == ""
        assert admission.holds("call-1"), "the live call must keep its slot"

    def test_frames_sent_over_the_socket_reach_the_engine(self):
        session = FakeSession(end_after=99)
        client = self._client(session)

        with client.websocket_connect("/twilio/media-stream") as ws:
            ws.send_text(json.dumps(_start()))
            ws.send_text(json.dumps(_media()))
            ws.send_text(json.dumps(_media()))

        assert session.frames_seen == 2
        assert session.call_id == "call-1"

    def test_the_answer_is_written_back_over_the_socket(self):
        session = FakeSession(audio=[b"\x11" * 160, b"\x22" * 160])
        client = self._client(session)

        with client.websocket_connect("/twilio/media-stream") as ws:
            ws.send_text(json.dumps(_start()))
            ws.send_text(json.dumps(_media()))
            first = ws.receive_json()
            second = ws.receive_json()

        assert first["event"] == "media"
        assert base64.b64decode(first["media"]["payload"]) == b"\x11" * 160
        assert second["event"] == "media"

    def test_a_dropped_socket_releases_the_slot(self):
        """AC-8 over the real transport: the caller hangs up mid-call."""
        admission = AdmissionController(max_concurrent_calls=2)
        admission.admit("call-1")
        session = FakeSession(end_after=99)
        client = self._client(session, admission)

        with client.websocket_connect("/twilio/media-stream") as ws:
            ws.send_text(json.dumps(_start()))
            assert admission.active_calls() == 1
        # Leaving the context closes the socket without a Twilio `stop`.

        assert admission.active_calls() == 0


class TestTheStreamIsAdmitted:
    """§8.6 — the media-stream socket is public, so it must prove it belongs to
    an admitted call.

    Before this, the handler accepted any connection and the relay only ever
    *released* a slot: a direct `wss://` connection drove STT/TTS with no
    admission, and a forged `call_id` released the slot of the live call it
    named. Both are reproduced below as the tests that fail without the gate.
    """

    def _tokens(self):
        from anita.browser_voice import SessionTokens

        return SessionTokens("test-secret")

    def test_a_stream_with_no_token_is_refused(self):
        tokens = self._tokens()
        session = FakeSession()
        relay, sent = _relay(session=session)
        relay.tokens = tokens

        asyncio.run(_drive(relay, [_start(), _media()]))

        assert relay.refused, "a tokenless stream must not run"
        assert session.frames_seen == 0, "refused audio must never reach the engine"
        assert session.call_id == ""
        assert _media_events(sent) == []

    def test_a_token_for_another_call_is_refused(self):
        tokens = self._tokens()
        relay, _ = _relay()
        relay.tokens = tokens
        forged = tokens.mint("some-other-call")

        asyncio.run(relay.handle_message(json.dumps(
            _start(customParameters={"token": forged})
        )))

        assert relay.refused == "token does not match the call"

    def test_an_expired_or_garbage_token_is_refused(self):
        relay, _ = _relay()
        relay.tokens = self._tokens()

        asyncio.run(relay.handle_message(json.dumps(
            _start(customParameters={"token": "not.a.token"})
        )))

        assert relay.refused.startswith("token ")

    def test_a_stream_for_a_call_that_holds_no_slot_is_refused(self):
        """A valid token is not enough on its own: the call must still hold the
        slot the webhook took, so a stream arriving after the slot was reaped
        cannot resurrect the call."""
        admission = AdmissionController(max_concurrent_calls=2)
        tokens = self._tokens()
        relay, _ = _relay(admission=admission)
        relay.tokens = tokens

        asyncio.run(relay.handle_message(json.dumps(
            _start(customParameters={"token": tokens.mint("call-1")})
        )))

        assert relay.refused == "no admitted call for this stream"

    def test_an_admitted_call_with_a_valid_token_is_served(self):
        admission = AdmissionController(max_concurrent_calls=2)
        admission.admit("call-1")
        tokens = self._tokens()
        session = FakeSession()
        relay, sent = _relay(session=session, admission=admission)
        relay.tokens = tokens

        asyncio.run(_drive(relay, [
            _start(customParameters={"token": tokens.mint("call-1")}),
            _media(),
        ]))

        assert relay.refused == ""
        assert session.call_id == "call-1"
        assert session.frames_seen == 1
        assert _media_events(sent), "the answer must still come back"

    def test_a_refused_stream_cannot_release_a_live_calls_slot(self):
        """The forgery this closes: name a live call on a socket you opened
        without a token, then disconnect — the live call must keep its slot."""
        admission = AdmissionController(max_concurrent_calls=2)
        admission.admit("call-1")          # the real, in-progress call
        tokens = self._tokens()

        relay, _ = _relay(admission=admission)
        relay.tokens = tokens
        asyncio.run(relay.handle_message(json.dumps(_start())))  # no token
        asyncio.run(relay.close(reason="disconnect"))

        assert admission.holds("call-1"), "a refused socket released a slot it never held"

    def test_a_relay_without_a_verifier_still_runs(self):
        """Tests and harnesses drive the relay directly with no minter
        installed; the gate must not make the relay unusable there."""
        session = FakeSession()
        relay, _ = _relay(session=session)
        asyncio.run(_drive(relay, [_start(), _media()]))
        assert relay.refused == ""
        assert session.frames_seen == 1


class TestTheSessionRow:
    """The relay keeps a session row keyed on the stream SID."""

    @staticmethod
    def _store():
        from anita.channel_store import ChannelStore

        return ChannelStore(":memory:")

    def test_start_opens_a_session_keyed_on_the_stream_id(self):
        store = self._store()
        relay, _ = _relay()
        relay.store = store
        asyncio.run(relay.handle_message(json.dumps(_start())))

        found = store.find_session(channel_kind="voice_inbound", session_key=STREAM_SID)
        assert found is not None
        assert found["closed_at"] is None

    def test_a_duplicate_start_does_not_open_a_second_session(self):
        store = self._store()
        relay, _ = _relay()
        relay.store = store
        asyncio.run(relay.handle_message(json.dumps(_start())))
        asyncio.run(relay.handle_message(json.dumps(_start())))
        assert len(store.open_sessions()) == 1

    def test_close_closes_the_session(self):
        store = self._store()
        relay, _ = _relay()
        relay.store = store
        asyncio.run(relay.handle_message(json.dumps(_start())))
        asyncio.run(relay.close(reason="disconnect"))

        assert store.open_sessions() == []

    def test_a_broken_store_does_not_drop_the_call(self):
        """Bookkeeping must never take a call down: the caller is mid-sentence
        and the failure is ours, not theirs."""
        class Broken:
            def open_session(self, **kwargs):
                raise RuntimeError("database is gone")

            def close_session(self, **kwargs):
                raise RuntimeError("database is gone")

        session = FakeSession(end_after=99)
        relay, _ = _relay(session)
        relay.store = Broken()
        asyncio.run(relay.handle_message(json.dumps(_start())))
        asyncio.run(relay.handle_message(json.dumps(_media())))
        asyncio.run(relay.close(reason="stop"))

        assert session.frames_seen == 1, "the call continued despite the store"


class TestCallIdIsNeverLoggedInFull:
    def test_the_mask_keeps_only_the_tail(self):
        """TAC-8 / REC-14: no caller identifier in a log line."""
        from anita.media_stream import _mask

        assert _mask("CA1234567890abcdef") == "…cdef"
        assert "CA1234" not in _mask("CA1234567890abcdef")
        assert _mask("") == "****"


class TestTheCallCeiling:
    """TAC-6 at the relay: the cap has to fire when nothing is happening.

    Frames are the only thing arriving on a call that never reaches
    end-of-speech, so the per-frame check is the only place the ceiling can be
    noticed during silence -- which is exactly the call it exists for.
    """

    class CappedSession(FakeSession):
        async def process_utterance(self, chunk_sink=None):
            self.processed += 1
            return [b"\x11" * 160], "Assistant: goodbye", True

        def call_expired(self) -> bool:
            return True

    def test_a_capped_call_is_closed_by_the_relay(self):
        admission = AdmissionController(max_concurrent_calls=2)
        admission.admit("call-1")
        session = self.CappedSession()
        relay, sent = _relay(session, admission)

        asyncio.run(relay.handle_message(json.dumps(_start())))
        asyncio.run(relay.handle_message(json.dumps(_media())))
        if relay._turn is not None:
            asyncio.run(relay._turn)

        assert session.processed == 1, "the ceiling forced a turn"
        assert relay._closed, "the call closed rather than running on"
        assert admission.active_calls() == 0, "and released its slot"
        assert _media_events(sent), "the caller heard why before the line closed"

    def test_an_uncapped_call_is_left_alone(self):
        session = FakeSession(end_after=99)
        relay, _ = _relay(session)
        asyncio.run(relay.handle_message(json.dumps(_start())))
        asyncio.run(relay.handle_message(json.dumps(_media())))
        assert session.processed == 0
        assert not relay._closed

    def test_a_session_without_the_cap_still_works(self):
        """The relay must not require the method -- a session double, or a
        future engine, should not have to implement the ceiling to be driven."""
        class NoCap:
            call_id = ""

            def feed_audio(self, frame):
                return False

            def admit_inbound_frame(self, frame):
                return FRAME_LISTENING

        relay, _ = _relay(NoCap())
        asyncio.run(relay.handle_message(json.dumps(_start())))
        asyncio.run(relay.handle_message(json.dumps(_media())))
        assert not relay._closed


class TestUS017BargeInClearsPlayout:
    """The engine stopping is only half a barge-in.

    Twilio keeps playing audio it has already buffered, so without a `clear`
    frame the interrupted answer keeps talking out of the carrier's buffer —
    which sounds exactly like the interruption failed. `clear_playout()` existed
    and had no caller in `app/`.
    """

    def _speak_over(self, verdict):
        session = FakeSession(end_after=99, verdict=verdict)
        relay, sent = _relay(session)
        asyncio.run(relay.handle_message(json.dumps(_start())))
        relay._speaking = True  # as it is while the answer plays
        asyncio.run(relay.handle_message(json.dumps(_media())))
        return session, sent

    def test_an_admitted_interruption_clears_twilios_buffer(self):
        _session, sent = self._speak_over(FRAME_INTERRUPTION)
        clears = [m for m in sent if m["event"] == "clear"]
        assert len(clears) == 1, f"expected exactly one clear, got {len(clears)}"
        assert clears[0]["streamSid"] == STREAM_SID

    def test_an_admitted_interruption_marks_the_answer_truncated(self):
        """The transcript must record that the caller did not hear the end."""
        session, _sent = self._speak_over(FRAME_INTERRUPTION)
        assert session.truncated_marks == 1

    @pytest.mark.parametrize("verdict", [FRAME_ECHO, FRAME_MUTED])
    def test_echo_and_muted_frames_do_not_clear(self, verdict):
        """The assistant must not stop its own answer for its own voice."""
        session, sent = self._speak_over(verdict)
        assert not [m for m in sent if m["event"] == "clear"]
        assert session.truncated_marks == 0

    def test_a_second_admitted_frame_does_not_re_signal(self):
        """One barge-in, one clear: after admission the engine returns to
        LISTENING, so the caller's continuing speech must not clear again."""
        session = FakeSession(end_after=99, verdict=FRAME_INTERRUPTION)
        relay, sent = _relay(session)
        asyncio.run(relay.handle_message(json.dumps(_start())))
        relay._speaking = True
        asyncio.run(relay.handle_message(json.dumps(_media())))
        session.verdict = FRAME_LISTENING  # as it is once the turn yields
        asyncio.run(relay.handle_message(json.dumps(_media())))

        clears = [m for m in sent if m["event"] == "clear"]
        assert len(clears) == 1, f"re-signalled on continuing speech: {len(clears)} clears"
        assert session.truncated_marks == 1
