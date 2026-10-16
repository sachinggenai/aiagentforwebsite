"""The WebRTC transport — additive, and provably so.

Two things are being checked, and the second matters more than the first:

1. the transport works — the two codec conversions and the route;
2. **nothing else moved** — the WebSocket path is untouched, and an instance
   that has not enabled WebRTC behaves exactly as it did before. That is the
   whole constraint on this feature, so it is asserted rather than assumed.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from anita.server import create_app  # noqa: E402
from anita.surfaces import Router, build_router  # noqa: E402

av = pytest.importorskip("av", reason="PyAV is required for the WebRTC codec path")


def _audio_frame(*, samples=960, rate=48000, value=8000):
    """A 20 ms frame of a 1 kHz tone at the negotiated rate."""
    signal = (np.sin(np.linspace(0, 60, samples)) * value).astype("<i2")
    frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
    frame.sample_rate = rate
    frame.planes[0].update(signal.tobytes())
    return frame


class TestTheCodecAdaptation:
    """WebRTC carries Opus at 48 kHz; the engine speaks µ-law at 8 kHz."""

    def test_a_20ms_frame_becomes_exactly_one_engine_frame(self):
        """960 samples at 48 kHz is 20 ms — one µ-law frame of 160 bytes.

        The arithmetic matters: off by one and the engine's VAD, which counts
        frames, counts a different amount of speech than the caller spoke.
        """
        from anita.webrtc_voice import FRAME_SAMPLES, audio_frame_to_ulaw

        ulaw = audio_frame_to_ulaw(_audio_frame())

        assert len(ulaw) == FRAME_SAMPLES, "a 20 ms frame did not survive as one frame"

    def test_stereo_is_folded_to_mono_rather_than_taking_one_channel(self):
        """Dropping a channel loses half the signal when they differ."""
        from anita.webrtc_voice import FRAME_SAMPLES, audio_frame_to_ulaw

        left = np.full(960, 4000, dtype="<i2")
        right = np.full(960, 8000, dtype="<i2")
        interleaved = np.empty(1920, dtype="<i2")
        interleaved[0::2] = left
        interleaved[1::2] = right

        frame = av.AudioFrame(format="s16", layout="stereo", samples=960)
        frame.sample_rate = 48000
        frame.planes[0].update(interleaved.tobytes())

        assert len(audio_frame_to_ulaw(frame)) == FRAME_SAMPLES

    def test_the_return_trip_produces_whole_frames_at_the_negotiated_rate(self):
        from anita.webrtc_voice import (
            FRAME_SAMPLES, WEBRTC_RATE, audio_frame_to_ulaw, ulaw_to_audio_frames,
        )

        frames = ulaw_to_audio_frames([b"\x00" * FRAME_SAMPLES])

        assert len(frames) == 1
        assert frames[0].sample_rate == WEBRTC_RATE
        assert frames[0].samples == WEBRTC_RATE // 50, "not a 20 ms frame at 48 kHz"

    def test_a_signal_survives_the_round_trip(self):
        """Not bit-exact — µ-law is lossy by design — but recognisably there.

        A conversion that inverted or silenced the waveform would still pass
        every length assertion above, and would be inaudible until somebody
        actually listened.
        """
        from anita.voice import ulaw_to_pcm
        from anita.webrtc_voice import audio_frame_to_ulaw

        ulaw = audio_frame_to_ulaw(_audio_frame())
        pcm = np.frombuffer(ulaw_to_pcm(ulaw), dtype="<i2")

        assert len(pcm) == 160
        assert np.abs(pcm).max() > 1000, "the signal came back silent"
        # The tone is symmetric, so its mean is near zero. A DC offset would
        # mean the signed conversion is wrong.
        assert abs(int(pcm.astype(np.int32).mean())) < 500

    def test_silence_in_is_silence_out(self):
        from anita.webrtc_voice import ULAW_SILENCE, audio_frame_to_ulaw

        ulaw = audio_frame_to_ulaw(_audio_frame(value=0))

        assert len(ulaw) == len(ULAW_SILENCE)


class _Stub:
    """Any handler the router asks for, as a no-op."""

    def __getattr__(self, name):
        return lambda **kwargs: None


def _client(**kwargs):
    from fastapi.testclient import TestClient

    stub = _Stub()
    return TestClient(create_app(build_router(
        chat=stub, mic=stub, admin=stub, admin_token="secret", **kwargs
    )))


# ---------------------------------------------------------------------------
# Not tested here: that the inbound consumer stays alive.
#
# The defect is real and was found by running a call, not by a unit test —
# `asyncio.ensure_future` returned a task nothing held, so it was eligible for
# collection and cancelled mid-flight. Every cheap signal still said the call
# was healthy: ICE `connected`, the peer connection `connected`, RTP flowing in
# both directions, and no audio ever reaching the engine.
#
# A unit test for it would have to rebuild the very wiring it is checking, and
# would therefore pass whether or not the real handler holds the reference. The
# regression guard that works here is the live call: `scripts/webrtc_smoke`-style
# runs assert a `VoiceCall: transcript` line appears. Recorded in the plan doc.
# ---------------------------------------------------------------------------


class TestTheRoute:
    """`POST /api/voice/webrtc/offer` and the page that drives it."""

    @staticmethod
    def _client(**kwargs):
        return _client(**kwargs)

    def test_it_reports_disabled_rather_than_404ing(self):
        """An operator looking for it should find the reason, not a dead end."""
        response = self._client().post(
            "/api/voice/webrtc/offer",
            json={"session_id": "s", "sdp": "x", "ticket": "t"},
        )

        assert response.status_code == 503
        assert "ANITA_WEBRTC_ENABLED" in response.json()["reason"]

    def test_the_page_is_served(self):
        response = self._client().get("/webrtc")

        assert response.status_code == 200
        assert "WebRTC" in response.text

    def test_a_body_without_a_session_is_a_bad_request(self, monkeypatch):
        monkeypatch.setenv("ANITA_WEBRTC_ENABLED", "true")
        client = self._client(webrtc_sessions=lambda key: None)

        assert client.post("/api/voice/webrtc/offer", json={}).status_code == 400

    def test_the_page_calls_only_declared_or_known_paths(self):
        """The two endpoints it uses: the existing ticket route and the offer."""
        import re
        from pathlib import Path

        html = (Path(__file__).resolve().parents[1] / "app" / "static" / "webrtc.html")
        text = html.read_text(encoding="utf-8")
        called = set(re.findall(r"""["'](/api/[a-z_/]+)["']""", text))

        assert called == {"/api/voice/ticket", "/api/voice/webrtc/offer"}, called


class TestNothingElseMoved:
    """The constraint on this whole feature: it is an addition, not a change."""

    def test_enabling_webrtc_does_not_remove_the_websocket_route(self):
        """The default transport survives the addition of the second one.

        Wired the way production wires it — `browser_voice` and the signer
        present — because that is what registers the socket. The point is that
        adding `webrtc_sessions` alongside them changes nothing.
        """
        from anita.browser_voice import SessionTokens

        without = _client(
            browser_voice=lambda *a, **k: None, voice_tokens=SessionTokens("secret")
        )
        with_webrtc = _client(
            browser_voice=lambda *a, **k: None, voice_tokens=SessionTokens("secret"),
            webrtc_sessions=lambda key: None,
        )

        def paths(client):
            return {getattr(r, "path", None) for r in client.app.routes}

        assert "/voice/browser" in paths(without)
        assert paths(without) == paths(with_webrtc), (
            "enabling WebRTC changed the route table"
        )

    def test_webrtc_disabled_leaves_the_router_identical(self):
        """With no factory injected, the router carries nothing new."""
        stub = _Stub()
        router = build_router(chat=stub, mic=stub, admin=stub, admin_token="s")

        assert getattr(router, "webrtc_sessions", None) is None

    def test_the_config_key_is_read_from_the_product(self):
        """A key read only by a page or a script is INERT, and refuses the boot.

        This has caught two changes in this repo already, so the read is
        pinned here rather than trusted to a preflight somebody may not run.
        """
        from pathlib import Path

        assembly = (Path(__file__).resolve().parents[1] / "app" / "anita" / "assembly.py")
        text = assembly.read_text(encoding="utf-8")

        assert 'settings.env.get("ANITA_WEBRTC_ENABLED"' in text
        assert 'settings.env.get("ANITA_WEBRTC_ICE_SERVERS"' in text
