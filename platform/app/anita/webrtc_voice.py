"""The browser-mic channel over WebRTC (US-003's transport, replaced).

A **sibling** of `BrowserVoiceSession`, not a modification of it. The WebSocket
path is untouched and remains the default; this module exists beside it, and an
instance that never enables it behaves exactly as it did before.

Why the shape works: a transport does not have to understand the voice engine.
It has to satisfy four calls —

    session.admit_inbound_frame(ulaw) -> LISTENING | INTERRUPTION | ECHO | MUTED
    session.feed_audio(ulaw)
    session.process_utterance(chunk_sink=...)
    session.mark_speaking() / note_playback() / end_turn() / request_interruption()

— and `BrowserVoiceSession` and the Twilio relay are already two different
callers of exactly those. This is the third, so the engine never learns it
exists and every layer above the seam (admission, tickets, cross-channel
identity, sentiment, escalation) is inherited rather than rebuilt.

**What is different is only the wire.** Two conversions have to happen, and both
are the kind already done elsewhere in this package:

* in  — `av.AudioFrame` (Opus decoded by aiortc, 48 kHz) → mono → 8 kHz → µ-law
* out — µ-law 8 kHz → PCM16 → 48 kHz → `av.AudioFrame` (aiortc encodes to Opus)

The downsampler averages groups of samples rather than dropping them, for the
reason `browser_voice.pcm16_to_ulaw` already documents: dropping aliases speech
energy back into the band the recogniser listens to, and the symptom is a
transcript that is *nearly* right.

**aiortc is imported here, and this module is imported lazily** by the route
that needs it. A box without aiortc installed must still boot and serve every
other surface.
"""

from __future__ import annotations

import asyncio
import fractions
import logging
import time

import numpy as np

from .voice import FRAME_MUTED, pcm_to_ulaw, ulaw_to_pcm

logger = logging.getLogger("anita.webrtc_voice")

#: The engine's frame: 20 ms of 8 kHz µ-law.
FRAME_SAMPLES = 160
FRAME_SECONDS = 0.02
ENGINE_RATE = 8000

#: What Opus is negotiated at. aiortc decodes to this before we see a frame.
WEBRTC_RATE = 48000

#: µ-law silence. An idle outbound track sends this rather than nothing, so the
#: stream keeps its timing — a track that stops producing stalls the jitter
#: buffer at the far end and the resume sounds like a stutter.
ULAW_SILENCE = bytes([0xFF]) * FRAME_SAMPLES


def audio_frame_to_ulaw(frame) -> bytes:
    """One `av.AudioFrame` → the engine's µ-law, downsampled by averaging."""
    channels = int(getattr(getattr(frame, "layout", None), "nb_channels", 1) or 1)
    array = frame.to_ndarray()
    if array.ndim == 1:
        array = array.reshape(1, -1)

    # PyAV returns a *packed* layout (plane 0 holding L,R,L,R…) as a single
    # row, not as one row per channel. Read as mono, a stereo frame is 1920
    # samples rather than 960 — which would emit 40 ms of audio for a 20 ms
    # frame and desynchronise the VAD that counts those frames. De-interleave
    # first, then fold.
    if array.shape[0] == 1 and channels > 1:
        array = array.reshape(-1, channels).T

    mono = array.mean(axis=0) if array.shape[0] > 1 else array[0]

    source_rate = int(getattr(frame, "sample_rate", 0) or WEBRTC_RATE)
    factor = max(1, int(round(source_rate / ENGINE_RATE)))
    if factor > 1:
        usable = len(mono) - (len(mono) % factor)
        if usable == 0:
            return b""
        mono = mono[:usable].reshape(-1, factor).mean(axis=1)

    pcm = mono.round().astype("<i2").tobytes()
    return pcm_to_ulaw(pcm)


def ulaw_to_audio_frames(chunks: list[bytes], *, rate: int = WEBRTC_RATE):
    """µ-law 8 kHz chunks → `av.AudioFrame`s at the negotiated rate.

    Upsampling repeats each sample rather than interpolating. At 6× the
    difference is inaudible against an 8 kHz source, and interpolation would be
    inventing detail the recogniser never had.
    """
    import av

    factor = max(1, rate // ENGINE_RATE)
    frames = []
    for chunk in chunks:
        if not chunk:
            continue
        samples = np.frombuffer(ulaw_to_pcm(chunk), dtype="<i2")
        if factor > 1:
            samples = np.repeat(samples, factor)
        frame = av.AudioFrame(format="s16", layout="mono", samples=len(samples))
        frame.sample_rate = rate
        frame.planes[0].update(samples.tobytes())
        frames.append(frame)
    return frames


def make_answer_track():
    """The assistant's voice, as an aiortc audio track.

    **A real `MediaStreamTrack` subclass, built here rather than wrapped.**
    aiortc checks track types internally, and a duck-typed stand-in is accepted
    by `addTrack` and then quietly not driven — which looks exactly like a
    connection that works and never carries audio.

    It paces itself: aiortc calls `recv()` in a loop and sends whatever comes
    back immediately, so a track that returns as fast as it is asked floods the
    socket. The 20 ms wait is what makes this real time, and the silence frames
    are what keep the far end's jitter buffer from stalling when the assistant
    has nothing to say.
    """
    from aiortc import MediaStreamTrack

    class AnswerTrack(MediaStreamTrack):
        kind = "audio"

        def __init__(self) -> None:
            super().__init__()
            self._buffer: list = []
            self._next_send = 0.0
            self._pts = 0
            #: Counted so "the connection is up and no audio moved" is
            #: observable rather than inferred.
            self.frames_sent = 0

        async def recv(self):
            now = time.monotonic()
            if not self._next_send:
                self._next_send = now
            delay = self._next_send - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_send += FRAME_SECONDS

            frame = self._buffer.pop(0) if self._buffer else _silence_frame()
            frame.pts = self._pts
            frame.time_base = fractions.Fraction(1, WEBRTC_RATE)
            self._pts += frame.samples
            self.frames_sent += 1
            return frame

        def push(self, frames) -> None:
            self._buffer.extend(frames)

        def flush(self) -> None:
            """Drop what has not been sent — the analogue of Twilio `clear`."""
            self._buffer.clear()

    return AnswerTrack()


def _silence_frame():
    import av

    samples = WEBRTC_RATE // 50          # 20 ms
    frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
    frame.sample_rate = WEBRTC_RATE
    frame.planes[0].update(b"\x00" * (samples * 2))
    return frame


__all__ = [
    "WEBRTC_RATE", "ENGINE_RATE", "FRAME_SAMPLES", "ULAW_SILENCE",
    "audio_frame_to_ulaw", "ulaw_to_audio_frames", "WebRTCVoiceSession",
    "WebRTCDisabled",
]


class WebRTCDisabled(RuntimeError):
    """WebRTC was asked for on an instance that has not enabled it."""


class WebRTCVoiceSession:
    """One WebRTC call, driven through the same seam the WebSocket path uses.

    Deliberately a thin transport: it owns signalling, the two codec
    conversions, and the turn bookkeeping that the WebSocket path already
    established. It owns no policy — admission, rate limiting and the answer
    path all arrive injected.
    """

    def __init__(
        self,
        *,
        session,
        session_key: str,
        tokens=None,
        admission=None,
        turn_limiter=None,
        ice_servers=None,
    ) -> None:
        self.session = session
        self.session_key = session_key
        self.tokens = tokens
        self.admission = admission
        self.turn_limiter = turn_limiter
        self.ice_servers = ice_servers or []
        self.transcript: list[str] = []
        self.closed = False

        self._pc = None
        self._track = None
        self._turn = None
        self._speaking = False
        self._pcm_buffer = b""
        #: Held for the life of the call. See `on_track`.
        self._consumer = None
        self._remote_track = None

    # -- signalling --------------------------------------------------------
    async def handle_offer(self, *, sdp: str, kind: str) -> dict:
        """Answer one SDP offer. Returns the local description as a dict."""
        from aiortc import (
            RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription,
        )

        config = RTCConfiguration(
            iceServers=[RTCIceServer(urls=url) for url in self.ice_servers]
        )
        pc = RTCPeerConnection(configuration=config)
        self._pc = pc

        self._track = make_answer_track()
        pc.addTrack(self._track)

        @pc.on("track")
        def on_track(track):
            if track.kind != "audio":
                return
            logger.info("webrtc voice: inbound track opened (%s)", track.kind)
            # **Both references are kept, and neither is optional.** A task
            # created and dropped is eligible for garbage collection, which
            # cancels the coroutine mid-flight — and the symptom is a call that
            # reports `connected` at both ends, carries RTP in both directions,
            # and never reaches the engine. Holding the task and the track is
            # what keeps the consumer alive for the life of the call.
            self._remote_track = track
            self._consumer = asyncio.ensure_future(self._consume(track))

        @pc.on("connectionstatechange")
        async def on_state():
            if pc.connectionState in ("failed", "closed"):
                await self.close(reason=f"webrtc_{pc.connectionState}")

        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=kind))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        # Non-trickle: the answer must already carry the candidates, because
        # the signalling channel here is a single request/response.
        for _ in range(100):
            if pc.iceGatheringState == "complete":
                break
            await asyncio.sleep(0.1)

        if self.admission is not None:
            self.admission.admit(self.session_key)

        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}

    # -- inbound audio -----------------------------------------------------
    async def _consume(self, track) -> None:
        """Decode frames from the browser onto the engine's 20 ms grid."""
        received = 0
        try:
            while not self.closed:
                frame = await track.recv()
                received += 1
                if received % 250 == 1:      # ~5 s of audio
                    logger.info(
                        "webrtc voice: %d inbound frames (%d Hz, %s) — %.1fs of audio",
                        received, getattr(frame, "sample_rate", 0),
                        getattr(getattr(frame, "layout", None), "name", "?"),
                        received * FRAME_SECONDS,
                    )
                ulaw = audio_frame_to_ulaw(frame)
                if not ulaw:
                    continue
                # Frame-size agnostic for the same reason the WebSocket path
                # is: the engine's VAD counts frames, so re-buffer here rather
                # than trusting the sender's chunking.
                self._pcm_buffer += ulaw
                while len(self._pcm_buffer) >= FRAME_SAMPLES:
                    chunk, self._pcm_buffer = (
                        self._pcm_buffer[:FRAME_SAMPLES], self._pcm_buffer[FRAME_SAMPLES:]
                    )
                    await self._feed(chunk)
        except Exception:  # noqa: BLE001 - a dropped track is a dropped call
            if not self.closed:
                logger.exception("webrtc voice: inbound track ended")
                await self.close(reason="track_ended")

    async def _feed(self, frame: bytes) -> None:
        from .voice import FRAME_ECHO, FRAME_INTERRUPTION

        if not frame:
            return
        if self._speaking:
            verdict = self.session.admit_inbound_frame(frame)
            if verdict in (FRAME_ECHO, FRAME_MUTED):
                return
            if verdict == FRAME_INTERRUPTION:
                # The browser's jitter buffer holds audio already sent. Without
                # this the interrupted answer keeps playing from it — the same
                # problem Twilio's `clear` solves on the phone path.
                self._track.flush()
                note = getattr(self.session, "note_answer_truncated", None)
                if note is not None:
                    note()
        elif self._turn is not None and not self._turn.done():
            return

        if self.session.feed_audio(frame):
            self._begin_turn()

    # -- the turn ----------------------------------------------------------
    def _begin_turn(self) -> None:
        if self._turn is not None and not self._turn.done():
            return
        if self.turn_limiter is not None:
            from .channels import RateLimited

            try:
                self.turn_limiter.check(self.session_key)
            except RateLimited:
                asyncio.ensure_future(self.close(reason="rate_limit"))
                return
        self._turn = asyncio.ensure_future(self._run_turn())

    async def _run_turn(self) -> None:
        self._speaking = True
        outcome = "answered"
        try:
            chunks, transcript, end_call = await self.session.process_utterance(
                chunk_sink=self._sink
            )
            if transcript:
                self.transcript.append(transcript)
            if chunks:
                self._track.push(ulaw_to_audio_frames(chunks))
            if end_call:
                await self.close(reason="sign-off")
        except asyncio.CancelledError:
            outcome = "interrupted"
            raise
        except Exception:  # noqa: BLE001 - a turn must not kill the call
            outcome = "failed"
            logger.exception("webrtc voice: turn failed")
        finally:
            self._speaking = False
            # §8.30 on the other two transports: leave the engine's SPEAKING
            # window on every exit path, or the buyer's ordinary speech is
            # gated as echo for the rest of the call.
            end = getattr(self.session, "end_turn", None)
            if end is not None:
                end()
            finish = getattr(self.session, "finish_trace", None)
            if finish is not None:
                finish(outcome=outcome)

    async def _sink(self, frames: list[bytes]) -> None:
        """Streamed clauses, handed over as they are produced."""
        self._track.push(ulaw_to_audio_frames(frames))

    # -- teardown ----------------------------------------------------------
    async def close(self, *, reason: str = "closed") -> None:
        if self.closed:
            return
        self.closed = True
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
        try:
            if self._pc is not None:
                await self._pc.close()
        except Exception:  # noqa: BLE001 - teardown must not raise
            logger.exception("webrtc voice: closing the peer connection failed")
        if self.admission is not None:
            try:
                self.admission.release(self.session_key)
            except Exception:  # noqa: BLE001
                logger.exception("webrtc voice: releasing the slot failed")
        logger.info("webrtc voice: closed (%s)", reason)
