"""Settings shim for the migrated voice engine (REC-01).

`voice.py` was copied from the source platform's `voice_handler.py` — 1,890
lines of working real-time turn loop that would be the single largest cost in
this project to rebuild. It reads 20 settings attributes.

This module presents those 20 names, sourced from ANITA's own configuration
where ANITA has them, so the copied engine reads **one** settings object and no
call site changes.

Twelve of the twenty have no ANITA equivalent. They are the barge-in tuning
constants and the STT confidence gates, and they are **defaults carried from the
source rather than invented**: those values were arrived at experimentally
against real phone audio, and re-deriving them here would be guessing dressed as
configuration. They are exposed as environment-overridable so they remain
tunable, and the ANITA settings that *do* exist (model, voice, VAD timings)
always win.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from .config import load_settings


def _int(key: str, default: int, env: Mapping[str, str] | None = None) -> int:
    raw = (env or os.environ).get(key)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _float(key: str, default: float, env: Mapping[str, str] | None = None) -> float:
    raw = (env or os.environ).get(key)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _bool(key: str, default: bool, env: Mapping[str, str] | None = None) -> bool:
    raw = ((env or os.environ).get(key) or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class VoiceSettings:
    """The 20 attributes `voice.py` reads, in one object."""

    # -- from ANITA's own configuration -----------------------------------
    WHISPER_MODEL: str
    WHISPER_NUM_THREADS: int
    KOKORO_VOICE: str
    KOKORO_SPEED: float
    VAD_SILENCE_MS: int
    VAD_SPECULATIVE_ADVANCE_MS: int
    BARGE_IN_ENABLED: bool
    TTS_STREAM: bool
    MAX_CALL_SECONDS: int

    # -- carried from the source (experimentally derived) -----------------
    BARGE_IN_MARGIN: float
    BARGE_IN_ABS_FLOOR: float
    BARGE_IN_ECHO_GAIN: float
    BARGE_IN_REF_DECAY: float
    BARGE_IN_MIN_FRAMES: int
    MIN_UTTERANCE_FRAMES: int
    STT_MIN_CHARS: int
    STT_MIN_AVG_LOGPROB: float
    STT_MAX_NO_SPEECH_PROB: float
    MUTE_STT_DURING_TTS: bool
    TTS_CACHE_SCOPE: str
    CUDA_VISIBLE_DEVICES: str

    def as_dict(self) -> dict:
        return {
            "whisper_model": self.WHISPER_MODEL,
            "kokoro_voice": self.KOKORO_VOICE,
            "vad_silence_ms": self.VAD_SILENCE_MS,
            "barge_in": self.BARGE_IN_ENABLED,
        }


def build_voice_settings(env: Mapping[str, str] | None = None) -> VoiceSettings:
    """Build the shim from ANITA settings plus carried defaults."""
    anita = load_settings(env=env).speech
    environment = dict(os.environ if env is None else env)
    if env is None:
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", environment.get("CUDA_VISIBLE_DEVICES", "0"))

    return VoiceSettings(
        # ANITA's values always win — these are the ones the platform owns.
        WHISPER_MODEL=anita.whisper_model,
        WHISPER_NUM_THREADS=anita.whisper_num_threads,
        KOKORO_VOICE=anita.kokoro_voice,
        KOKORO_SPEED=anita.kokoro_speed,
        VAD_SILENCE_MS=anita.vad_silence_ms,
        VAD_SPECULATIVE_ADVANCE_MS=anita.vad_speculative_advance_ms,
        BARGE_IN_ENABLED=anita.barge_in_enabled,
        TTS_STREAM=anita.tts_stream,
        MAX_CALL_SECONDS=anita.max_call_seconds,
        # Carried from the source; env-overridable, never silently recomputed.
        BARGE_IN_MARGIN=_float("BARGE_IN_MARGIN", 1.6, environment),
        BARGE_IN_ABS_FLOOR=_float("BARGE_IN_ABS_FLOOR", 380.0, environment),
        BARGE_IN_ECHO_GAIN=_float("BARGE_IN_ECHO_GAIN", 1.25, environment),
        BARGE_IN_REF_DECAY=_float("BARGE_IN_REF_DECAY", 0.985, environment),
        BARGE_IN_MIN_FRAMES=_int("BARGE_IN_MIN_FRAMES", 3, environment),
        MIN_UTTERANCE_FRAMES=_int("MIN_UTTERANCE_FRAMES", 15, environment),
        STT_MIN_CHARS=_int("STT_MIN_CHARS", 3, environment),
        STT_MIN_AVG_LOGPROB=_float("STT_MIN_AVG_LOGPROB", -4.0, environment),
        STT_MAX_NO_SPEECH_PROB=_float("STT_MAX_NO_SPEECH_PROB", 0.8, environment),
        MUTE_STT_DURING_TTS=_bool("MUTE_STT_DURING_TTS", True, environment),
        TTS_CACHE_SCOPE=environment.get("TTS_CACHE_SCOPE", "per_instance"),
        CUDA_VISIBLE_DEVICES=environment.get("CUDA_VISIBLE_DEVICES", "0"),
    )


# The object `voice.py` imports.
settings = build_voice_settings()
