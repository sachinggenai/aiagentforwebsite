"""
Real-time voice call handler — runs STT → RAG → LLM → TTS inside a
Twilio Media Streams WebSocket loop.

Reuses the platform's adapters, one per stage:
  - faster-whisper (model from `SpeechSettings.whisper_model`) for speech-to-text
  - anita.voice_pipeline / anita.voice_rag for retrieval and the answer
  - kokoro_onnx.Kokoro for text-to-speech

Flow (inside the WebSocket media loop):
  1. Accumulate µ-law audio chunks until the caller stops speaking.
  2. Decode → PCM, run faster-whisper STT.
  3. Hand the transcript to the installed answer engine.
  4. Synthesise answer with Kokoro TTS → PCM.
  5. Encode PCM → µ-law, send back through WebSocket.
  6. Listen for the next utterance.

Usage (from `anita.media_stream`, one session per call):
    from anita.voice import VoiceCallSession

    session = VoiceCallSession()
    if session.feed_audio(ulaw_frame):
        chunks, dialogue, end_call = await session.process_utterance()
"""

from __future__ import annotations

import asyncio
import base64
import io as _io
import logging
import re
import os
import time as _time
from pathlib import Path

import numpy as np
from typing import AsyncIterator, Callable

# US-011 TAC-1: settings resolve once, through one loader that also reads .env.
# Reading os.environ here meant the value in force depended on whether some
# other importer had already loaded .env — this module never did.
from . import audio_format
from .voice_settings import settings

logger = logging.getLogger("anita.voice")

# Structured call-event timeline (Phase 0 observability): every line is
#   EVENT <call_id> turn=<n> <NAME> [key=value ...]
# so a call can be reconstructed end-to-end from the log.
voice_events = logging.getLogger("anita.voice.events")

# ── Lazy-loaded models (shared across all sessions) ───────────────────

_stt_model = None
_tts_engine = None
_STT_MODEL_SIZE = settings.WHISPER_MODEL  # small.en or medium.en


def _get_stt_model():
    """Load faster-whisper on CUDA once and cache (numpy bypass avoids PyAV DLL)."""
    global _stt_model
    if _stt_model is not None:
        return _stt_model

    from faster_whisper import WhisperModel

    # Detect compute device — CTranslate2 has no Metal backend, so Apple
    # Silicon (mps) maps to CPU with NEON-accelerated int8.
    try:
        from .voice_platform import get_whisper_device_config
        device, compute_type = get_whisper_device_config()
    except Exception:
        device = "cuda" if settings.CUDA_VISIBLE_DEVICES else "cpu"
        compute_type = "int8" if device == "cuda" else "float32"

    logger.info(f"Loading faster-whisper {_STT_MODEL_SIZE} on {device} ({compute_type})...")
    # WHISPER_NUM_THREADS is sized per machine by scripts/predeploy.py.
    # Note: pipecat's WhisperSTTSettings has no cpu_threads param, so this
    # applies to the ANITA voice STT path only.
    _stt_model = WhisperModel(
        _STT_MODEL_SIZE,
        device=device,
        compute_type=compute_type,
        cpu_threads=settings.WHISPER_NUM_THREADS,
    )
    logger.info(f"faster-whisper {_STT_MODEL_SIZE} ready on {device}")
    return _stt_model


def _get_tts_engine():
    """Load Kokoro ONNX once and cache.

    Requests the CUDA execution provider, then reports which provider the
    session ACTUALLY got.

    The original version logged "CUDA GPU enabled" whenever
    `ort.get_available_providers()` listed CUDA — that lists providers compiled
    into the wheel, not providers that instantiate. On this box the CUDA
    provider fails ("Require cuDNN 9.* and CUDA 13.*"; torch ships CUDA 12.8)
    and ONNX Runtime silently falls back to CPU, so the log claimed a 5-10x
    speedup that was never happening while the CPU did all the work.

    Measured consequence: RTF ~0.6 on CPU, which is the ~6 s single-session
    synthesis and the 12-16 s N=2 bottleneck. The claim is now read from the
    session itself, so it cannot be true-by-accident again.
    """
    global _tts_engine
    if _tts_engine is not None:
        return _tts_engine

    # Ask for CUDA. Whether we GET it is checked below, from the session.
    import onnxruntime as ort
    requested = "CUDAExecutionProvider" if "CUDAExecutionProvider" in \
        ort.get_available_providers() else None
    if requested:
        os.environ["ONNX_PROVIDER"] = requested

    # Make CUDA/cuDNN actually resolvable. On Windows the ORT wheel does NOT
    # bundle them, so the provider fails to instantiate and ONNX Runtime
    # silently falls back to CPU. torch ships CUDA 12.8 + cuDNN 9 and registers
    # its own lib dir on import; preload_dlls() reuses those.
    #
    # This is the difference between RTF 0.63 and RTF 0.06 on this box — the
    # ~6 s single-session synthesis and the 12-16 s two-caller bottleneck.
    # Order matters: torch first, then preload_dlls(), then the session.
    try:
        import torch  # noqa: F401  (registers torch's bundled CUDA/cuDNN dirs)
        ort.preload_dlls()
    except Exception as exc:
        logger.warning("Kokoro TTS: CUDA DLL preload skipped (%s)", exc)

    from kokoro_onnx import Kokoro

    cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "pipecat", "kokoro-onnx")
    _tts_engine = Kokoro(
        os.path.join(cache_dir, "kokoro-v1.0.onnx"),
        os.path.join(cache_dir, "voices-v1.0.bin"),
    )

    # The truth, read back from the session that was actually built.
    try:
        active = _tts_engine.sess.get_providers()
        on_gpu = "CUDAExecutionProvider" in active
    except Exception:
        active, on_gpu = ["<unknown>"], False

    if on_gpu:
        logger.info("Kokoro TTS: running on CUDA (%s)", ",".join(active))
    else:
        logger.warning(
            "Kokoro TTS: CPU ONLY — requested %s, session got %s. Synthesis is "
            "~0.6x real-time and contends with everything else on the CPU; this "
            "is the dominant cost of a two-caller turn. See doc/perf/"
            "IMPLEMENTATION_STATUS.md.",
            requested or "(nothing)", ",".join(active),
        )
    return _tts_engine


#: Voice and speaking rate for the LIVE call path.
#:
#: Both keys shipped in .env and were ignored here: the call site hardcoded
#: voice="af_heart", speed=1.0. KOKORO_VOICE was read by app/pipeline.py, so it
#: looked alive in a grep while the Twilio path -- the one that actually serves
#: callers -- never consulted it. Reading them here makes the live path agree
#: with the rest of the stack.
#:
#: Speed is clamped to the range Kokoro renders cleanly; outside it the output
#: degrades into artefacts, which is worse than ignoring the setting. A value
#: that will not parse falls back to 1.0 with a warning rather than raising --
#: a typo in .env must not stop the stack from answering calls.
TTS_VOICE = settings.KOKORO_VOICE


def _parse_tts_speed(raw: str) -> float:
    try:
        speed = float(raw)
    except (TypeError, ValueError):
        logger.warning("KOKORO_SPEED=%r is not a number; using 1.0", raw)
        return 1.0
    clamped = min(max(speed, 0.5), 2.0)
    if clamped != speed:
        logger.warning(
            "KOKORO_SPEED=%.2f is outside the clean-synthesis range 0.5-2.0; "
            "using %.2f", speed, clamped)
    return clamped


TTS_SPEED = _parse_tts_speed(settings.KOKORO_SPEED)

#: US-012 AC-4: the isolation fallback must be a CONFIGURATION change, not a code
#: revert. "shared" is the DG-06 decision (one process-wide cache, reused across
#: callers); "per_call" bounds the cache to a single session's utterances and
#: gives up cross-caller reuse in exchange for isolation that needs no argument.
TTS_CACHE_SCOPE = settings.TTS_CACHE_SCOPE

#: US-005 (streaming TTS, PO build-approved 2026-09-19): the FIRST audio chunk
#: of an answer is sent while the rest is still being synthesised. Behind a
#: flag because adoption is gated on DG-03; the batch path (`_synthesise`) is
#: retained unchanged as the BRD-15 revert (flip the flag, no code change).
TTS_STREAM = settings.TTS_STREAM


def _cache_key_sha(cache_key: tuple) -> str:
    """Short, stable digest of a cache key, safe to write to a trace.

    Deterministic across processes, unlike `hash()` of a str, which is salted
    per interpreter (PYTHONHASHSEED). In-process that salt is harmless; in a log
    it would make two callers' keys for the same utterance look different, and
    the isolation test reads keys across a whole run.

    The digest is truncated to 12 hex characters and is one-way, so no agent
    text lands in the trace (TAC-4).
    """
    import hashlib

    raw = "\x1f".join(str(part) for part in cache_key)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _tts_voice() -> str:
    return TTS_VOICE


def _tts_speed() -> float:
    return TTS_SPEED


# ── u-law ↔ PCM conversion (stdlib audioop) ───────────────────────────
# Every conversion in the voice path funnels through these two, and both take the
# sample width as an argument — so the number comes from `app/audio_format.py`
# instead of a literal at each call site. Twilio's wire format carries 8-bit
# u-law samples; what these produce and consume is the pipeline's 16-bit linear
# PCM.

def ulaw_to_pcm(ulaw_bytes: bytes) -> bytes:
    import audioop
    return audioop.ulaw2lin(ulaw_bytes, audio_format.SAMPLE_WIDTH)


def pcm_to_ulaw(pcm_bytes: bytes) -> bytes:
    import audioop
    return audioop.lin2ulaw(pcm_bytes, audio_format.SAMPLE_WIDTH)


def frame_rms(ulaw_chunk: bytes) -> float:
    """RMS amplitude of one µ-law frame, on the VAD's own scale.

    Extracted so `is_silent` and the US-014 echo gate measure energy the SAME
    way. Two copies of this would be two definitions of "loud", and the defect
    would surface as a gate whose `BARGE_IN_ABS_FLOOR` silently did not mean
    what `is_silent`'s 80 means -- the same reasoning that keeps `_prepare_audio`
    single-copy for the speculative and real STT passes.
    """
    if len(ulaw_chunk) < 2:
        return 0.0
    try:
        samples = np.frombuffer(ulaw_to_pcm(ulaw_chunk), dtype=np.int16)
        return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    except Exception:                                   # noqa: BLE001 — never break a call
        return 0.0


def _reference_rms(playout: list[bytes], cursor: int = 0) -> float:
    """RMS of the audio being played out at `cursor`, holding TWO frames.

    Two rather than one (~40 ms) because the echo of the frame being played and
    the echo of the frame before it are both in flight at once, and the carrier
    jitters by a frame in either direction. Holding one frame would let the
    reference momentarily read as silence between two loud frames, and a
    reference of zero is a gate with no ratio at all.
    """
    window = playout[cursor:cursor + 2]
    if not window:
        return 0.0
    return max(frame_rms(f) for f in window)


# ── Standalone helper for main.py greeting ────────────────────────────


def _turn_outcome(chunks: list[bytes]) -> list[bytes]:
    """Record a turn on the four-outcome axis, then pass the audio through.

    US-016 AC-2: `served`, `degraded`, `failed` and `refused` are four different
    things and are never collapsed. `degraded` is recorded where the fixed
    response is played, `refused` where a call is turned away at admission, and
    this covers the other two -- a turn that produced speech, and a turn that
    produced none.
    """
    from .voice_admission import REGISTRY

    if chunks:
        REGISTRY.note_served()
    else:
        REGISTRY.note_failed()
    return chunks


def load_call_asset_ulaw(name: str) -> list[bytes] | None:
    """Read a pre-synthesised call asset as µ-law chunks. No model, no synthesis.

    US-016 AC-5: the fixed response and the busy message are the two sentences
    a caller hears when the machinery is not available to say anything else.
    Both are read from disk here -- this function touches no engine and no
    synthesiser, which is precisely why it still works when one or both are
    gone. Returns None if the asset is missing, and the caller decides what to
    do rather than being handed silence.
    """
    from .voice_admission import asset_path

    path = asset_path(name)
    # ANITA ships no bundled audio, so `asset_path` returns None by design
    # (voice_admission.asset_path). The guard has to test for None *before*
    # calling is_file(): the source's helper always returned a Path, so the
    # original `not path.is_file()` never had to consider absence.
    if path is None or not path.is_file():
        return None
    try:
        import wave

        with wave.open(str(path), "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                logger.warning("call asset %s is not mono 16-bit PCM", name)
                return None
            rate = w.getframerate()
            pcm = w.readframes(w.getnframes())
        if rate != 8000:
            # Built at carrier rate, so this is a guard rather than a path.
            logger.warning("call asset %s is %d Hz, expected 8000", name, rate)
            return None
    except Exception:
        logger.exception("call asset %s could not be read", name)
        return None

    chunk = 320  # 20 ms at 8 kHz, the Media Streams frame size
    return [pcm_to_ulaw(pcm[i:i + chunk]) for i in range(0, len(pcm), chunk)]


# NOTE (CL-8): the source's `generate_ulaw_greeting()` lived here. It
# pre-synthesised the greeting that the source's `app/main.py` played from a
# WebSocket start handler — ANITA greets through TwiML `<Say>` instead, was
# never called by anything in this repo, and is recorded as a deliberate
# removal in tests/test_mod05_voice_migration.py (DELIBERATE_GONE).


# ── VoiceCallSession — runs the full AI pipeline per utterance ────────

#: Assistant phrasings that ask the caller to pin down a product or an order.
#: This is the signal the deterministic loop-breaker counts — "the assistant
#: has asked for clarification twice in a row" — so it matches ANITA's
#: catalogue vocabulary rather than the admissions vocabulary the engine was
#: copied with (work-status §10, CL-3). The generic lead-ins ("could you
#: specify", "did you mean") are domain-neutral and stay.
_CLARIFICATION_RE = re.compile(
    r"(which (specific )?(product|model|item|variant|version|one|size|pack)|"
    r"what (product|model|item|variant|version|quantity|size)|"
    r"could you (please )?(specify|clarify|tell me)|did you mean|just to confirm|"
    r"are you (looking|asking|interested|referring|planning))",
    re.IGNORECASE,
)


def _is_clarification(text: str) -> bool:
    """True when an assistant turn reads as a clarification question."""
    return "?" in text and bool(_CLARIFICATION_RE.search(text))


# ── STT noise gate + deterministic hangup (pre-LLM guardrails) ────────

STT_MIN_AVG_LOGPROB = settings.STT_MIN_AVG_LOGPROB
STT_MAX_NO_SPEECH_PROB = settings.STT_MAX_NO_SPEECH_PROB
STT_MIN_CHARS = settings.STT_MIN_CHARS
# Utterances shorter than this are treated as noise bursts (15 × 20 ms = 300 ms).
MIN_UTTERANCE_FRAMES = settings.MIN_UTTERANCE_FRAMES

# ── BRD-04: the ONE live end-of-speech decision ─────────────────────────
# "The system shall have exactly one live end-of-speech decision, its delay
#  documented, and its value reconciled explicitly against BRD-02."
#
# This is it. `is_silent()` gates the audio and `feed_audio()` counts trailing
# silence; when the count reaches VAD_SILENCE_FRAMES the turn is closed. There
# is no second decision on this path, and there must never be one -- a second
# VAD with a different delay is two systems disagreeing about when a caller
# stopped talking, which is how a caller gets talked over.
#
# Reconciled against BRD-02 (p50 <= 700 ms for TURN_E2E_MS): this delay is
# measured at 600 ms, i.e. 86% of that budget before STT has run (`AS-04`).
# BRD-02's 700 ms is therefore NOT reachable by tuning this number -- see
# doc/perf/us015-model-decision.md for the measured chain. The value is kept
# because 600 ms is the pause tolerance callers are given, and the fixtures
# encode a 240 ms mid-turn hesitation as a real caller behaviour that must
# survive. Lowering it is not a free win: it splits callers who pause.
VAD_SILENCE_MS = settings.VAD_SILENCE_MS
VAD_SILENCE_FRAMES = max(1, round(VAD_SILENCE_MS / 20))


# ── US-014: the turn state and the ONE inbound-frame decision ───────────
# `doc/sdlc/03-data-state-analysis.md` SM-01 names SPEAKING -> PROCESSING an
# illegal transition, because `MUTE_STT_DURING_TTS` discarded the audio. The
# state existed only as the `tts_playing` local in each WebSocket handler, and
# it was True only while the handler was NOT reading the socket -- so the guard
# that consumed it could never fire. Stating the state on the session, where the
# audio buffer lives, is what makes one decision point possible instead of the
# two copies that existed.
#
# READ THIS BEFORE ADDING A SECOND VAD: the gate below decides when an
# interruption STARTS, never when a turn ENDS. `feed_audio` remains BRD-04's one
# end-of-speech decision. An admitted interruption is appended to the same
# buffer and closed by the same 600 ms rule as any other utterance.
STATE_LISTENING = "LISTENING"
STATE_PROCESSING = "PROCESSING"
STATE_SPEAKING = "SPEAKING"

#: The verdicts of `admit_inbound_frame`. Exactly one of these, per frame.
FRAME_LISTENING = "LISTENING"        # not speaking: an ordinary caller frame
FRAME_INTERRUPTION = "INTERRUPTION"  # admitted while speaking: stop and listen
FRAME_ECHO = "ECHO"                  # rejected: our own playback coming back
FRAME_MUTED = "MUTED"                # BARGE_IN_ENABLED off: the documented discard

BARGE_IN_ENABLED = settings.BARGE_IN_ENABLED
BARGE_IN_ECHO_GAIN = settings.BARGE_IN_ECHO_GAIN
BARGE_IN_MARGIN = settings.BARGE_IN_MARGIN
BARGE_IN_ABS_FLOOR = settings.BARGE_IN_ABS_FLOOR
BARGE_IN_MIN_FRAMES = settings.BARGE_IN_MIN_FRAMES
BARGE_IN_REF_DECAY = settings.BARGE_IN_REF_DECAY
#: The pre-existing AEC workaround, now the gate's OFF-mode behaviour: with
#: BARGE_IN_ENABLED=0 this key decides between discarding the caller's
#: overlapping speech (1, the default) and letting the mic bleed through (0).
#: Read here rather than in the handlers because the decision moved onto the
#: session -- one decision point, TRD-05.
MUTE_STT_DURING_TTS = settings.MUTE_STT_DURING_TTS
#: One frame of carrier audio, and therefore one tick of the playout clock.
FRAME_MS = 20

#: Appended to a history entry when the caller cut in before the answer had
#: finished playing. The prompt's "Interruption Handling" section describes this
#: marking; the two are asserted to agree by `tests/test_us014_barge_in.py`.
TRUNCATION_MARK = ("[the caller began speaking before I finished - they may not "
                   "have heard the end of this]")


def _vad_silence_frames() -> int:
    """Read the live end-of-speech delay at SESSION construction.

    Read per call rather than once at import so a deployment can change the
    delay without a rebuild, and so a test can construct a session with a
    different delay without reloading the module. BRD-04's "configuration that
    does not execute shall not be presented as the live setting" cuts both
    ways: a value nobody can reach is not a setting.
    """
    raw = os.environ.get("VAD_SILENCE_MS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return max(1, round(int(raw) / 20))
    return VAD_SILENCE_FRAMES


#: BRD-04 does not forbid closing a turn EARLY if the work has already been
#: done -- it forbids a second *decision*. Speculative STT keeps the single
#: decision and its single 600 ms delay; it merely transcribes the audio the
#: caller has already produced while the remaining silence is still being
#: counted. The result is used only if the audio did not change, so the
#: transcript is identical to the non-speculative one by construction. Set to
#: 0 to disable. Default: start STT 220 ms before the endpoint fires, which is
#: the measured STT p50 (203 ms) rounded up -- starting earlier would usually
#: finish before the window closed and waste a GPU pass on resumed callers.
VAD_SPECULATIVE_ADVANCE_MS = settings.VAD_SPECULATIVE_ADVANCE_MS


def _speculative_advance_ms() -> int:
    """Read the speculative lead at SESSION construction.

    Same reasoning as `_vad_silence_frames`: a value read once at import is a
    value a deployment cannot change and a test cannot vary, which is the
    defect BRD-04 names. Read per session so both can.
    """
    raw = os.environ.get("VAD_SPECULATIVE_ADVANCE_MS", "").strip()
    if raw.isdigit():
        return int(raw)
    return VAD_SPECULATIVE_ADVANCE_MS

_NOISE_WORDS = {"is", "uh", "um", "oh", "ah", "hmm", "hm", "mhm", "eh", "huh"}

#: Fixed reply for gated/noisy input — deliberately bypasses the LLM.
NOISE_REPLY = "Sorry, I didn't quite catch that — could you repeat that?"

#: Escalation after repeated noise-gated turns — the line itself is bad.
LINE_QUALITY_REPLY = (
    "It sounds like the line is breaking up. "
    "Could you try speaking a little closer to the phone?"
)

#: Step 3 — the human-transfer offer, and the last thing the caller hears.
#:
#: An `ALT_CHANNEL_REPLY` used to sit between these, offering "WhatsApp or
#: email". Two things were wrong with it here: inbound WhatsApp is a declared
#: deferral on this product (UC-02 in `config/anita.yaml`), so the assistant was
#: naming a channel it cannot answer on; and it made the human offer the
#: *fourth* rung where `AC-7` puts it at the third. The offer now names only
#: what exists, and "a human or a callback" is one rung rather than two.
HUMAN_TRANSFER_REPLY = (
    "I'm sorry — the line still isn't clear enough for me to help. "
    "I'll ask someone from our team to call you back. "
    "Could you tell me once more which number we should reach?"
)

#: `AC-7`: three consecutive unintelligible utterances, then a human.
#: The last rung is terminal — after it the call closes.
_NOISE_LADDER = [NOISE_REPLY, LINE_QUALITY_REPLY, HUMAN_TRANSFER_REPLY]

#: `TAC-6` fallback if the settings object cannot supply a ceiling.
_DEFAULT_MAX_CALL_SECONDS = 1800


def _max_call_seconds() -> int:
    """Read the call ceiling at SESSION construction, like the VAD delay.

    Read per call rather than once at import, so a deployment can change the
    ceiling without a rebuild and a test can construct a session with a short
    one. A value nobody can reach is not a setting.
    """
    try:
        from .voice_settings import settings

        return int(getattr(settings, "MAX_CALL_SECONDS", _DEFAULT_MAX_CALL_SECONDS))
    except Exception:  # noqa: BLE001 - a settings failure must not lose the cap
        return _DEFAULT_MAX_CALL_SECONDS


#: Said when the ceiling is reached. Spoken once, then the call ends.
CALL_CAP_REPLY = (
    "I'm sorry, I've not been able to help on this call. "
    "I'll ask someone from our team to call you back — goodbye."
)

# ── D1 output-boundary scrubber ────────────────────────────────────────
# Leaked reasoning/meta narration observed in real calls. Sentence-level
# filter: any sentence containing one of these markers is dropped before
# the reply is spoken or logged. Clean text passes through untouched.
_META_MARKERS = (
    "based on",
    "here's the relevant",
    "catalogue profile",
    "the caller",
    "asked for clarification",
    "let's assume",
    "natural response",
    "this response provides",
    "provide concrete information",
    "without asking",
    "given that",
    "they are interested",
)


#: Meta lead-in glued directly to real content ("Here's the relevant
#: information from the catalogue profile: Ultrasound Gel is listed at
#: ₹60/Piece.") — strip the prefix so the real content survives.
_PREFIX_STRIP = re.compile(
    r"^here'?s the relevant information\b[^:\n]*[:：]\s*", re.IGNORECASE
)


def scrub_meta_leak(text: str) -> str | None:
    """
    Hard output boundary (D1): remove reasoning/meta narration from an
    LLM answer before it reaches TTS. Returns None when nothing usable
    remains after scrubbing. Clean text passes through untouched.
    """
    low = text.lower()
    if not any(m in low for m in _META_MARKERS):
        return text  # clean reply — byte-identical pass-through

    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    kept = []
    for s in sentences:
        s2 = _PREFIX_STRIP.sub("", s)
        if s2.strip() and not any(m in s2.lower() for m in _META_MARKERS):
            kept.append(s2)
    out = re.sub(r"\s{2,}", " ", " ".join(kept)).strip(" .\n")
    if len(out) < 12:
        return None
    return out

CLOSING_PHRASES_RE = re.compile(
    r"\b(bye|bye[- ]bye|goodbye|see ya|see you|talk later|"
    r"that's all|thank you bye|have a good day)\b",
    re.IGNORECASE,
)

#: Fixed closing line for the deterministic hangup path.
#:
#: This is spoken on every sign-off — "bye", "goodbye", "see you", "that's
#: all" — so it names the product to every caller who hangs up politely. It
#: previously named the sibling product this engine was copied from, which
#: meant ANITA callers were thanked by a university admissions office
#: (work-status §10, CL-1).
CLOSING_REPLY = "Thank you for calling ANITA Medical Systems. Have a great day!"


def is_closing_phrase(text: str) -> bool:
    """Hard sign-off cues — handled deterministically, before the LLM."""
    return bool(CLOSING_PHRASES_RE.search(text))


def is_noise_fragment(text: str) -> bool:
    """Ultra-short or single-noise-word transcripts (STT artifacts)."""
    t = text.strip().strip(".,!?;").lower()
    if len(t) < STT_MIN_CHARS:
        return True
    return len(t.split()) == 1 and t in _NOISE_WORDS


def _split_sentences(text: str) -> list[str]:
    """Split answer text at sentence boundaries for per-sentence streaming.

    kokoro's `create_stream` splits phonemes into batches up to
    MAX_PHONEME_LENGTH (~510 phonemes) and prefers punctuation boundaries —
    but a typical answer (~150-250 chars) is ONE batch, so the stream yields
    one chunk equal to the whole answer and streaming buys nothing (measured
    live 2026-09-20: tts_first_chunk -> tts_done = 0 ms). Synthesising
    sentence by sentence is what makes the first chunk small: the first
    sentence's audio exists while later ones are still being synthesised.

    Conservative split: only on `.` `!` `?` `;` followed by whitespace and a
    capital letter, and never right after a digit -- so a figure like
    "₹2,500. Shipping extra" stays intact while real sentence boundaries
    split. (An abbreviation like "Dr. Smith" still splits into two synthesis
    chunks; that costs a clause boundary of silence, never correctness --
    and US-004's clause cutter replaces this splitter when it lands.)
    """
    parts = re.split(r"(?<![0-9][.!?;])(?<=[.!?;])\s+(?=[A-Z])", text.strip())
    return [p for p in (p.strip() for p in parts) if p]


class VoiceCallSession:
    """
    Handles a single outbound (or inbound) voice call.

    Accumulates audio, runs STT when the caller pauses, queries the
    RAG + LLM pipeline, synthesises the answer via TTS, and returns
    µ-law audio chunks ready to be sent back over Twilio Media Streams.
    """

    def __init__(
        self,
        silence_threshold_frames: int | None = None,  # None -> the live BRD-04 value
        max_utterance_frames: int = 300,     # ~6 seconds max
        sample_rate: int = 8000,             # Twilio uses 8 kHz
        direction: str = "inbound",          # "inbound" | "outbound" — shapes LLM behavior
    ):
        # BRD-04: one decision, and `None` resolves to the setting rather than
        # to a literal. The parameter used to default to the number 30 while no
        # caller ever passed it, so the documented "configuration" was a
        # constant that nothing could reach.
        self._silence_threshold = (_vad_silence_frames()
                                   if silence_threshold_frames is None
                                   else silence_threshold_frames)
        self._max_utterance = max_utterance_frames
        # Speculative-STT state. `_spec_epoch` invalidates a result whose
        # utterance has already been reset, so a slow transcription can never
        # be mistaken for the next caller turn's audio.
        self._spec_task = None
        self._spec_result = None
        self._spec_epoch = 0
        self._spec_started = False
        self._spec_advance_ms = _speculative_advance_ms()
        self._sample_rate = sample_rate
        self.direction = direction
        self._noise_streak = 0  # consecutive noise-gated turns
        # TAC-6. Started at construction, which is when the session is created
        # for a call — the relay builds one per media stream, so this is the
        # call's own clock rather than the process's.
        self._call_started = _time.monotonic()
        self._max_call_seconds = _max_call_seconds()
        self._capped = False
        self.call_id = ""       # set by the WS handler (stream_sid)
        # US-004: who is calling, when the carrier tells us, and which channel
        # this session actually is. Both are set by the relay that owns the
        # socket — the media stream sets a phone number, the browser relay
        # marks the session as browser_mic. Defaults describe a PSTN call.
        self.caller = ""
        self.channel_kind = "voice_inbound"

        # US-012: the per-call TTS cache. Unused while TTS_CACHE_SCOPE is
        # "shared" (the DG-06 default), where every session reads and writes the
        # class-level cache instead. It is allocated unconditionally so that
        # switching scope is a configuration change with no code path missing.
        self._session_tts_cache: dict = {}
        self._turn = 0          # utterance counter for the event timeline

        # Audio buffer
        self._audio_buffer: list[bytes] = []      # raw µ-law chunks
        self._silence_count = 0
        self._total_frames = 0

        # US-014: the turn state, and the reference signal the echo gate
        # measures the caller against. `_playout` is the outbound audio the
        # carrier is playing out on a 20 ms clock; one inbound frame consumes
        # one outbound frame, which is what keeps the reference ALIGNED with
        # what the caller is hearing right now rather than with what we sent
        # minutes ago. Without that alignment the gate would measure a quiet
        # passage late in a long answer against the loud passage at its start
        # and admit the agent's own voice as an interruption.
        self.state = STATE_LISTENING
        self._playout: list[bytes] = []
        self._ref_env = 0.0
        self._ref_at: float | None = None
        self._barge_run = 0
        #: Set when a turn began as an admitted interruption, so the min-burst
        #: noise floor can be applied knowingly (see `process_utterance`).
        self._turn_began_as_interruption = False
        #: US-004's cancel signal, produced here rather than left unwired. The
        #: chain below it (anita.voice_rag.query_rag_stream -> anita.voice_llm)
        #: has always consumed one; nothing ever made one, so `cancel` was
        #: permanently None and `GenerationCancelled` was unreachable.
        self._cancel: asyncio.Event | None = None
        #: What the gate did during the last playback window, logged once when
        #: the window closes. Per-frame logging here would be 50 lines/second
        #: and would say nothing a count does not.
        self._barge_counts = {"echo": 0, "short": 0, "admitted": 0}

        # Conversation context (accumulated across utterances)
        self._conversation_history: list[str] = []

        # US-001 turn tracing (app/perf_trace.py). Never raises; may be None.
        self._trace = None
        #: US-017: did this turn's answer reach the caller clause by clause?
        #: On the streamed path the history already holds only what was heard,
        #: so a barge-in must not add a "not fully heard" marker to it.
        self._spoke_streamed = False

    def log_event(self, name: str, **fields) -> None:
        """Emit one structured event line for the call timeline."""
        extra = " ".join(f"{k}={v}" for k, v in fields.items())
        voice_events.info("EVENT %s turn=%d %s %s", self.call_id or "-", self._turn, name, extra)

    # ── Public API ──────────────────────────────────────────────────

    def reset_utterance(self):
        """Clear the current utterance buffer (call when speech ends)."""
        self._audio_buffer.clear()
        self._silence_count = 0
        self._total_frames = 0
        # Invalidate any in-flight speculation. The epoch, not the snapshot
        # alone, is what makes this safe: the harness loops a four-fixture set,
        # so two turns genuinely can carry byte-identical audio, and a stale
        # result would otherwise be indistinguishable from a fresh one.
        self._spec_epoch += 1
        self._spec_started = False
        self._spec_result = None
        self._spec_task = None

    # ── US-014: the turn state and the ONE inbound-frame decision ──────

    def mark_speaking(self, chunks: list[bytes] | None = None) -> None:
        """Enter SPEAKING, and seed the reference the echo gate compares to.

        `chunks` is the audio being handed to the carrier. It is queued rather
        than measured here because the carrier plays it out on its own clock:
        the reference has to follow the PLAYOUT position, not the send position.

        The reference is PRIMED here, not on the first tick. Without that, a
        session entering SPEAKING has `_ref_env == 0` until an inbound frame
        arrives, so the ratio test degenerates to the absolute floor alone
        exactly at playback onset -- the moment the carrier starts returning our
        voice, and therefore the moment a floor-only gate is most likely to
        admit our own speech as an interruption.
        """
        self.state = STATE_SPEAKING
        self._barge_run = 0
        self._ref_at = _time.monotonic()
        self.note_playback(chunks)
        if self._playout:
            self._ref_env = _reference_rms(self._playout, 0)

    def note_playback(self, frames: list[bytes] | None) -> None:
        """Queue frames the carrier will play out, for the gate's reference.

        Called once per frame actually written to the socket, in send order, so
        the reference tracks what the caller is hearing rather than what was
        synthesised. A frame we never sent is a frame nobody hears, and counting
        it would raise the threshold against the caller for no reason.
        """
        if frames:
            self._playout.extend(frames)

    def mark_listening(self) -> None:
        """Leave SPEAKING: playback finished, or was cancelled, or the stream died.

        Must be on every exit path from a turn. A session left in SPEAKING would
        gate the caller's ordinary speech as echo for the rest of the call.
        """
        if any(self._barge_counts.values()):
            # The gate's own account of the window it was watching. `echo` frames
            # were rejected as our own bleed, `short` runs were speech-shaped but
            # too brief to believe, `admitted` is what interrupted. This is the
            # measurement TAC-4 tunes against, and it is the signal the old
            # `BARGE_IN_DETECTED` claimed to provide per frame and never did once.
            self.log_event("BARGE_IN_SUMMARY", **self._barge_counts)
            self._barge_counts = {"echo": 0, "short": 0, "admitted": 0}
        self.state = STATE_LISTENING
        self._barge_run = 0
        self._playout.clear()
        self._ref_env = 0.0
        # A new playback window starts its own playout clock; keeping the old
        # timestamp would make the first frame of the next window look like a
        # stall of however long the silence lasted.
        self._ref_at = None

    # ── the per-turn cancel signal (US-004's producer) ─────────────────

    def begin_turn(self) -> None:
        """Open a turn and mint its cancel signal.

        Fresh per turn, never reused: an Event left set from the previous
        interruption would cancel the next turn the moment it started.

        Deliberately does NOT clear `_turn_began_as_interruption`: the gate sets
        that when it admits the interrupting frame, which is BEFORE the utterance
        closes and this runs -- clearing it here would erase the only record that
        this turn started as an interruption.
        """
        self._cancel = asyncio.Event()

    def request_interruption(self) -> None:
        """Ask the running turn to stop. Idempotent, and safe before a turn exists."""
        if self._cancel is not None:
            self._cancel.set()

    def cancel_requested(self) -> bool:
        """Has this turn been interrupted? Read by the turn's own send loop."""
        return self._cancel is not None and self._cancel.is_set()

    def end_turn(self) -> None:
        """Close the turn. Does NOT clear the caller's audio buffer."""
        self._cancel = None
        self._turn_began_as_interruption = False
        self.mark_listening()

    def note_answer_truncated(self) -> None:
        """The caller cut in mid-playback: mark the answer they did not finish hearing.

        On the BATCH path (`TTS_STREAM=0`, the shipped configuration) generation
        has already completed by the time an interruption lands -- the turn is
        stopped in its send loop -- so `process_utterance` has already written
        the whole answer into the history. The caller heard only the frames that
        were actually sent before the `clear`.

        The text cannot be truncated to match, because there is no exact mapping
        from sent audio back to characters. So the history keeps the answer and
        gains a marker saying the ending was not heard, which is what the model
        actually needs: without it, the next turn's model believes it delivered
        an answer the caller is still waiting for, and will refuse to repeat it.

        Idempotent, and a no-op when the last entry is not an answer.

        US-017: also a no-op on the streamed path, where `_query_llm_streamed`
        writes only the clauses that were actually spoken — marking that entry
        "not fully heard" would claim the caller missed something the assistant
        never said.
        """
        if self._spoke_streamed:
            return
        if not self._conversation_history:
            return
        last = self._conversation_history[-1]
        if not last.startswith("Assistant: ") or last.endswith(TRUNCATION_MARK):
            return
        self._conversation_history[-1] = f"{last} {TRUNCATION_MARK}"

    def _advance_reference(self) -> None:
        """Move the reference along the playout clock.

        Driven by `time.monotonic()`, not by inbound frames. Frames are only the
        OPPORTUNITY to sample the playout position; if the inbound stream stalls
        or arrives in a burst, counting frames would let the reference drift
        behind what the caller is hearing and measure them against a passage
        that has already ended. Wall clock cannot drift.

        Catch-up is bounded: more than half a second behind means the stream
        stalled or the process was descheduled, and replaying that much playout
        in one step would produce a reference that means nothing. The floor is
        the honest fallback, so the queue is drained instead.
        """
        now = _time.monotonic()
        if self._ref_at is None:
            self._ref_at = now
        ticks = int(((now - self._ref_at) * 1000.0) // FRAME_MS)
        if ticks <= 0:
            return                      # same frame: the reference holds
        if ticks > 25:                  # > 500 ms behind: re-sync, do not replay
            self._ref_at = now
            self._playout.clear()
            # Decay across the gap that was missed rather than one step: a
            # single 0.95 would leave a threshold standing on a passage that
            # ended over half a second ago, and the floor is the honest fallback
            # when we no longer know what is playing. Bounded so the result is
            # deterministic rather than denormal.
            self._ref_env *= BARGE_IN_REF_DECAY ** min(ticks, 200)
            return
        self._ref_at += ticks * FRAME_MS / 1000.0
        for _ in range(ticks):
            if not self._playout:
                break
            self._playout.pop(0)
        if self._playout:
            self._ref_env = _reference_rms(self._playout, 0)
        else:
            self._ref_env *= BARGE_IN_REF_DECAY

    def admit_inbound_frame(self, ulaw_chunk: bytes) -> str:
        """The ONE place a caller frame is admitted or rejected while we speak.

        TRD-05: one decision point. Both Twilio handlers call this and nothing
        else decides; before US-014 the same four lines were copy-pasted into
        each handler and neither copy could execute.

        Returns exactly one of `FRAME_LISTENING` / `FRAME_INTERRUPTION` /
        `FRAME_ECHO` / `FRAME_MUTED`. The verdict says what the frame IS; the
        caller decides what to do about it. An admitted frame is handed to
        `feed_audio` by the handler so that the buffer, the VAD and BRD-04's one
        end-of-speech decision stay in a single place.
        """
        if self.state != STATE_SPEAKING:
            return FRAME_LISTENING

        if not BARGE_IN_ENABLED:
            if not MUTE_STT_DURING_TTS:
                # The pre-existing escape hatch, kept and still read: a
                # deployment that would rather take the mic bleed than lose the
                # caller's overlap sets this to 0 and gets the frames through.
                # With the gate ON this key is superseded -- the gate does the
                # job it was approximating by doing nothing.
                return FRAME_LISTENING
            # The BRD-15 revert. This is the discard the documents always
            # claimed and the code never did: before the reader split the guard
            # was unreachable, so the real behaviour was to buffer the caller's
            # overlapping speech and feed it back as a fresh turn after playback.
            self.reset_utterance()
            self._advance_reference()
            return FRAME_MUTED

        rms = frame_rms(ulaw_chunk)
        threshold = max(BARGE_IN_ABS_FLOOR,
                        self._ref_env * BARGE_IN_ECHO_GAIN * BARGE_IN_MARGIN)
        self._advance_reference()

        if rms < threshold:
            self._barge_counts["echo"] += 1
            if self._barge_run:
                # A run was counting and ran out: speech-shaped but too brief to
                # believe. Counted separately from plain echo, because the two
                # have different fixes -- `echo` is a gate that is too loose,
                # `short` is one that is too strict.
                self._barge_counts["short"] += 1
            self._barge_run = 0
            return FRAME_ECHO

        self._barge_run += 1
        if self._barge_run < BARGE_IN_MIN_FRAMES:
            # Still counting. A click, a lip smack or a single frame of line
            # noise clears the ratio and stops here; a spoken word does not.
            # These frames are deliberately NOT buffered: holding them would
            # mean replaying audio into the buffer from two places, and the
            # cost of not holding them is the first ~100 ms of the caller's
            # first word. If TAC-4's tuning shows that onset costing the
            # transcript, buffering the run is the contained fix.
            return FRAME_ECHO

        # Believed: the caller is speaking over the agent. From here the frame
        # is ordinary caller audio -- state returns to LISTENING so that the
        # rest of the utterance is not re-litigated frame by frame, and the
        # turn that follows is closed by BRD-04's one endpointing rule.
        self.state = STATE_LISTENING
        self._turn_began_as_interruption = True
        self._barge_counts["admitted"] += 1
        # The evidence for TAC-4: what was heard, what it was measured against,
        # and by how much it cleared it. The threshold is recorded rather than
        # recomputed later, because the reference it came from decays.
        self.log_event("BARGE_IN_ADMITTED", frames=BARGE_IN_MIN_FRAMES,
                       rms=round(rms, 1), ref_env=round(self._ref_env, 1),
                       threshold=round(threshold, 1))
        self.request_interruption()
        return FRAME_INTERRUPTION

    def _maybe_start_speculative_stt(self) -> None:
        """Transcribe the audio-so-far while the pause is still being measured.

        This does NOT close the turn early and does NOT change the delay: the
        caller is still given the full silence window. It only moves the STT
        pass inside that window, so the ~200 ms it costs is no longer added to
        the caller's wait.

        Skipped when there is no running loop (a synchronous caller, e.g. a
        unit test driving feed_audio directly), because there is nothing to
        schedule the transcription onto.
        """
        if self._spec_advance_ms <= 0 or self._spec_started:
            return
        trigger = self._silence_threshold - round(self._spec_advance_ms / 20)
        if self._silence_count < trigger:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._spec_started = True
        snapshot = b"".join(self._audio_buffer)
        epoch = self._spec_epoch
        self._spec_task = loop.create_task(self._speculative_stt(snapshot, epoch))

    async def _speculative_stt(self, snapshot: bytes, epoch: int) -> None:
        """Background transcription of a snapshot. Never raises into the caller."""
        try:
            audio = self._prepare_audio(snapshot)
            self._spec_result = (epoch, snapshot) + await self._transcribe(audio)
        except Exception:                       # noqa: BLE001
            # A failed speculation is not an error -- the real pass still runs.
            self.log_event("SPECULATIVE_STT_FAILED")
            self._spec_result = None

    def _consume_speculative(self) -> tuple[str, bool] | None:
        """The speculative transcript, if it exists and is still this audio."""
        res = self._spec_result
        if res is None:
            return None
        epoch, snapshot, text, low_conf = res
        if epoch != self._spec_epoch:
            return None                          # a later utterance; stale
        if snapshot != b"".join(self._audio_buffer):
            return None                          # caller resumed; different audio
        self.log_event("STT_FROM_SPECULATIVE", chars=len(text or ""))
        return text, low_conf

    def _prepare_audio(self, ulaw: bytes) -> np.ndarray:
        """µ-law -> resampled 16 kHz int16, for Whisper.

        Extracted so the speculative and the real pass preprocess IDENTICALLY.
        Two copies of this would be two chances to transcribe the same audio
        two different ways, and the bug would show up as an intermittent
        transcript difference on a call.

        Returns **int16**, which is what `_transcribe` expects: it does the
        int16 -> float32 normalisation itself, immediately before calling
        faster-whisper. The two belong together — `_prepare_audio` scales to
        int16 and `_transcribe` scales back — and a change to one without the
        other produces audio that is either 32768x too loud or 32768x too
        quiet. Both fail as a Whisper hallucination rather than as an error,
        which is why this pairing is stated here rather than left implicit.
        """
        pcm_bytes = ulaw_to_pcm(ulaw)
        audio_8k = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        try:
            from scipy.signal import resample

            audio_16k = resample(audio_8k, int(len(audio_8k) * 16000 / 8000))
        except Exception:
            audio_16k = np.repeat(audio_8k, 2)
        return (audio_16k * 32767).clip(-32768, 32767).astype(np.int16)

    def _noise_reply(self) -> tuple[str, bool]:
        """
        Fixed reply for noise-gated turns (D4 escalation ladder, AC-7):
          1st miss  → plain retry
          2nd miss  → line-quality hint
          3rd miss  → human-transfer offer, and the call ENDS

        Each step is a DIFFERENT sentence — never the identical line twice
        in a row, which is the defect this ladder exists to prevent.

        Returns ``(reply, end_call)``. The third rung is terminal. AC-7 asks for
        a human or a callback *and* for the call to close cleanly; a ladder that
        only repeats its last rung would leave a caller who cannot be heard on
        the line indefinitely, holding one of the two admission slots.
        """
        self._noise_streak += 1
        rung = min(self._noise_streak, len(_NOISE_LADDER)) - 1
        return _NOISE_LADDER[rung], self._noise_streak >= len(_NOISE_LADDER)

    # -- TAC-6: the call ceiling -------------------------------------------
    def call_seconds(self) -> float:
        """How long this call has been up."""
        return _time.monotonic() - self._call_started

    def call_expired(self) -> bool:
        """True once the call has reached its ceiling.

        Public so the media relay can ask on every inbound frame: the ceiling
        has to fire when *nothing* is happening — which is exactly when no turn
        is being processed and no other method of this class is called.
        """
        if self._capped:
            return False
        return self.call_seconds() >= self._max_call_seconds

    def mark_capped(self) -> None:
        """Record that the ceiling has been announced, so it is said once."""
        self._capped = True

    def finish_trace(self, *, outcome: str = "answered") -> None:
        """Emit this turn's trace and clear it. Best-effort: never raises.

        The media relay calls this when a turn resolves — normally, on a
        barge-in cancellation, and on failure — so every turn that reached
        end-of-speech leaves a record regardless of how it ended.
        """
        trace, self._trace = self._trace, None
        if trace is None:
            return
        try:
            from .voice_trace import emit_trace

            emit_trace(trace, outcome=outcome)
        except Exception:  # noqa: BLE001 - a trace must never affect the call
            pass

    def is_silent(self, ulaw_chunk: bytes) -> bool:
        """
        Quick energy-based check: is this chunk mostly silence?

        Decodes a few samples and checks RMS amplitude.
        """
        if len(ulaw_chunk) < 2:
            return True
        return frame_rms(ulaw_chunk) < 80  # threshold for 8 kHz µ-law

    def feed_audio(self, ulaw_chunk: bytes) -> bool:
        """
        Feed one µ-law chunk into the session.

        Returns True if the caller has just stopped speaking
        (i.e. we should now process the utterance).
        """
        silent = self.is_silent(ulaw_chunk)

        if not silent:
            # Speech — accumulate
            if not self._audio_buffer:
                self.log_event("USER_SPEECH_STARTED")
            self._audio_buffer.append(ulaw_chunk)
            self._silence_count = 0
            self._total_frames += 1
            # The caller resumed: whatever was speculated is now the wrong
            # audio, and a later pause in this same utterance may speculate
            # again on the longer buffer.
            self._spec_started = False
            self._spec_result = None
        elif self._audio_buffer:
            # Silence AFTER speech — count trailing silence
            self._silence_count += 1
            self._maybe_start_speculative_stt()

        # Trigger on trailing silence OR max utterance length
        if self._audio_buffer and (
            self._silence_count >= self._silence_threshold
            or self._total_frames >= self._max_utterance
        ):
            self.log_event("USER_SPEECH_STOPPED", frames=self._total_frames)
            # US-001: open a trace for this turn. vad_end is the zero point for
            # processing_ms. The 600 ms the caller waited before this instant is
            # endpointing_ms, which BRD-02 measures separately — do not conflate
            # the two clocks. Guarded: tracing never breaks a call.
            try:
                from .voice_trace import new_trace

                self._trace = new_trace(self.call_id, self._turn)
                self._trace.mark("vad_end")
                self._trace.note(
                    vad_frames=self._total_frames,
                    endpoint_ms=self._silence_threshold * 20,
                    # US-017 T-10: every caller turn is stamped with its work
                    # class, so the priority rule ("zero background starts
                    # during a voice turn") is countable from the records after
                    # the fact rather than only observable live. A rule that
                    # cannot be counted from the record is a rule that can only
                    # be asserted.
                    work_class="voice",
                )
            except Exception:
                self._trace = None
            return True
        return False

    async def process_utterance(self, chunk_sink: Callable | None = None
                                ) -> tuple[list[bytes], str, bool]:
        """
        Run the full pipeline on the accumulated audio:

           µ-law buffer → PCM WAV → Whisper STT → RAG + LLM → Kokoro TTS → µ-law chunks

        Returns a tuple of (ulaw_chunks, transcript_dialogue, end_call) where
        transcript_dialogue is the full exchange (caller + AI) for logging and
        end_call is True when a hard sign-off cue was detected (the caller
        should hang up after playing the returned audio).
        """
        # ── TAC-6: the call ceiling, checked before anything else ──────────
        # Before the empty-buffer return, deliberately: the case this exists
        # for is a call where end-of-speech never succeeds, so by definition
        # there may be no audio to process. Checking later would mean the cap
        # only ever fired on calls that did not need it.
        if self.call_expired():
            self.mark_capped()
            logger.warning(
                "VoiceCall: call ceiling reached at %.0fs — closing",
                self.call_seconds(),
            )
            self.log_event("CALL_CAPPED", seconds=round(self.call_seconds(), 1))
            self.reset_utterance()
            tts_pcm = await self._synthesise(CALL_CAP_REPLY)
            dialogue = f"Assistant: {CALL_CAP_REPLY}"
            self._conversation_history.append(dialogue)
            if tts_pcm is None or len(tts_pcm) == 0:
                # Nothing to play, but the call still ends: an uncapped silent
                # call holds an admission slot forever.
                return [], dialogue, True
            return _turn_outcome(self._pcm_to_ulaw_chunks(tts_pcm)), dialogue, True

        if not self._audio_buffer:
            self.reset_utterance()
            return [], "", False

        self._turn += 1
        self.begin_turn()
        self.log_event("UTTERANCE_PROCESSING", frames=self._total_frames,
                       barge_in=self._turn_began_as_interruption)

        # ── Min-burst filter: drop sub-300ms noise bursts before STT ──
        # US-014: an admitted interruption is held to a lower floor. The 300 ms
        # rule exists to keep a cough or a door slam out of the LLM, and the gate
        # has already applied that test to this audio by a different means -- it
        # cleared the echo ratio for BARGE_IN_MIN_FRAMES and was believed as
        # speech. Re-applying 300 ms would discard the very utterance the caller
        # interrupted to make ("stop", "wait", "no -- "), which is the failure
        # this story exists to fix. This is NOT a second end-of-turn decision:
        # BRD-04 still closes the turn, at 600 ms, exactly once (TAC-5).
        floor = (BARGE_IN_MIN_FRAMES if self._turn_began_as_interruption
                 else MIN_UTTERANCE_FRAMES)
        if self._total_frames < floor:
            logger.info(
                f"VoiceCall: dropped {self._total_frames}-frame burst "
                f"(<{floor * 20} ms) as noise"
            )
            self.reset_utterance()
            return [], "", False

        # ── Step 1: Decode µ-law → 16 kHz mono for Whisper ────────
        combined_ulaw = b"".join(self._audio_buffer)
        audio_16k_int16 = self._prepare_audio(combined_ulaw)

        logger.info(
            f"VoiceCall: processing utterance ({len(self._audio_buffer)} chunks, "
            f"{len(audio_16k_int16)/16000:.1f}s at 16 kHz)"
        )

        # ── Step 2: Whisper STT ───────────────────────────────────
        # Prefer the pass that already ran during the endpointing window. It is
        # the same audio and the same preprocessing, so the transcript is the
        # same one -- but it cost the caller nothing.
        spec = self._consume_speculative()
        if spec is not None:
            transcript, low_conf = spec
            # STT DID happen for this turn -- it happened during the endpointing
            # window, which is the whole point. The stage must still be closed
            # here, or the record loses it: `stt_done` is marked inside
            # `_transcribe`, which this path never calls, and a 200-turn run
            # then reports `stages_seen: 6` with no STT stage at all. The trace's
            # only job is to say where the time went, and a stage that silently
            # vanishes from it is worse than a slow one.
            if self._trace is not None:
                self._trace.mark("stt_done")
                self._trace.note(stt_chars=len(transcript),
                                 stt_low_conf=bool(low_conf),
                                 stt_from_speculative=True)
        else:
            self.log_event("STT_STARTED")
            transcript, low_conf = await self._transcribe(audio_16k_int16)
        self.log_event("STT_FINAL", chars=len(transcript), low_conf=low_conf)
        self.reset_utterance()

        if not transcript or not transcript.strip():
            # VAD removed all audio (pure noise/echo) — one quick check
            # line is better than dead air; the LLM never sees this.
            logger.info("VoiceCall: no speech in utterance — fixed fallback reply")
            reply, end_call = self._noise_reply()
            dialogue = f"Caller: (unintelligible)\nAssistant: {reply}"
            self._conversation_history.append(f"Caller: (unintelligible)")
            self._conversation_history.append(f"Assistant: {reply}")
            tts_pcm = await self._synthesise(reply)
            if tts_pcm is None or len(tts_pcm) == 0:
                return [], dialogue, end_call
            return _turn_outcome(self._pcm_to_ulaw_chunks(tts_pcm)), dialogue, end_call

        logger.info(f"VoiceCall: transcript = \"{transcript[:120]}\"")

        # Add to conversation history
        self._conversation_history.append(f"Caller: {transcript}")

        # ── Step 2b: Noise gate — short fragments / low-confidence STT
        #    get a fixed one-line check; the LLM never sees them.
        if low_conf or is_noise_fragment(transcript):
            logger.info(
                f"VoiceCall: noise-gated \"{transcript[:60]}\" "
                f"(low_conf={low_conf}) — fixed fallback reply"
            )
            reply, end_call = self._noise_reply()
            dialogue = f"Caller: {transcript}\nAssistant: {reply}"
            self._conversation_history.append(f"Assistant: {reply}")
            tts_pcm = await self._synthesise(reply)
            if tts_pcm is None or len(tts_pcm) == 0:
                return [], dialogue, end_call
            return _turn_outcome(self._pcm_to_ulaw_chunks(tts_pcm)), dialogue, end_call

        # Real speech decoded — reset the noise streak.
        self._noise_streak = 0

        # ── Step 2c: Deterministic hangup — hard sign-off cues end the
        #    call with a static closing; the LLM never sees them.
        if is_closing_phrase(transcript):
            logger.info(f"VoiceCall: closing phrase detected: \"{transcript[:60]}\"")
            reply = CLOSING_REPLY
            dialogue = f"Caller: {transcript}\nAssistant: {reply}"
            self._conversation_history.append(f"Assistant: {reply}")
            tts_pcm = await self._synthesise(reply)
            if tts_pcm is None or len(tts_pcm) == 0:
                return [], dialogue, True
            return _turn_outcome(self._pcm_to_ulaw_chunks(tts_pcm)), dialogue, True

        # ── Step 3: RAG + LLM ─────────────────────────────────────
        self.log_event("LLM_STARTED")
        from .voice_llm import LLM_STREAM

        streamed_chunks: list[bytes] = []
        if LLM_STREAM:
            # US-004: clauses are spoken as the model decodes (the overlap
            # happens only when TTS_STREAM is on too -- without it the clauses
            # accumulate and the batch synthesis below is unchanged).
            # US-014: the cancel signal goes with it, so an interruption stops
            # the decode rather than only muting its output.
            answer, streamed_chunks = await self._query_llm_streamed(
                transcript, chunk_sink if TTS_STREAM else None, cancel=self._cancel)
        else:
            answer = await self._query_llm(transcript)
        self.log_event("LLM_COMPLETED", chars=len(answer or ""))
        if self.cancel_requested():
            # The caller interrupted while we were generating. There is nothing
            # to say and nothing to apologise for: the utterance AFTER this one
            # is already being collected in the buffer, and answering the
            # interruption with the degraded asset ("I'm sorry, I didn't catch
            # that") would respond to a successful interruption with a
            # non-sequitur -- and count the turn `degraded` when it was in fact
            # `served` by being stopped. An empty audio return is correct here.
            #
            # The history entry is what the prompt's contract is written against
            # ("your response appears with [interrupted here]"). On the streamed
            # path `parts` holds only the clauses actually SPOKEN, so the marker
            # lands at the point the caller really cut in; on the batch path
            # nothing was spoken, and saying so is more useful to the model than
            # an empty answer it might try to finish.
            heard = " ".join(answer.split()) if answer else ""
            self._conversation_history.append(
                "Assistant: " + (
                    f"{heard} [interrupted here]"
                    if heard else
                    "[interrupted here - the caller began speaking before I answered]"
                )
            )
            self.log_event("TURN_INTERRUPTED", stage="generation",
                           spoken_chars=len(heard))
            dialogue = f"Caller: {transcript}\nAssistant: (interrupted)"
            return [], dialogue, False
        if not answer:
            # US-016 AC-5: the engine returned nothing. This used to return []
            # -- silence on a live line. The caller now hears the fixed
            # response, played from the asset set, so the turn ends in a
            # sentence rather than in nothing. The record names it as a
            # degraded turn, which is a different outcome from served, failed
            # and refused and is never collapsed into any of them.
            dialogue = f"Caller: {transcript}\nAssistant: (no response)"
            chunks = self._speak_fixed_response(reason="empty generation")
            if chunks:
                return chunks, dialogue, False
            return [], dialogue, False

        # ── Step 3b: D1 output boundary — strip leaked reasoning/meta
        #    narration before anything is spoken.
        scrubbed = scrub_meta_leak(answer)
        if scrubbed != answer:
            self.log_event("META_LEAK_SCRUBBED", before=len(answer), after=len(scrubbed or ""))
        if scrubbed is None:
            logger.warning(f"VoiceCall: answer fully scrubbed as meta-leak: {answer[:100]!r}")
            scrubbed = NOISE_REPLY
        answer = scrubbed

        self._conversation_history.append(f"Assistant: {answer}")
        logger.info(f"VoiceCall: answer ({len(answer)} chars) = \"{answer[:120]}...\"")

        # Build dialogue for transcript logging
        dialogue = f"Caller: {transcript}\nAssistant: {answer}"

        # ── Step 4: Kokoro TTS → PCM ──────────────────────────────
        # US-005 (streaming, behind TTS_STREAM): chunks are converted and
        # forwarded through `chunk_sink` as they are produced, so first audio
        # reaches the socket while later clauses are still synthesising. The
        # batch path below is the unchanged BRD-15 revert (flag off).
        if TTS_STREAM:
            if LLM_STREAM and streamed_chunks:
                # The clauses were already synthesised and sent as they were
                # cut; nothing left to do but record the turn.
                return _turn_outcome(streamed_chunks), dialogue, False
            tts_chunks = await self._synthesise_streamed(answer, chunk_sink)
            if tts_chunks is None or not tts_chunks:
                # US-016 AC-5, second branch: the synthesiser failed before
                # anything audible existed. The fixed response needs no
                # synthesiser, so the caller still hears a sentence.
                chunks = self._speak_fixed_response(reason="synthesis failed")
                if chunks:
                    if chunk_sink is not None:
                        await chunk_sink(chunks)
                    return chunks, dialogue, False
                return [], dialogue, False
            return _turn_outcome(tts_chunks), dialogue, False

        tts_pcm = await self._synthesise(answer)
        if tts_pcm is None or len(tts_pcm) == 0:
            # US-016 AC-5, second branch: the synthesiser is the thing that
            # failed. The fixed response needs no synthesiser, so the caller
            # still hears a sentence rather than the tail of a dead turn.
            dialogue = f"Caller: {transcript}\nAssistant: {answer}"
            chunks = self._speak_fixed_response(reason="synthesis failed")
            if chunks:
                return chunks, dialogue, False
            return [], dialogue, False  # Transcript saved even if TTS fails

        # ── Step 5: PCM → µ-law chunks (320 samples = 20 ms at 16 kHz) ──
        return _turn_outcome(self._pcm_to_ulaw_chunks(tts_pcm)), dialogue, False

    # ── Catalogue dictionary for phone audio corrections ───────────

    #: STT mis-hearings of ANITA's own vocabulary on phone audio.
    #:
    #: Every *target* is vocabulary from this product's own catalogue —
    #: product names and brands from `data/out/ams.sqlite`, plus the company
    #: name — and `test_every_correction_target_is_catalogue_vocabulary`
    #: asserts it against the corpus at test time. That invariant is the
    #: point: this table previously held the sibling product's admissions
    #: vocabulary (`emma`→`MBA`, `intuition`→`tuition`, `maridian`→
    #: `Meridian`), so ANITA callers had their transcripts rewritten into
    #: another product's language (work-status §10, CL-2).
    _CORRECTIONS = {
        "x ray": "X-Ray",
        "ex ray": "X-Ray",
        "ultra sound": "Ultrasound",
        "ultra-sound": "Ultrasound",
        "fuji film": "FUJIFILM",
        "f c r": "FCR",
        "capsule a": "Capsula",
        "pro fect": "Profect",
        "micro bar": "Microbar",
        "intensive flying": "Intensifying",
        "cath-lab": "Cath Lab",
        "annita": "ANITA",
    }

    def _post_process_transcript(self, text: str) -> str:
        """Apply catalogue corrections to fix common STT errors on phone audio.

        Matched on **word boundaries** and escaped. A mis-hearing is a spoken
        word, so a key must not fire inside a longer word that merely contains
        it — the substring behaviour of the previous implementation is how a
        stray `emma` could rewrite an unrelated name.
        """
        result = text
        for wrong, correct in self._CORRECTIONS.items():
            result = re.sub(
                r"\b" + re.escape(wrong) + r"\b",
                correct,
                result,
                flags=re.IGNORECASE,
            )
        return result.strip()

    # ── Internal ──────────────────────────────────────────────────

    async def _transcribe(self, audio_16k_int16: np.ndarray) -> tuple[str, bool]:
        """Run faster-whisper STT in a thread (GPU via CTranslate2).

        Accepts int16 numpy array directly — no PyAV/file I/O needed,
        which avoids the AppLocker DLL block on this machine.

        Returns (transcript, low_confidence): low_confidence is True when
        the best segment's avg_logprob falls below STT_MIN_AVG_LOGPROB or
        no_speech_prob exceeds STT_MAX_NO_SPEECH_PROB.
        """
        try:
            # `to_thread`, because this LOADS faster-whisper when cold -- the
            # boot warmup normally makes it a cache hit, but a warmup that
            # failed (or a model that was evicted) would otherwise load it on
            # the event loop, mid-call, for every live call at once.
            model = await asyncio.to_thread(_get_stt_model)
            # Convert int16 → float32 for faster-whisper
            audio_float = audio_16k_int16.astype(np.float32) / 32768.0

            # faster-whisper API returns (segments, info) — different from openai-whisper.
            # Greedy decoding (beam_size=1) + condition_on_previous_text=False:
            # on noisy phone audio, beam search and previous-text conditioning
            # are what produce hallucination loops ("Listen to her again").
            segments, info = await asyncio.to_thread(
                lambda: model.transcribe(
                    audio_float,
                    language="en",
                    beam_size=1,
                    best_of=1,
                    temperature=0.0,
                    condition_on_previous_text=False,
                    vad_filter=True,
                    vad_parameters=dict(
                        threshold=0.5,
                        min_speech_duration_ms=250,
                        min_silence_duration_ms=500,
                        speech_pad_ms=100,
                    ),
                )
            )
            texts, logprobs, no_speech_probs = [], [], []
            for seg in segments:
                texts.append(seg.text.strip())
                logprobs.append(seg.avg_logprob)
                no_speech_probs.append(seg.no_speech_prob)
            transcript = self._post_process_transcript(" ".join(texts))
            low_conf = bool(
                logprobs
                and (
                    min(logprobs) < STT_MIN_AVG_LOGPROB
                    or max(no_speech_probs) > STT_MAX_NO_SPEECH_PROB
                )
            )
            min_logprob = min(logprobs) if logprobs else None
            logger.info(
                f"STT: \"{transcript[:100]}\" "
                f"(lang={info.language}, prob={info.language_probability:.2f}, "
                f"min_logprob={f'{min_logprob:.2f}' if min_logprob is not None else 'n/a'}, "
                f"low_conf={low_conf})"
            )
            if self._trace is not None:
                self._trace.mark("stt_done")
                self._trace.note(stt_chars=len(transcript), stt_low_conf=bool(low_conf))
            return transcript, low_conf
        except Exception:
            logger.exception("VoiceCall: STT failed")
            return "", True

    async def _query_llm(self, question: str) -> str:
        """Query the shared RAG + LLM pipeline in a thread."""
        try:
            from .voice_pipeline import run_rag_query_sync

            prompt = self._build_llm_prompt(question)

            # US-001: llm_sent brackets retrieval + prefill + generation. The
            # split between those three comes from the engine counters captured
            # in anita.voice_llm and the retrieval_done mark from anita.voice_rag.
            # llm_first_token is deliberately NOT marked here: with a
            # non-streaming call there is no first-token event, and marking this
            # instant would fabricate a TTFT (TRD-21).
            if self._trace is not None:
                self._trace.mark("llm_sent")
            # C3/N1: retrieval embeds the caller's actual utterance, not the
            # assembled instruction prompt (history + style rules dominated
            # the dense vector and diluted the question).
            # `self.call_id` is the stream SID the WS handler set, so the turn
            # is recorded against this call's conversation rather than a
            # process-wide one.
            answer = await asyncio.to_thread(
                run_rag_query_sync, prompt, "voice", question, self.call_id,
                caller=self.caller, channel_kind=self.channel_kind,
            ) or ""
            if self._trace is not None:
                self._trace.mark("llm_done")
                self._trace.note(answer_chars=len(answer))
            return answer
        except Exception:
            logger.exception("VoiceCall: LLM query failed")
            return ""

    async def _query_llm_streamed(
        self,
        question: str,
        chunk_sink: Callable | None,
        cancel: "asyncio.Event | None" = None,
    ) -> tuple[str, list[bytes]]:
        """US-004: the generation is consumed as clauses, and (with a sink and
        TTS_STREAM) each clause is synthesised while the model still decodes —
        the LLM/TTS overlap. Returns (answer_text, ulaw_chunks).

        The first clause's audio can reach the socket while the model is still
        producing the rest; `llm_first_token` is marked at the FIRST delta, so
        the trace carries a real TTFT for streamed turns.
        """
        try:
            from .voice_llm import ClauseCutter, GenerationCancelled, GenerationFailed
            from .voice_rag import query_rag_stream

            # US-017: this is the clause-by-clause path, so a barge-in here
            # leaves a history that already matches what the caller heard.
            self._spoke_streamed = chunk_sink is not None

            prompt = self._build_llm_prompt(question)
            cutter = ClauseCutter()
            parts: list[str] = []
            chunks: list[bytes] = []
            any_audio = False
            first_delta = True
            counters = None
            if self._trace is not None:
                self._trace.mark("llm_sent")

            async def _speak_clause(text: str) -> None:
                nonlocal any_audio
                scrubbed = scrub_meta_leak(text)
                if not scrubbed:
                    return
                parts.append(scrubbed)
                if chunk_sink is None:
                    return
                frames = await self._synthesise_streamed(scrubbed, chunk_sink)
                if frames is None:
                    # AC-4: a clause that fails to synthesise gets one retry,
                    # and a second failure speaks the human-transfer offer
                    # rather than leaving the caller in silence.
                    self.log_event("TTS_CLAUSE_RETRY")
                    logger.warning("VoiceCall: clause synthesis failed — retrying once")
                    frames = await self._synthesise_streamed(scrubbed, chunk_sink)
                if frames is None:
                    self.log_event("TTS_CLAUSE_FALLBACK")
                    logger.error(
                        "VoiceCall: clause synthesis failed twice — speaking the "
                        "human-transfer offer instead of silence"
                    )
                    frames = await self._synthesise_streamed(
                        HUMAN_TRANSFER_REPLY, chunk_sink
                    )
                if frames:
                    chunks.extend(frames)
                    any_audio = True

            cancelled = False
            try:
                # C3/N1: same retrieval-query decoupling as the batch path.
                async for item in query_rag_stream(
                    prompt, cancel=cancel, retrieval_query=question,
                    session_key=self.call_id,
                    caller=self.caller, channel_kind=self.channel_kind,
                ):
                    if isinstance(item, str):
                        if first_delta:
                            first_delta = False
                            self.log_event("LLM_FIRST_TOKEN")
                            if self._trace is not None:
                                self._trace.mark("llm_first_token")
                        # ANITA's ClauseCutter returns plain strings
                        # (llm.py:81 `feed -> list[str]`). The source's
                        # returned Clause objects, so this read `clause.text`
                        # and every clause would have raised AttributeError.
                        for clause in cutter.feed(item):
                            if cancel is not None and cancel.is_set():
                                # US-017: the caller cut in. Stop before the
                                # next clause — the remaining clauses were
                                # already decoded, and speaking them is exactly
                                # the "finish the sentence" behaviour barge-in
                                # exists to remove. Raising the engine's own
                                # cancellation lands in the handler below, so
                                # the tail is not flushed either.
                                raise GenerationCancelled()
                            await _speak_clause(clause)
                    else:
                        counters = item
            except GenerationCancelled:
                # NOT a failure (AC-3). The caller interrupted and the decode
                # stopped: the trailing clause is deliberately NOT flushed,
                # because finishing the sentence is the exact behaviour US-014
                # exists to remove. Before this handler existed the broad
                # `except Exception` below caught it, logged a traceback and
                # returned ("", []) -- which `process_utterance` reads as an
                # empty engine answer and answers with the degraded "I didn't
                # catch that" asset, i.e. an apology for the caller's own
                # successful interruption.
                cancelled = True
                self.log_event("LLM_CANCELLED")
                logger.info("VoiceCall: generation cancelled by caller interruption")
            except GenerationFailed:
                logger.warning("VoiceCall: streamed generation failed")
            if not cancelled:
                # A complete trailing clause is spoken; a half-sentence never is.
                # `flush()` returns str, empty when nothing was held — hence
                # a truthiness test rather than `is not None`.
                tail = cutter.flush()
                if tail:
                    await _speak_clause(tail)

            # `parts` holds only what was actually SPOKEN, so on a cancel this is
            # the heard prefix -- which is what belongs in the conversation
            # history. Recording the full answer would tell the model it had
            # said things the caller never heard, and it would then refuse to
            # repeat them.
            answer = " ".join(parts)
            if self._trace is not None:
                self._trace.mark("llm_done")
                self._trace.note(answer_chars=len(answer))
                if counters is not None:
                    self._trace.note(
                        prompt_eval_count=counters.prompt_eval_count,
                        eval_count=counters.eval_count,
                    )
            if not answer and any_audio is False:
                return "", []
            return answer, chunks
        except Exception:
            logger.exception("VoiceCall: streamed LLM query failed")
            return "", []

    def _build_llm_prompt(self, question: str) -> str:
        """The prompt the serving path sends (shared by batch and stream).

        Catalogue vocabulary only. This text is assembled on every turn and
        was — until CL-5 — the sibling product's admissions prompt; it is
        inert today only because `voice_pipeline.run_rag_query_sync` answers
        the *question*, not the prompt. US-014 wires the prose leg, at which
        point every sentence here reaches a model.
        """
        # Include recent conversation for context
        if self._conversation_history:
            context = "\n".join(self._conversation_history[-6:])
            prompt = (
                f"This is an {self.direction} call.\n"
                f"Previous conversation:\n{context}\n\n"
                f"The caller just said: \"{question}\"\n"
                f"Answer naturally as a catalogue assistant for medical "
                f"equipment. Keep responses concise for voice (under 3 "
                f"sentences). NEVER ask the same or similar clarifying "
                f"question twice in a row. If the caller repeats a similar "
                f"answer, STOP asking — state your best interpretation and "
                f"answer with concrete information from the catalogue, then "
                f"invite the caller to correct you."
            )
            # Deterministic loop-breaker: if the assistant's own last
            # two+ turns were clarification questions, rebuild the
            # prompt with caller-only history and force a grounded
            # answer — quantized models sometimes ignore prompt-only
            # guidance when they can see their own question pattern.
            assistant_turns = [
                h for h in self._conversation_history if h.startswith("Assistant: ")
            ]
            clarify_streak = 0
            for h in reversed(assistant_turns):
                if _is_clarification(h):
                    clarify_streak += 1
                else:
                    break
            if clarify_streak >= 2:
                caller_turns = [
                    h for h in self._conversation_history if h.startswith("Caller: ")
                ]
                prompt = (
                    f"This is an {self.direction} call.\n"
                    f"Caller's recent replies:\n" + "\n".join(caller_turns[-4:]) +
                    f"\n\nThe caller just said: \"{question}\"\n"
                    f"You have already asked for clarification too many "
                    f"times. Do NOT ask any question. Answer now with "
                    f"concrete product information from the catalogue — "
                    f"model, specifications, price — and let the caller "
                    f"correct you."
                )
        else:
            prompt = f"This is an {self.direction} call.\n{question}"
        return prompt

    #: TTS cache (DG-06 / US-012).
    #:
    #: These are CLASS attributes, so the cache is process-wide: every concurrent
    #: caller shares one dict. That is the DG-06 "use-as-is" decision, and it is
    #: conditional on the N=2 isolation test -- not on the argument that content
    #: keys make sharing obviously safe.
    #:
    #: The key is (agent text, voice, speed) and nothing else -- never caller
    #: speech, caller identity or transcript content (TAC-4). Voice and speed
    #: joined the key when they became configurable in this same change: with
    #: them outside it, editing KOKORO_VOICE or KOKORO_SPEED would have kept
    #: serving audio rendered in the OLD voice from cache.
    #:
    #: The value carries the owning call_id. It is not part of the key and never
    #: affects a lookup; it exists so that "caller B received audio synthesised
    #: for caller A" is measurable rather than arguable (MOD-04 A.5.2).
    _shared_tts_cache: dict[tuple, tuple[np.ndarray, int, str]] = {}
    _tts_cache_max = 50

    async def _synthesise(self, text: str) -> np.ndarray | None:
        """Run Kokoro TTS in a thread, return float32 PCM array. Cached for speed."""
        self.log_event("TTS_STARTED", chars=len(text))
        try:
            tts_text = text[:500] if len(text) > 500 else text

            # US-012: shared (DG-06 default) or per-call (the pre-decided
            # fallback). Chosen by configuration only -- AC-4 requires the
            # revert to be a setting, never a code change.
            voice, speed = _tts_voice(), _tts_speed()
            cache = (self._session_tts_cache if TTS_CACHE_SCOPE == "per_call"
                     else VoiceCallSession._shared_tts_cache)
            cache_key = (tts_text, voice, speed)

            # Check cache
            if cache_key in cache:
                cached_audio, cached_sr, owner = cache[cache_key]
                # A "cross-call hit" is caller B being served audio that was
                # synthesised during caller A's session (MOD-04 A.5.2). It is
                # recorded, not prevented -- measuring it is the whole point of
                # US-012, and AC-4 pre-decides what a non-zero count means.
                cross_call = bool(owner) and owner != self.call_id
                logger.debug(f"TTS cache HIT: {tts_text[:60]}...")
                self.log_event("TTS_COMPLETED", cached=1, cross_call=int(cross_call))
                # US-001: mark on EVERY return path. This one was missed on the
                # first pass — the cached path returned before the mark, so a
                # cache hit produced a trace with tts_done absent and the
                # llm_done->first_audio segment unaccounted for. Found by
                # running a live call, not by inspection.
                if self._trace is not None:
                    self._trace.mark("tts_done")
                    self._trace.note(
                        tts_cache_hit=True,
                        tts_cache_scope=TTS_CACHE_SCOPE,
                        tts_cache_cross_call=cross_call,
                        tts_cache_owner=owner,
                        tts_cache_key_sha=_cache_key_sha(cache_key),
                    )
                # Copy discipline: the caller gets its own buffer, so a later
                # mutation of one caller's audio cannot reach the cache or any
                # other caller (TAC-2).
                return cached_audio.copy()

            # Only a miss needs the model. Loading it inside the hit path meant a
            # cache hit still touched the engine, which kept the isolation tests
            # from running without a GPU.
            #
            # `to_thread` because this LOADS the Kokoro ONNX model when cold --
            # and it was never pre-warmed at boot, so the first synthesis of the
            # first call did exactly that, on the event loop, freezing every
            # other live call for the duration. The boot warmup now loads it, so
            # this is normally a cache hit; the thread hop is what makes a cold
            # path survivable rather than fatal.
            kokoro = await asyncio.to_thread(_get_tts_engine)
            audio, sr = await asyncio.to_thread(
                kokoro.create, tts_text, voice=voice, speed=speed
            )
            # Store in cache. Bound first, FIFO by insertion order (TAC-3):
            # re-inserting an existing key keeps its original position, which is
            # what makes the eviction order predictable.
            if cache_key not in cache and len(cache) >= self._tts_cache_max:
                oldest = next(iter(cache))
                del cache[oldest]
            cache[cache_key] = (audio.copy(), sr, self.call_id)

            logger.info(f"VoiceCall: TTS generated ({len(audio)/sr:.1f}s at {sr} Hz)")
            if self._trace is not None:
                self._trace.mark("tts_done")
                self._trace.note(
                    tts_cache_hit=False,
                    tts_cache_scope=TTS_CACHE_SCOPE,
                    tts_cache_cross_call=False,
                    # The key is recorded on the MISS path too -- this line is
                    # what establishes a key's owner. Without it only hits
                    # carried a key, so nothing could attribute one and the
                    # isolation analysis had no owner map to compare against.
                    tts_cache_key_sha=_cache_key_sha(cache_key),
                    audio_s=round(len(audio) / sr, 2),
                )
            # The cache holds its own copy, so the buffer handed back here is
            # the caller's alone.
            return audio
        except Exception:
            logger.exception("VoiceCall: TTS failed")
            return None

    def _chunk_to_ulaw_frames(self, audio: np.ndarray, sr: int) -> list[bytes]:
        """One synthesiser chunk -> 20 ms µ-law frames (US-005 streaming leg).

        Resamples from the chunk's own sample rate (the stream reports it per
        chunk, so a provider change never silently misresamples) and snaps to
        a whole frame: the sub-frame tail of a MID-STREAM chunk is dropped
        rather than padded, because padding a mid-stream chunk would insert a
        silence gap into the middle of a sentence. The loss is < 20 ms of
        audio at a clause boundary -- inaudible next to the clause pause.
        """
        from scipy.signal import resample

        target = int(len(audio) * 8000 / sr)
        target -= target % 160                      # snap to a whole 20 ms frame
        if target <= 0:
            return []
        audio_8k = resample(audio, target)
        int16 = (audio_8k * 32767).clip(-32768, 32767).astype(np.int16)
        return [pcm_to_ulaw(int16[i:i + 160].tobytes())
                for i in range(0, len(int16), 160)]

    async def synthesise_stream(self, text: str, *, voice: str | None = None,
                                speed: float | None = None
                                ) -> AsyncIterator[tuple[np.ndarray, int]]:
        """US-005: yield (audio_f32, sample_rate) chunks as Kokoro produces them.

        Cache hit -> the cached copy is yielded once, immediately. Cache miss ->
        `kokoro.create_stream()` is delegated to a thread per chunk-free await
        (the generator itself runs on the loop; kokoro's async generator does
        the waiting, and the model load stays a to_thread hop). The full audio
        is still assembled and cached so the DAT-11 entry shape is unchanged.

        The first yield marks `tts_first_chunk` on the trace -- the instant
        the first audible audio of the answer existed. A failure BEFORE the
        first chunk re-raises (the caller falls back to the fixed response);
        a failure mid-stream ends the stream (the caller already heard the
        beginning, and the turn is logged degraded, never silent).
        """
        self.log_event("TTS_STARTED", chars=len(text))
        tts_text = text[:500] if len(text) > 500 else text
        voice = voice if voice is not None else _tts_voice()
        speed = speed if speed is not None else _tts_speed()
        cache = (self._session_tts_cache if TTS_CACHE_SCOPE == "per_call"
                 else VoiceCallSession._shared_tts_cache)
        cache_key = (tts_text, voice, speed)

        if cache_key in cache:
            cached_audio, cached_sr, owner = cache[cache_key]
            cross_call = bool(owner) and owner != self.call_id
            logger.debug(f"TTS cache HIT (stream): {tts_text[:60]}...")
            self.log_event("TTS_COMPLETED", cached=1, cross_call=int(cross_call))
            if self._trace is not None:
                # The audio exists in full now: both marks are true at once.
                self._trace.mark("tts_first_chunk")
                self._trace.mark("tts_done")
                self._trace.note(
                    tts_cache_hit=True,
                    tts_cache_scope=TTS_CACHE_SCOPE,
                    tts_cache_cross_call=cross_call,
                    tts_cache_key_sha=_cache_key_sha(cache_key),
                )
            yield cached_audio.copy(), cached_sr
            return

        kokoro = await asyncio.to_thread(_get_tts_engine)
        parts: list[np.ndarray] = []
        sr_out: int | None = None
        first = True
        try:
            # Per SENTENCE, not per answer: kokoro's internal batching makes a
            # typical answer one chunk (measured: tts_first_chunk -> tts_done
            # = 0 ms live), which streams nothing. A sentence at a time makes
            # the first chunk the first sentence -- small, and produced while
            # the rest of the answer is still to come (US-004's clause cutter
            # will replace this splitter when it lands; the stream interface
            # here does not change).
            for sentence in _split_sentences(tts_text):
                if self.cancel_requested():
                    # US-017: stop *producing*. The consumer stops sending on
                    # the same signal; this stops the synthesiser doing work
                    # nobody will hear. A partial is never cached.
                    return
                async for audio, sr in kokoro.create_stream(
                        sentence, voice=voice, speed=speed):
                    if self.cancel_requested():
                        return
                    if first:
                        first = False
                        self.log_event("TTS_FIRST_CHUNK", samples=int(len(audio)))
                        if self._trace is not None:
                            self._trace.mark("tts_first_chunk")
                    sr_out = sr
                    parts.append(audio)
                    yield audio, sr
        except Exception:
            logger.exception("VoiceCall: streaming TTS failed")
            if first:
                raise           # nothing audible was produced -> fallback path
            return              # partial audio heard; end the turn, degraded

        # Stream complete: assemble and cache, so a repeat pays nothing and
        # the DAT-11 entry shape is unchanged by streaming.
        if parts and sr_out:
            audio_full = np.concatenate(parts)
            if cache_key not in cache and len(cache) >= self._tts_cache_max:
                oldest = next(iter(cache))
                del cache[oldest]
            cache[cache_key] = (audio_full.copy(), sr_out, self.call_id)
            logger.info(f"VoiceCall: TTS streamed ({len(audio_full) / sr_out:.1f}s "
                        f"at {sr_out} Hz)")
            if self._trace is not None:
                self._trace.mark("tts_done")
                self._trace.note(
                    tts_cache_hit=False,
                    tts_cache_scope=TTS_CACHE_SCOPE,
                    tts_cache_cross_call=False,
                    tts_cache_key_sha=_cache_key_sha(cache_key),
                    audio_s=round(len(audio_full) / sr_out, 2),
                )

    async def _synthesise_streamed(self, text: str, sink: Callable | None
                                   ) -> list[bytes] | None:
        """Drive the stream, convert per chunk, forward through the sink.

        Returns the full µ-law chunk list (the batch-path contract, so the
        caller's record-keeping is unchanged) or None when the synthesiser
        failed before any audible chunk existed. The sink is awaited per
        chunk; it is what sends the frames to the socket as produced.
        """
        chunks: list[bytes] = []
        any_audio = False
        try:
            async for audio, sr in self.synthesise_stream(text):
                if self.cancel_requested():
                    # US-017: a barge-in mid-clause. Stop sending audio now
                    # rather than finishing the sentence the caller
                    # interrupted; nothing is cached, because a partial is not
                    # the answer. The relay separately sends Twilio a `clear`
                    # so what is already buffered stops playing too.
                    self.log_event("TTS_CANCELLED", chunks=len(chunks))
                    return chunks if any_audio else None
                any_audio = True
                frames = self._chunk_to_ulaw_frames(audio, sr)
                chunks.extend(frames)
                if sink is not None and frames:
                    await sink(frames)
        except Exception:
            logger.exception("VoiceCall: streaming TTS failed")
            return chunks if any_audio else None
        return chunks

    def _speak_fixed_response(self, reason: str) -> list[bytes]:
        """Return the pre-synthesised fixed response as µ-law chunks.

        US-016 / BRD-13: a lost inference engine must end in a deterministic
        fixed response rather than silence. "Deterministic" is literal -- the
        bytes come off disk, so the same failure on two turns in two calls
        produces byte-identical audio, and no model generated any part of it.

        Nothing is synthesised here. That is why this still works when the
        synthesiser is down at the same time as the engine, which is the case
        AC-5 calls out explicitly.

        Returns [] when no asset applies, and the caller then terminates the
        turn cleanly rather than hanging in silence.
        """
        from .voice_admission import FIXED_RESPONSE_ASSET, REGISTRY

        chunks = load_call_asset_ulaw(FIXED_RESPONSE_ASSET)
        if not chunks:
            logger.error(
                "VoiceCall: fixed response unavailable (%s) and no asset to play. "
                "Build the call assets: .venv/Scripts/python.exe "
                "scripts/build_call_assets.py", reason)
            # No asset applies, so this turn is a genuine failure rather than a
            # degraded one. The caller's turn terminates cleanly and the record
            # says which of the two it was.
            REGISTRY.note_failed()
            return []
        # The outcome axis is aggregate: `note_asset_played` counts *that* a
        # canned response was played, and takes no argument. Passing the asset
        # path and call id raised a TypeError the moment that branch was first
        # reached (§8.10) — unobservable only because no asset is configured.
        REGISTRY.note_asset_played()
        self.log_event("FIXED_RESPONSE", reason=reason, chunks=len(chunks))
        if self._trace is not None:
            # A degraded turn is its own outcome. It is not a failure: the
            # caller heard a sentence and the call continued.
            self._trace.note(outcome="degraded", degraded_reason=reason,
                             fixed_response_played=True)
        return chunks

    def _pcm_to_ulaw_chunks(self, audio: np.ndarray) -> list[bytes]:
        """
        Convert a float32 PCM array (Kokoro output, typically 24 kHz) to
        a list of 8 kHz µ-law byte chunks suitable for Twilio Media Streams.

        Each chunk is 320 bytes (20 ms at 8 kHz = 160 samples × 2 bytes).
        """
        # Kokoro outputs at 24 kHz by default — resample to 8 kHz for Twilio
        from scipy.signal import resample

        target_len = int(len(audio) * 8000 / 24000)
        audio_8k = resample(audio, target_len)
        audio_8k_int16 = (audio_8k * 32767).clip(-32768, 32767).astype(np.int16)

        # Convert to µ-law in 20 ms chunks
        chunk_size = 160  # 160 samples = 20 ms at 8 kHz
        chunks = []
        for i in range(0, len(audio_8k_int16), chunk_size):
            chunk = audio_8k_int16[i : i + chunk_size]
            if len(chunk) < chunk_size:
                # Pad last chunk with silence
                chunk = np.pad(chunk, (0, chunk_size - len(chunk)))
            pcm_bytes = chunk.tobytes()
            ulaw_bytes = pcm_to_ulaw(pcm_bytes)
            chunks.append(ulaw_bytes)
        return chunks
