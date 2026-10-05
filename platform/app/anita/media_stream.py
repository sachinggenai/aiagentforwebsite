"""Twilio Media Streams relay (US-001).

**This is new code, not a port.** The source platform's media loop lived in
`app/main.py`, which `REC-10` dropped by design, and `voice.py`'s docstring
refers to a `handle_media` that was never written. Nothing here is recovered
from the sibling; it is written against Twilio's documented protocol and the
engine's actual interface.

Twilio speaks a small JSON protocol over the socket::

    connected            the socket is open
    start                streamSid, callSid, customParameters, from
    media                base64 µ-law, 20 ms frames, 8 kHz
    mark                 playout acknowledgement for flow control
    stop                 the stream ended

The relay holds no decision of its own. End-of-speech belongs to the engine
(`feed_audio` returns True exactly once per utterance), the barge-in verdict
belongs to `admit_inbound_frame`, and the answer belongs to the installed
answer engine. This class moves bytes and keeps the protocol honest.

Two things it deliberately does **not** do, because doing them here would put
a second decision point beside the engine's:

* it never decides the caller has stopped speaking — it asks `feed_audio`
* it never decides a frame is an interruption — it asks `admit_inbound_frame`
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

logger = logging.getLogger("anita.media_stream")

# A `mark` every 25 frames is ~500 ms of audio. Twilio buffers playout and
# acknowledges each mark; sending one per frame would double the message rate
# for no useful resolution.
MARK_EVERY_N_FRAMES = 25

# Twilio's frame size (8 kHz µ-law, 20 ms ⇒ 160 bytes) is deliberately *not* a
# constant here: the relay forwards each payload as it arrives and never slices
# or pads, so a frame-size constant would only be read by tests — the `FRAME_BYTES`
# that sat here had no caller (§8.12).


class MediaStreamError(RuntimeError):
    """The stream cannot continue."""


class TwilioMediaStream:
    """One call's media stream. Protocol in, audio out, no socket of its own.

    The transport is injected as `send`, an async callable taking a JSON-able
    dict. That is what makes the protocol testable without a socket, a carrier,
    or a loaded STT/TTS model — the tests drive `handle_message` with the exact
    JSON Twilio sends and assert what comes back.
    """

    def __init__(self, *, session, send, admission=None, store=None, tokens=None) -> None:
        self.session = session
        self._send = send
        self.admission = admission
        # Verifies the token the webhook minted for an admitted call. Optional
        # so a relay can be driven directly (tests, harnesses); when it is
        # installed the stream is refused without a valid token — the socket is
        # public and an unauthenticated one would drive the engine and release
        # slots it never held (§8.6).
        self.tokens = tokens
        # MOD-01's channel tables. Optional: a relay with no store still
        # carries the call, it just cannot offer a stable identity for a caller
        # who withheld their number, nor leave a row for a restart to reconcile.
        self.store = store

        self.stream_sid = ""
        self.call_id = ""
        self.caller = ""

        self._started = False
        self._closed = False
        #: Why this stream was refused, or "". Set only by `_on_start`.
        self.refused = ""
        self._frames_since_mark = 0
        self._marks_outstanding = 0

        # The turn currently being answered, if any. Held so the receive loop
        # keeps running while the engine works — a relay that awaited the
        # answer inline could not hear the caller interrupt it.
        self._turn: asyncio.Task | None = None
        self._speaking = False
        # Whether audio reached the socket through the streaming sink. When it
        # did, `process_utterance` still returns the same frames (that is its
        # batch contract) and sending them again would speak every clause twice.
        self._streamed = False

        self.transcript: list[str] = []

    # -- protocol ----------------------------------------------------------
    async def handle_message(self, raw: str) -> None:
        """Dispatch one frame from Twilio. Never raises on a malformed frame."""
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("media stream: unparseable frame dropped")
            return
        if not isinstance(message, dict):
            return

        event = message.get("event")
        if event == "start":
            await self._on_start(message.get("start") or {})
        elif event == "media":
            await self._on_media(message.get("media") or {})
        elif event == "mark":
            await self._on_mark(message.get("mark") or {})
        elif event == "stop":
            await self._on_stop(message.get("stop") or {})
        elif event == "connected":
            # Nothing to do: `start` carries everything needed. Accepted so an
            # unknown event never looks like an error.
            logger.debug("media stream: connected")
        else:
            logger.debug("media stream: ignoring event %r", event)

    async def _on_start(self, start: dict) -> None:
        """Capture the call's identity. Idempotent by streamSid (UC-03: a
        duplicate `start` must not begin a second conversation)."""
        stream_sid = str(start.get("streamSid") or "")
        if self._started:
            if stream_sid and stream_sid != self.stream_sid:
                logger.warning("media stream: a second start arrived for a different stream")
            return
        self._started = True
        self.stream_sid = stream_sid

        # `call_id` is what we put in the TwiML `<Parameter>`, so the relay
        # learns which call it holds from the stream itself rather than from a
        # table it would have to keep in step.
        params = start.get("customParameters") or {}
        self.call_id = str(params.get("call_id") or start.get("callSid") or "")
        self.caller = str(start.get("from") or "")

        # §8.6: the socket is public and Twilio is not the only thing that can
        # open one. Before any audio is fed, this stream must present the token
        # the webhook minted for an admitted call. A `call_id` alone is a value
        # anyone can guess or read out of a log, and a guessed one would both
        # drive the engine and — at teardown — release the slot of the call it
        # named.
        self.refused = self._refusal(params) or ""
        if self.refused:
            logger.warning("media stream: refused (%s)", self.refused)
            return

        # The engine keys its trace and its conversation on this.
        self.session.call_id = self.call_id
        # US-004: the caller's number from the carrier. Read here and, until
        # this change, dropped — so the voice path could not recognise a buyer
        # who had already spoken to the web chat (`BRD-02`).
        if hasattr(self.session, "caller"):
            self.session.caller = self.caller
        if hasattr(self.session, "channel_kind"):
            self.session.channel_kind = "voice_inbound"

        # The session row is keyed on the stream SID, not the phone number: a
        # caller who withholds their number has no other stable identity for
        # this call (`AC-6`). Idempotent, so a duplicate `start` cannot open a
        # second one.
        if self.store is not None and self.stream_sid:
            try:
                self.store.open_session(
                    channel_kind="voice_inbound", session_key=self.stream_sid
                )
            except Exception:  # noqa: BLE001 - bookkeeping must not drop a call
                logger.exception("media stream: could not open the session row")

        logger.info("media stream: started (call %s)", _mask(self.call_id))

    def _refusal(self, params: dict) -> str | None:
        """Why this stream must not run, or `None` when it may.

        Two questions, in order: did the webhook mint this call's token (so the
        stream is the one Twilio was told to open), and does the call actually
        hold a slot right now (so a stream that arrives after the slot was
        reaped cannot resurrect it)?
        """
        if self.tokens is not None:
            from .browser_voice import TokenError

            try:
                token = self.tokens.verify(str(params.get("token") or ""))
            except TokenError as exc:
                return f"token {exc.reason}"
            if token.session_id != self.call_id:
                return "token does not match the call"
        if self.admission is not None:
            if not self.call_id:
                return "no call id"
            if not self.admission.holds(self.call_id):
                return "no admitted call for this stream"
        return None

    async def _on_media(self, media: dict) -> None:
        """One 20 ms frame of caller audio."""
        if self._closed or self.refused:
            # Twilio can deliver frames that were already in flight when the
            # stream ended. Feeding them would append audio to a conversation
            # that has been closed and whose slot is already released.
            return
        # TAC-6. Frames are the only thing arriving on a call where nothing is
        # happening, so this is the one place the ceiling can be noticed during
        # silence — and a call that never reaches end-of-speech is exactly the
        # call the cap exists for. Forcing the turn lets `process_utterance`
        # speak the closing line and return `end_call`, which tears the call
        # down through the normal path rather than a special one.
        if getattr(self.session, "call_expired", None) is not None:
            if self.session.call_expired():
                self._begin_turn()
                return

        payload = media.get("payload")
        if not payload:
            return
        try:
            frame = base64.b64decode(payload)
        except (TypeError, ValueError):
            logger.warning("media stream: undecodable media payload dropped")
            return

        if self._speaking:
            # The agent is talking. Whether this frame is the caller
            # interrupting is the engine's decision, not the relay's — the echo
            # gate has to compare it against what is currently playing out.
            from .voice import FRAME_ECHO, FRAME_INTERRUPTION, FRAME_MUTED

            verdict = self.session.admit_inbound_frame(frame)
            if verdict in (FRAME_ECHO, FRAME_MUTED):
                return
            # US-017: on the *transition* into an interruption — and only then.
            # Once admitted, the engine returns to LISTENING, so the caller's
            # continuing speech arrives as FRAME_LISTENING and must not
            # re-signal: one barge-in, one `clear`, one truncation mark.
            if verdict == FRAME_INTERRUPTION:
                # The engine stopping is only half a barge-in. Twilio is still
                # holding audio it has buffered but not played, so without this
                # the interrupted answer keeps talking out of the carrier's
                # buffer — which sounds exactly like the interruption failed.
                await self.clear_playout()
                # And the transcript must record that the answer was cut short;
                # a model that believes it delivered an answer the caller never
                # finished hearing will refuse to repeat it. No-op on the
                # streamed path, where the history already holds only what was
                # spoken.
                note = getattr(self.session, "note_answer_truncated", None)
                if note is not None:
                    note()
            # The frame is ordinary caller audio from here and belongs to the
            # one end-of-speech decision.
        elif self._turn is not None and not self._turn.done():
            return

        if self.session.feed_audio(frame):
            self._begin_turn()

    async def _on_mark(self, mark: dict) -> None:
        self._marks_outstanding = max(0, self._marks_outstanding - 1)

    async def _on_stop(self, stop: dict) -> None:
        await self.close(reason="stop")

    # -- the turn ----------------------------------------------------------
    def _begin_turn(self) -> None:
        """Run the answer off the receive loop.

        Inline would be simpler and wrong: while `process_utterance` awaits the
        LLM, nothing is reading the socket, so the caller cannot interrupt the
        answer they are listening to. The task is what makes barge-in possible
        at all.
        """
        if self._turn is not None and not self._turn.done():
            return
        self._turn = asyncio.create_task(self._run_turn())

    async def _run_turn(self) -> None:
        self._speaking = True
        self._streamed = False
        outcome = "answered"
        try:
            chunks, transcript, end_call = await self.session.process_utterance(
                chunk_sink=self._sink
            )
            if transcript:
                self.transcript.append(transcript)

            # Only when nothing streamed: the returned list is the same audio
            # the sink already sent, so sending it again would double every
            # clause. It is sent when the sink was never reached — the fixed
            # response and the batch path, which produce audio without
            # streaming it.
            if chunks and not self._streamed:
                await self.send_frames(chunks)

            if end_call:
                await self.close(reason="sign-off")
        except asyncio.CancelledError:
            # A barge-in. The caller is mid-sentence; the audio already sent is
            # what they heard, and the partial transcript is kept.
            outcome = "interrupted"
            logger.info("media stream: turn cancelled by caller interruption")
            raise
        except Exception:
            outcome = "failed"
            logger.exception("media stream: turn failed")
        finally:
            self._speaking = False
            # US-025: emit the turn's trace however it ended. `getattr` because
            # tests drive this relay with a fake session that has no engine.
            finish = getattr(self.session, "finish_trace", None)
            if finish is not None:
                finish(outcome=outcome)

    async def _sink(self, frames: list[bytes]) -> None:
        """The engine's streaming sink — clause audio, as it is produced."""
        self._streamed = True
        await self.send_frames(frames)

    # -- audio out ---------------------------------------------------------
    async def send_frames(self, frames: list[bytes]) -> None:
        """Base64 each frame to Twilio, with a `mark` for flow control."""
        for frame in frames:
            if self._closed:
                return
            await self._send({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": base64.b64encode(frame).decode("ascii")},
            })
            self._frames_since_mark += 1
            if self._frames_since_mark >= MARK_EVERY_N_FRAMES:
                self._frames_since_mark = 0
                self._marks_outstanding += 1
                await self._send({
                    "event": "mark",
                    "streamSid": self.stream_sid,
                    "mark": {"name": f"chunk-{self._marks_outstanding}"},
                })

    async def clear_playout(self) -> None:
        """Tell Twilio to drop audio it has buffered but not yet played.

        Sent when the caller interrupts: without it the answer they just
        interrupted keeps playing from Twilio's buffer, which sounds exactly
        like the barge-in did not work.
        """
        if self.stream_sid and not self._closed:
            await self._send({"event": "clear", "streamSid": self.stream_sid})

    # -- teardown ----------------------------------------------------------
    async def close(self, *, reason: str) -> None:
        """Release the call. Idempotent, and safe after an abnormal drop.

        `AC-8`: a socket that closes without a Twilio `stop` must still tear
        down cleanly, keep the partial transcript, and release the slot — a
        dropped stream that leaked capacity would erode the ceiling silently.
        """
        if self._closed:
            return
        self._closed = True

        if self.refused:
            # Nothing was started and no slot was taken, so there is nothing to
            # release, no session row to close and no conversation to finish —
            # and releasing here would be the very forgery this guards against.
            logger.info("media stream: closed (refused: %s)", self.refused)
            return

        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
            try:
                await self._turn
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        if self.admission is not None and self.call_id:
            self.admission.release(self.call_id)

        # US-006 TAC-5: the call's conversation gets its summary at teardown.
        # Not closed — a buyer who calls and then messages should continue one
        # thread; the idle sweep owns closing.
        from .voice_pipeline import finish_call

        finish_call(self.call_id, reason=f"voice_{reason}")

        # Closing is idempotent on both sides: a call can end by Twilio `stop`,
        # by socket drop, or by both, and the row must reflect the first.
        if self.store is not None and self.stream_sid:
            try:
                self.store.close_session(
                    channel_kind="voice_inbound", session_key=self.stream_sid
                )
            except Exception:  # noqa: BLE001 - teardown must not raise
                logger.exception("media stream: could not close the session row")

        logger.info(
            "media stream: closed (%s, %d turn(s) transcribed)",
            reason, len(self.transcript),
        )


def _mask(call_id: str) -> str:
    """Never log a full call identifier (`TAC-8`, `REC-14`)."""
    if len(call_id) <= 4:
        return "****"
    return f"…{call_id[-4:]}"
