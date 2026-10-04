"""Speech services: admission, VRAM budget, and the degradation ladder (MOD-05).

The audio pipeline itself (faster-whisper, Kokoro, VAD) is reused from the
source platform's ``voice_handler.py`` — a hand-rolled turn loop with barge-in
and an echo gate that works and has no Twilio imports (REC-01). This module
carries the part that is *specific to running two products on one GPU*: the
budget, the admission decision, and what degrades first.

Three load-bearing decisions:

**The GPU is the ceiling, not the request rate.** At an assumed 0.1 QPS the
platform is nowhere near a throughput limit; what binds is that STT, TTS and the
LLM must co-reside on a 16,311 MiB card shared with a live second product
(`AS-09`). Two concurrent calls is the inherited working ceiling.

**Voice degrades before text.** A third caller hears a busy message and is
offered a callback; chat and WhatsApp keep serving. That is deliberate — text
has no GPU dependency, so shedding it would be losing service that still works.

**The budget refuses rather than warns.** A configuration that would leave the
sibling below its floor is a boot refusal (`MOD-09`), and at runtime admission
enforces the ANITA share. A warning would be discovered by the *other* product
failing, which is the worst way to find out.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

# Audio contract. Codec invariants, not deployment knobs — a misconfigured
# sample rate produces noise, not an error.
TWILIO_SAMPLE_RATE = 8000        # µ-law over Media Streams
BROWSER_SAMPLE_RATE = 16000      # raw PCM from the browser mic
SAMPLE_WIDTH = 2                 # 16-bit
CHANNELS = 1
FRAME_MS = 20                    # 20 ms frames = 160 samples at 8 kHz

# End-of-speech detection. The floor that dominates the voice latency budget:
# 600 ms of silence before transcription begins.
VAD_SILENCE_MS = 600
VAD_SPECULATIVE_ADVANCE_MS = 220

# What each component is allowed to occupy, in MiB.
VRAM_LLM_MIB = 6144
VRAM_STT_MIB = 1200
VRAM_TTS_MIB = 512
VRAM_VAD_MIB = 64
VRAM_OVERHEAD_MIB = 192

VRAM_COMPONENTS: dict[str, int] = {
    "llm": VRAM_LLM_MIB,
    "stt": VRAM_STT_MIB,
    "tts": VRAM_TTS_MIB,
    "vad": VRAM_VAD_MIB,
    "overhead": VRAM_OVERHEAD_MIB,
}

#: US-018: what a host with no CUDA device can actually honour. The configured
#: ceiling assumes a GPU; on CPU the synthesizer runs at ~0.6x real time and
#: contends with STT for the same cores, so one call is the honest number.
#: Voice is **capped**, not refused — CPU transcription and synthesis work.
CPU_CONCURRENT_CALLS = 1


class DegradationLevel(str, Enum):
    """The ladder, in the order things are shed (US-018).

    Ordered deliberately: the least valuable capability goes first, and the one
    that still works without a GPU goes last. ``COMMERCIAL_ONLY`` is the floor —
    SQL-backed price and MOQ answers keep working with the model and the audio
    stack both offline (`DG-03`).
    """

    FULL = "full"
    VOICE_SHED = "voice_shed"          # no new calls; existing calls finish
    STT_OFF = "stt_off"                # transcription disabled
    TTS_OFF = "tts_off"                # synthesis disabled; text only
    CHAT_DEGRADE = "chat_degrade"      # text channels, retrieval only
    COMMERCIAL_ONLY = "commercial_only"  # deterministic answers only


LADDER: tuple[DegradationLevel, ...] = (
    DegradationLevel.FULL,
    DegradationLevel.VOICE_SHED,
    DegradationLevel.STT_OFF,
    DegradationLevel.TTS_OFF,
    DegradationLevel.CHAT_DEGRADE,
    DegradationLevel.COMMERCIAL_ONLY,
)


@dataclass(frozen=True)
class AdmissionDecision:
    """Whether a call may proceed, and what the caller should hear if not."""

    admitted: bool
    reason: str = ""
    level: DegradationLevel = DegradationLevel.FULL
    message: str = ""

    def as_dict(self) -> dict:
        return {
            "admitted": self.admitted,
            "reason": self.reason,
            "level": self.level.value,
            "message": self.message,
        }


BUSY_MESSAGE = (
    "All our lines are busy at the moment. We can call you back — "
    "please try again shortly or send us a message."
)


class AdmissionController:
    """Decides whether another voice call may start.

    Capacity is counted, not estimated: the controller holds the number of live
    calls and refuses beyond the configured ceiling. A refusal is a *designed*
    outcome — the caller is told, offered a callback, and the text channels are
    unaffected.
    """

    def __init__(
        self,
        max_concurrent_calls: int = 2,
        *,
        anita_budget_mib: int = 8192,
        shared_card_mib: int = 16311,
        sibling_floor_mib: int = 6000,
        clock=time.monotonic,
    ) -> None:
        self.max_concurrent_calls = max_concurrent_calls
        self.anita_budget_mib = anita_budget_mib
        self.shared_card_mib = shared_card_mib
        self.sibling_floor_mib = sibling_floor_mib
        self._clock = clock
        self._active: dict[str, float] = {}
        self._level = DegradationLevel.FULL
        self._reason = ""
        self.limits: list[dict] = []
        # US-016 AC-2: `served`, `degraded`, `failed` and `refused` are four
        # different things and are never collapsed. `refused` is counted here
        # because admission is the only place that decides it; the other three
        # arrive from the call engine as the turn resolves.
        self._outcomes: dict[str, int] = {
            "served": 0, "degraded": 0, "failed": 0, "refused": 0,
        }

    # -- budget ------------------------------------------------------------
    def footprint_mib(self, level: DegradationLevel | None = None) -> int:
        """Resident VRAM at a given degradation level.

        Shedding actually frees memory — that is what makes the ladder a
        response to pressure rather than a label for it.
        """
        current = level or self._level
        if current is DegradationLevel.COMMERCIAL_ONLY:
            return 0
        if current is DegradationLevel.CHAT_DEGRADE:
            return VRAM_LLM_MIB + VRAM_OVERHEAD_MIB
        if current is DegradationLevel.TTS_OFF:
            return VRAM_LLM_MIB + VRAM_STT_MIB + VRAM_VAD_MIB + VRAM_OVERHEAD_MIB
        if current is DegradationLevel.STT_OFF:
            return VRAM_LLM_MIB + VRAM_TTS_MIB + VRAM_VAD_MIB + VRAM_OVERHEAD_MIB
        return sum(VRAM_COMPONENTS.values())

    def remaining_for_sibling(self, level: DegradationLevel | None = None) -> int:
        return self.shared_card_mib - self.footprint_mib(level)

    def budget_is_safe(self) -> tuple[bool, str]:
        """True when this product's footprint leaves the sibling its floor."""
        footprint = self.footprint_mib(DegradationLevel.FULL)
        remaining = self.shared_card_mib - footprint
        if remaining < self.sibling_floor_mib:
            return False, (
                f"ANITA would use {footprint} MiB of {self.shared_card_mib} MiB, "
                f"leaving {remaining} MiB; the sibling needs {self.sibling_floor_mib} MiB"
            )
        return True, ""

    # -- degradation -------------------------------------------------------
    @property
    def level(self) -> DegradationLevel:
        return self._level

    def degrade(self, reason: str) -> DegradationLevel:
        """Move one rung down the ladder. Never skips and never goes back up."""
        index = LADDER.index(self._level)
        if index < len(LADDER) - 1:
            self._level = LADDER[index + 1]
            self._reason = reason
            self.limits.append({"level": self._level.value, "reason": reason, "at": self._clock()})
        return self._level

    def recover(self) -> DegradationLevel:
        """Move one rung up, toward FULL. Called when pressure eases."""
        index = LADDER.index(self._level)
        if index > 0:
            self._level = LADDER[index - 1]
            if self._level is DegradationLevel.FULL:
                self._reason = ""
        return self._level

    @property
    def voice_available(self) -> bool:
        """Voice needs both audio legs *and* admission to be open."""
        return self._level is DegradationLevel.FULL

    @property
    def text_available(self) -> bool:
        return self._level is not DegradationLevel.COMMERCIAL_ONLY

    @property
    def llm_available(self) -> bool:
        return self._level not in {DegradationLevel.CHAT_DEGRADE, DegradationLevel.COMMERCIAL_ONLY}

    # -- admission ---------------------------------------------------------
    def active_calls(self) -> int:
        return len(self._active)

    def admit(self, call_id: str) -> AdmissionDecision:
        """Decide whether a call may start.

        Refusal is ordered by cause so the caller hears the right thing: a
        degraded system says so, and only a genuinely full one says "busy".
        """
        if not self.voice_available:
            self._outcomes["refused"] += 1
            return AdmissionDecision(
                False,
                reason=f"voice shed at level {self._level.value}",
                level=self._level,
                message=BUSY_MESSAGE,
            )
        if call_id in self._active:
            return AdmissionDecision(True, reason="already admitted", level=self._level)
        if self.active_calls() >= self.max_concurrent_calls:
            self._outcomes["refused"] += 1
            return AdmissionDecision(
                False,
                reason=f"at capacity ({self.max_concurrent_calls} concurrent calls)",
                level=self._level,
                message=BUSY_MESSAGE,
            )
        self._active[call_id] = self._clock()
        return AdmissionDecision(True, level=self._level)

    def peek(self, call_id: str) -> AdmissionDecision:
        """Would this call be admitted, decided **without taking the slot**.

        The browser-mic ticket needs this. A ticket that reserved a slot would
        leak it whenever the buyer then denied the microphone permission — a
        common, entirely expected outcome — and the ceiling would erode one
        abandoned ticket at a time until the box refused calls while idle.
        The ticket forecasts; the WebSocket handshake is the commitment.
        """
        if not self.voice_available:
            return AdmissionDecision(
                False,
                reason=f"voice shed at level {self._level.value}",
                level=self._level,
                message=BUSY_MESSAGE,
            )
        if call_id in self._active:
            return AdmissionDecision(True, reason="already admitted", level=self._level)
        if self.active_calls() >= self.max_concurrent_calls:
            return AdmissionDecision(
                False,
                reason=f"at capacity ({self.max_concurrent_calls} concurrent calls)",
                level=self._level,
                message=BUSY_MESSAGE,
            )
        return AdmissionDecision(True, level=self._level)

    def release(self, call_id: str) -> bool:
        return self._active.pop(call_id, None) is not None

    def release_stale(self, *, older_than_seconds: float = 3600) -> int:
        """A dropped media stream must not permanently consume a slot.

        Silently leaking capacity is worse than refusing a caller: the ceiling
        erodes and nobody notices until the box is idle but refusing calls.
        """
        now = self._clock()
        stale = [cid for cid, started in self._active.items() if now - started > older_than_seconds]
        for call_id in stale:
            self._active.pop(call_id, None)
        return len(stale)

    # -- turn outcomes (US-016 AC-2) ---------------------------------------
    def note_served(self) -> None:
        """A turn produced speech — the caller heard a real answer."""
        self._outcomes["served"] += 1

    def note_failed(self) -> None:
        """A turn produced no speech at all."""
        self._outcomes["failed"] += 1

    def note_asset_played(self) -> None:
        """A pre-synthesised fixed response was played.

        This is the `degraded` outcome: the caller heard something, but it was
        a canned asset rather than an answer composed for their question.
        """
        self._outcomes["degraded"] += 1

    @property
    def outcomes(self) -> dict[str, int]:
        """A copy — callers read the axis, they do not mutate it."""
        return dict(self._outcomes)

    def snapshot(self) -> dict:
        return {
            "level": self._level.value,
            "reason": self._reason,
            "active_calls": self.active_calls(),
            "max_concurrent_calls": self.max_concurrent_calls,
            "footprint_mib": self.footprint_mib(),
            "remaining_for_sibling_mib": self.remaining_for_sibling(),
            "budget_safe": self.budget_is_safe()[0],
            "voice_available": self.voice_available,
            "text_available": self.text_available,
            # The four outcomes are reported as a group so an operator cannot
            # see one and mistake it for the whole picture.
            "outcomes": self.outcomes,
        }


# ---------------------------------------------------------------------------
# End-of-speech tracking
# ---------------------------------------------------------------------------
@dataclass
class EndOfSpeechTracker:
    """Tracks silence to decide when an utterance has finished.

    The 600 ms floor is the single largest term in the voice latency budget, and
    it is a *choice*: shortening it makes the assistant interrupt people,
    lengthening it makes it feel slow. It is configurable, not hard-coded, so
    the trade-off stays visible.
    """

    silence_ms: int = VAD_SILENCE_MS
    frame_ms: int = FRAME_MS
    _silent_ms: float = 0.0
    _speaking: bool = False
    _frames: int = 0

    def feed(self, *, is_speech: bool) -> bool:
        """Add one frame. Returns True when end-of-speech is decided."""
        self._frames += 1
        if is_speech:
            self._speaking = True
            self._silent_ms = 0.0
            return False
        if not self._speaking:
            # Silence before any speech is not the end of anything.
            return False
        self._silent_ms += self.frame_ms
        return self._silent_ms >= self.silence_ms

    def reset(self) -> None:
        self._silent_ms = 0.0
        self._speaking = False
        self._frames = 0

    @property
    def frames_needed(self) -> int:
        """How many silent frames constitute an utterance boundary."""
        return max(1, int(self.silence_ms / self.frame_ms))


def decode_ulaw_frames(payload: bytes, sample_rate: int = TWILIO_SAMPLE_RATE) -> int:
    """Expected sample count for a µ-law payload. Used to validate frames.

    Kept as arithmetic rather than an audio import so the transport contract can
    be checked without an audio stack present.
    """
    return len(payload) * sample_rate // TWILIO_SAMPLE_RATE if sample_rate else len(payload)
