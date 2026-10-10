"""The browser-mic voice channel (US-003, TRD-05/TRD-06).

The browser records; this module turns what the browser sends into the same
audio the phone path produces, and the same answer engine back into audio the
browser can play. Three decisions carry the story:

**A signed session token, not a UUID.** The old ticket was a random UUID that
nothing could verify — anyone who could reach the socket was admitted to it
(`TAC-2`). The token is HMAC-signed server-side with a TTL, so an unsigned or
expired handshake is refused with `4001` **before** a single audio frame is
read. The token names a session, not a buyer: `CV-02` still holds, and an
anonymous session never becomes a lead.

**PCM16 16 kHz in, µ-law 8 kHz to the engine.** The browser speaks what
`getUserMedia` gives it (`TAC-2`: mono 16 kHz PCM16); the voice engine speaks
Twilio's wire format (8 kHz µ-law) and is not going to learn a second one. The
conversion — box-filter decimation, then µ-law encode — happens here, once, so
both transports feed `VoiceCallSession` byte-identical frames.

**The close code is part of the contract.** `4001` token, `4002` admission,
`4008` rate limit, `1000` normal. A client that cannot tell "busy" from
"broken" shows a spinner for both; these codes are why US-008 can show the
busy state with a callback number instead.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger("anita.browser_voice")

# The frozen close-code contract (US-008's LLD reads these).
CLOSE_NORMAL = 1000
CLOSE_TOKEN = 4001
CLOSE_ADMISSION = 4002
CLOSE_RATE_LIMITED = 4008

# One server frame vocabulary, shared by the client (`US-008`):
#   ready · transcript(role, text, final) · audio(ulaw_b64) · turn_complete
#   busy(callback_number) · error(code) · flush
PCM_SAMPLE_RATE = 16000
FRAME_SAMPLES = 160          # 20 ms at 8 kHz — the engine's frame size
IDLE_TIMEOUT_S = 30.0

# The browser's own honesty line, said when the rate limit closes a socket.
BUSY_TEXT = "All our lines are busy right now. Please try again shortly, or call us."


class TokenError(ValueError):
    """The session token did not verify."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SessionToken:
    session_id: str
    expires_at: float
    nonce: str


class SessionTokens:
    """Mints and verifies the browser session token (TAC-2).

    HMAC-SHA256 over a compact JSON payload. Not JWT: a JWT would add a header
    and an alg-confusion class of bug for a token this server mints and this
    server verifies — the reader is the same process that wrote it.
    """

    def __init__(self, secret: str, *, ttl_seconds: int = 3600,
                 clock: Callable[[], float] | None = None) -> None:
        self.secret = secret
        self.ttl_seconds = int(ttl_seconds)
        self._clock = clock or time.time

    def mint(self, session_id: str) -> str:
        if not self.secret:
            raise TokenError("no session secret configured")
        payload = {
            "sid": str(session_id),
            "exp": self._clock() + self.ttl_seconds,
            "nonce": secrets.token_urlsafe(8),
        }
        body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        return f"{body}.{self._sign(body)}"

    def verify(self, token: str) -> SessionToken:
        if not self.secret:
            raise TokenError("invalid")
        body, _, signature = (token or "").partition(".")
        if not body or not signature:
            raise TokenError("invalid")
        if not hmac.compare_digest(self._sign(body), signature):
            raise TokenError("invalid")
        try:
            payload = json.loads(_unb64(body))
        except Exception as exc:  # noqa: BLE001 - a malformed payload is an invalid token
            raise TokenError("invalid") from exc
        if not isinstance(payload, dict):
            raise TokenError("invalid")
        if float(payload.get("exp") or 0) <= self._clock():
            raise TokenError("expired")
        session_id = str(payload.get("sid") or "")
        if not session_id:
            raise TokenError("invalid")
        return SessionToken(
            session_id=session_id,
            expires_at=float(payload["exp"]),
            nonce=str(payload.get("nonce") or ""),
        )

    def _sign(self, body: str) -> str:
        digest = hmac.new(
            self.secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
        ).digest()
        return _b64(digest)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


# ---------------------------------------------------------------------------
# Audio format
# ---------------------------------------------------------------------------
def pcm16_to_ulaw(pcm: bytes, *, src_rate: int = PCM_SAMPLE_RATE,
                  dst_rate: int = 8000) -> bytes:
    """Browser PCM16 to the engine's µ-law.

    The decimation averages each group of samples rather than dropping every
    other one. Dropping is free and aliases: speech energy above 4 kHz folds
    back into the band the recogniser listens to, and the symptom is a
    transcript that is *nearly* right — the worst kind, because it still looks
    like STT being imperfect.
    """
    import numpy as np

    from .voice import pcm_to_ulaw

    if not pcm:
        return b""
    samples = np.frombuffer(pcm, dtype="<i2")
    factor = max(1, int(round(src_rate / dst_rate)))
    if factor > 1:
        usable = len(samples) - (len(samples) % factor)
        if usable == 0:
            return b""
        samples = (
            samples[:usable].reshape(-1, factor).astype(np.int32).mean(axis=1)
            .round().astype("<i2")
        )
    return pcm_to_ulaw(samples.tobytes())


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------
class BrowserVoiceSession:
    """One browser-mic session's protocol. No socket of its own.

    Same shape as the Twilio relay — the transport is an injected async
    `send` — for the same reason: every decision this class makes (end of
    speech, barge-in, teardown) is testable without a browser, and the tests
    assert the exact frames a browser would receive.
    """

    def __init__(
        self,
        *,
        session,
        send,
        admission=None,
        store=None,
        call_id: str = "",
        session_key: str = "",
        idle_timeout_s: float = IDLE_TIMEOUT_S,
        turn_limiter=None,
    ) -> None:
        self.session = session
        self._send = send
        self.admission = admission
        self.store = store

        self.call_id = call_id or session_key or "browser"
        self.session_key = session_key or self.call_id
        self.idle_timeout_s = float(idle_timeout_s)
        # 20 turns per session per minute (`TAC-4`). Over the allowance the
        # socket is closed with 4008 — not dropped mid-audio, and not
        # silently throttled, so the client can say what happened.
        self.turn_limiter = turn_limiter

        self._started = False
        self._closed = False
        self._turn: asyncio.Task | None = None
        self._speaking = False
        self._streamed = False
        # §8.30: whether the engine has been put into its SPEAKING window by
        # this turn — see `send_frames`. Set when the first batch reaches the
        # buyer's playback queue, cleared when the turn ends.
        self._playback_open = False
        self._pcm_buffer = b""
        self.transcript: list[str] = []

        # What the socket handler reads to decide why and how to close.
        self.close_requested = False
        self.close_reason = ""
        self.close_code = CLOSE_NORMAL
        self.last_activity = time.monotonic()

    # -- protocol ----------------------------------------------------------
    async def handle_message(self, raw: str) -> None:
        """Dispatch one frame from the browser. Never raises on malformed input."""
        self.last_activity = time.monotonic()
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("browser voice: unparseable frame dropped")
            return
        if not isinstance(message, dict):
            return

        kind = message.get("type")
        if kind == "start":
            await self._on_start(message)
        elif kind == "audio":
            await self._on_audio(message)
        elif kind == "stop_speaking":
            self.on_interrupt()
        elif kind == "commit":
            # Push-to-talk from the MediaRecorder fallback: the whole
            # utterance arrived at once and there is no trailing silence for
            # the VAD to find, so the client says when it has finished.
            if not self._speaking:
                self._begin_turn()
        elif kind == "stop":
            # Marked, not closed here: the socket's owner closes, so there is
            # exactly one place that tears a session down.
            self.close_requested = True
            self.close_reason = "client_stop"
        else:
            logger.debug("browser voice: ignoring frame %r", kind)

    async def _on_start(self, message: dict) -> None:
        if self._started:
            return  # duplicate start frames are ignored (edge case 4)
        self._started = True
        # US-004: the browser session id is the *same* id the web chat uses, so
        # a voice turn lands on the conversation the buyer was already typing
        # into (US-003 AC-1). `channel_kind` tells the answer path this is the
        # browser, not a PSTN call; a browser session supplies no phone
        # identity, so `CV-02` holds — no lead until details are given.
        if hasattr(self.session, "call_id"):
            self.session.call_id = self.session_key
        if hasattr(self.session, "channel_kind"):
            self.session.channel_kind = "browser_mic"
        if self.store is not None:
            try:
                self.store.open_session(
                    channel_kind="browser_mic", session_key=self.session_key,
                    conversation_id=str(message.get("conversation_id") or "") or None,
                )
            except Exception:  # noqa: BLE001 - bookkeeping must not drop a session
                logger.exception("browser voice: could not open the session row")
        await self._send({"type": "ready", "session_id": self.session_key})

    async def _on_audio(self, message: dict) -> None:
        if self._closed:
            return
        payload = message.get("pcm16") or ""
        try:
            pcm = base64.b64decode(payload) if payload else b""
        except (TypeError, ValueError):
            logger.warning("browser voice: undecodable audio dropped")
            return
        if not pcm:
            return

        # Frame-size agnostic: worklet, ScriptProcessor and the MediaRecorder
        # fallback all arrive at different chunk sizes, and the engine's VAD
        # counts frames. Re-buffering here to the engine's 20 ms grid is what
        # lets all three share one end-of-speech decision.
        self._pcm_buffer += pcm
        frame_bytes = FRAME_SAMPLES * 2 * (PCM_SAMPLE_RATE // 8000)
        while len(self._pcm_buffer) >= frame_bytes:
            chunk, self._pcm_buffer = (
                self._pcm_buffer[:frame_bytes], self._pcm_buffer[frame_bytes:]
            )
            await self._feed(pcm16_to_ulaw(chunk))

    async def _feed(self, frame: bytes) -> None:
        if not frame:
            return
        if self._speaking:
            from .voice import FRAME_ECHO, FRAME_INTERRUPTION, FRAME_MUTED

            verdict = self.session.admit_inbound_frame(frame)
            if verdict in (FRAME_ECHO, FRAME_MUTED):
                return
            if verdict == FRAME_INTERRUPTION:
                # The browser's playback queue is the analogue of Twilio's
                # playout buffer: without the flush the interrupted answer
                # keeps playing from audio already handed to the client.
                await self._send({"type": "flush"})
                note = getattr(self.session, "note_answer_truncated", None)
                if note is not None:
                    note()
        elif self._turn is not None and not self._turn.done():
            return

        if self.session.feed_audio(frame):
            self._begin_turn()

    def on_interrupt(self) -> None:
        """The buyer pressed the interrupt control (US-008 AC-2).

        Stops the engine and cancels the turn; the client flushes its own
        queue the moment it sends this. No frame goes back — the flush already
        happened where the audio is.
        """
        if not self._speaking and (self._turn is None or self._turn.done()):
            return  # interrupt while silent is a no-op (edge case 8)
        self.session.request_interruption()

    # -- the turn ----------------------------------------------------------
    def _begin_turn(self) -> None:
        if self._turn is not None and not self._turn.done():
            return
        if self.turn_limiter is not None:
            from .channels import RateLimited

            try:
                self.turn_limiter.check(self.call_id)
            except RateLimited:
                self._turn = asyncio.create_task(self._close_rate_limited())
                return
        self._turn = asyncio.create_task(self._run_turn())

    async def _close_rate_limited(self) -> None:
        """Over the turn allowance: say so, then let the socket close 4008."""
        await self._send({"type": "error", "code": "rate_limited", "text": BUSY_TEXT})
        self.close_code = CLOSE_RATE_LIMITED
        self.close_reason = "rate_limit"
        self.close_requested = True

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
                # Both halves go to the page: the buyer sees their own words
                # transcribed (which is how they learn the mic misheard them)
                # and the answer as text even when the audio never plays
                # (`T-14`).
                for role, text in _dialogue_lines(transcript):
                    await self._send({
                        "type": "transcript", "role": role, "text": text, "final": True,
                    })

            if chunks and not self._streamed:
                await self.send_frames(chunks)

            await self._send({
                "type": "turn_complete",
                "turn_id": int(getattr(self.session, "_turn", 0) or 0),
            })
            if end_call:
                await self.close(reason="sign-off")
        except asyncio.CancelledError:
            outcome = "interrupted"
            raise
        except Exception:
            outcome = "failed"
            logger.exception("browser voice: turn failed")
            await self._send({"type": "error", "code": "turn_failed"})
        finally:
            self._speaking = False
            # §8.30: leave the engine's SPEAKING window on every exit path, for
            # the same reason the PSTN relay does — an unclosed window gates the
            # buyer's ordinary speech as echo for the rest of the session.
            if self._playback_open:
                self._playback_open = False
                end = getattr(self.session, "end_turn", None)
                if end is not None:
                    end()
            finish = getattr(self.session, "finish_trace", None)
            if finish is not None:
                finish(outcome=outcome)

    async def _sink(self, frames: list[bytes]) -> None:
        self._streamed = True
        await self.send_frames(frames)

    async def send_frames(self, frames: list[bytes]) -> None:
        """Hand µ-law frames to the page's playback queue.

        §8.30: the browser's queue is the analogue of Twilio's playout buffer,
        so this is where the engine is told it is speaking. The first batch
        arms the barge-in gate and primes the echo reference; later batches are
        only queued on the playout clock. `admit_inbound_frame` also feeds the
        `flush` on `FRAME_INTERRUPTION`, so without this the page keeps playing
        the interrupted answer out of its own queue.
        """
        if not frames:
            return
        if self._playback_open:
            note = getattr(self.session, "note_playback", None)
            if note is not None:
                note(frames)
        else:
            self._playback_open = True
            mark = getattr(self.session, "mark_speaking", None)
            if mark is not None:
                mark(frames)

        for frame in frames:
            if self._closed:
                return
            await self._send({
                "type": "audio",
                "ulaw_b64": base64.b64encode(frame).decode("ascii"),
            })

    # -- teardown ----------------------------------------------------------
    async def close(self, *, reason: str) -> None:
        """Release the session. Idempotent, safe after an abnormal drop."""
        if self._closed:
            return
        self._closed = True
        self.close_requested = True
        if not self.close_reason:
            self.close_reason = reason
        if reason == "rate_limit":
            self.close_code = CLOSE_RATE_LIMITED

        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
            try:
                await self._turn
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        if self.admission is not None and self.call_id:
            self.admission.release(self.call_id)

        # US-006 TAC-5: summarise the session's conversation at teardown.
        from .voice_pipeline import finish_call

        finish_call(self.session_key, reason=f"browser_{reason}")

        if self.store is not None and self.session_key:
            try:
                self.store.close_session(
                    channel_kind="browser_mic", session_key=self.session_key,
                    reason=reason,
                )
            except Exception:  # noqa: BLE001 - teardown must not raise
                logger.exception("browser voice: could not close the session row")

        logger.info(
            "browser voice: closed (%s, %d turn(s))", reason, len(self.transcript)
        )


def _dialogue_lines(dialogue: str) -> list[tuple[str, str]]:
    """Split the engine's dialogue block into ``(role, text)`` lines.

    `process_utterance` returns "Caller: …\\nAssistant: …". The page shows the
    answer as text (`T-14`) and the buyer's own words too, because a transcript
    the buyer cannot see is a transcription error the buyer cannot notice.
    """
    lines: list[tuple[str, str]] = []
    for raw in (dialogue or "").splitlines():
        text = raw.strip()
        if not text:
            continue
        lowered = text.lower()
        if lowered.startswith("caller:"):
            lines.append(("caller", text.split(":", 1)[1].strip()))
        elif lowered.startswith("assistant:"):
            lines.append(("assistant", text.split(":", 1)[1].strip()))
        elif lines:
            # A continuation line belongs to the role that opened it — answers
            # contain newlines.
            role, previous = lines[-1]
            lines[-1] = (role, f"{previous}\n{text}")
        else:
            lines.append(("assistant", text))
    return lines


__all__ = [
    "BUSY_TEXT", "BrowserVoiceSession", "CLOSE_ADMISSION", "CLOSE_NORMAL",
    "CLOSE_RATE_LIMITED", "CLOSE_TOKEN", "FRAME_SAMPLES", "IDLE_TIMEOUT_S",
    "PCM_SAMPLE_RATE", "SessionToken", "SessionTokens", "TokenError",
    "pcm16_to_ulaw",
]
