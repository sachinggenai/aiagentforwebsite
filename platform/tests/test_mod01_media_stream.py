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

    def _client(self, session, admission=None):
        from fastapi.testclient import TestClient

        from anita.server import create_app
        from anita.surfaces import Router
        from anita.voice_webhook import VoiceWebhook

        webhook = VoiceWebhook(
            auth_token="t", public_base_url="https://a.example.com", admission=admission
        )
        router = Router(voice=webhook, voice_sessions=lambda _call_id: session)
        return TestClient(create_app(router))

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


class TestCallIdIsNeverLoggedInFull:
    def test_the_mask_keeps_only_the_tail(self):
        """TAC-8 / REC-14: no caller identifier in a log line."""
        from anita.media_stream import _mask

        assert _mask("CA1234567890abcdef") == "…cdef"
        assert "CA1234" not in _mask("CA1234567890abcdef")
        assert _mask("") == "****"
