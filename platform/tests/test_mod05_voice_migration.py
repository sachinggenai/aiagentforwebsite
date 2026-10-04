"""MOD-05 voice migration — the copied engine and its adapters (REC-01).

`voice.py` is a **copy**, not a rewrite: 1,890 lines of working real-time turn
loop (VAD → STT → RAG → LLM → TTS, barge-in, echo gate) that would be the single
largest cost in this project to rebuild. These tests pin the properties that
make the copy safe to keep.

Run:  .venv\\Scripts\\python -m pytest tests -q
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

ANITA = PLATFORM / "app" / "anita"


class TestMigrationIntegrity:
    def test_the_engine_is_a_faithful_copy(self):
        """The turn loop must not have been edited in transit — a rewritten
        engine would lose the barge-in and echo-gate tuning that was derived
        experimentally against real phone audio.

        The copy is allowed to differ from the source by **exactly** the
        divergences US-001 requires, listed below. Anything else still fails,
        which is the property this test exists for: a deliberate, traceable
        change and an accidental rewrite must not look the same.
        """
        source = Path("D:/project/universityDemo/app/voice_handler.py")
        if not source.exists():
            pytest.skip("source repo not available")
        copied = (ANITA / "voice.py").read_text(encoding="utf-8")
        original = source.read_text(encoding="utf-8")

        # Compare the *definitions*, not byte equality: prose legitimately
        # names the source module, and the import lines were retargeted.
        def definitions(text: str) -> list[str]:
            return [
                line.strip() for line in text.splitlines()
                if re.match(r"\s*(def |class |async def )", line)
            ]

        copied_defs = definitions(copied)
        source_defs = definitions(original)

        # AC-7 makes the third rung terminal, so `_noise_reply` now reports
        # whether the call should end. TAC-6 adds the call ceiling. Both are
        # requirements the source predates.
        DELIBERATE_GONE = {"def _noise_reply(self) -> str:"}
        DELIBERATE_NEW = {
            "def _noise_reply(self) -> tuple[str, bool]:",
            "def call_seconds(self) -> float:",
            "def call_expired(self) -> bool:",
            "def mark_capped(self) -> None:",
            "def _max_call_seconds() -> int:",
        }

        missing = set(source_defs) - DELIBERATE_GONE - set(copied_defs)
        assert not missing, (
            f"the engine was altered in transit — source definitions are gone: "
            f"{sorted(missing)}"
        )

        added = set(copied_defs) - set(source_defs)
        assert added == DELIBERATE_NEW, (
            f"definitions changed without a recorded reason: "
            f"{sorted(added - DELIBERATE_NEW)}"
        )

        # And the tuning constants must survive verbatim.
        for constant in ("BARGE_IN_ABS_FLOOR =", "BARGE_IN_ECHO_GAIN =", "VAD_SILENCE_MS ="):
            assert constant in copied

    def test_no_sibling_import_remains(self):
        """Every lazy import must point at an ANITA adapter."""
        source = (ANITA / "voice.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders: list[str] = []
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            # level > 0 is a relative import — those are the adapters.
            offenders += [n for n in names if n.split(".")[0] == "app"]
        assert not offenders, f"unconverted sibling imports: {offenders}"

    def test_the_engine_imports(self):
        import anita.voice as voice

        assert hasattr(voice, "VoiceCallSession")

    def test_audio_format_is_carried_verbatim(self):
        """Codec constants are invariants: a changed sample rate produces noise,
        not an error."""
        from anita import audio_format

        assert audio_format.SAMPLE_RATE == 16000
        assert audio_format.CHANNELS == 1
        assert audio_format.SAMPLE_WIDTH == 2
        assert audio_format.CHUNK_FRAMES == 320

    def test_platform_shim_does_not_shadow_the_stdlib(self):
        """The source named its module `app.platform`. Inside this package that
        would shadow Python's stdlib `platform` — the shim is named
        `voice_platform` deliberately."""
        assert (ANITA / "voice_platform.py").exists()
        assert not (ANITA / "platform.py").exists()
        import platform as stdlib_platform

        assert hasattr(stdlib_platform, "machine")


class TestSettingsShim:
    def test_the_shim_exposes_every_attribute_the_engine_reads(self):
        """Built by reading voice.py, so a future edit that needs a new setting
        fails here rather than at the first live call."""
        import re

        from anita.voice_settings import settings

        source = (ANITA / "voice.py").read_text(encoding="utf-8")
        needed = sorted(set(re.findall(r"settings\.([A-Z_][A-Z0-9_]*)", source)))
        assert needed, "no settings reads found — the regex or the file changed"
        missing = [name for name in needed if not hasattr(settings, name)]
        assert not missing, f"voice.py reads settings.{missing} but the shim lacks them"

    def test_anita_values_win_over_carried_defaults(self):
        """The platform owns model and voice; the source's carried constants are
        only for what ANITA has no opinion about."""
        from anita.config import load_settings
        from anita.voice_settings import build_voice_settings

        built = build_voice_settings(env={"ANITA_ADMIN_TOKEN": "x"})
        anita = load_settings(env={"ANITA_ADMIN_TOKEN": "x"}).speech
        assert built.WHISPER_MODEL == anita.whisper_model
        assert built.KOKORO_VOICE == anita.kokoro_voice
        assert built.VAD_SILENCE_MS == anita.vad_silence_ms
        assert built.BARGE_IN_ENABLED == anita.barge_in_enabled

    def test_carried_defaults_are_overridable(self):
        from anita.voice_settings import build_voice_settings

        built = build_voice_settings(env={"ANITA_ADMIN_TOKEN": "x", "STT_MIN_CHARS": "5"})
        assert built.STT_MIN_CHARS == 5


class TestAdapters:
    def test_admission_adapter_backs_onto_the_anita_controller(self):
        from anita import voice_admission
        from anita.speech import AdmissionController

        assert isinstance(voice_admission.REGISTRY, AdmissionController)
        assert voice_admission.REGISTRY.max_concurrent_calls == 2

    def test_the_admission_adapter_offers_no_bundled_audio(self):
        """ANITA ships no pre-synthesised WAV, so the caller must fall back to
        the carrier's own `<Say>` rather than streaming a missing file."""
        from anita.voice_admission import asset_path

        assert asset_path("anything") is None

    def test_platform_adapter_reports_a_whisper_device_config(self):
        """Must unpack exactly as the engine unpacks it.

        This test previously asserted a 3-key dict — the adapter's own shape,
        not the shape the call site consumes. Both sides of the seam were
        tested; the join between them was not, so the ValueError at
        `voice.py:72` was invisible and the broad except that follows turned
        it into a silent fallback to CUDA.
        """
        from anita.voice_platform import get_whisper_device_config

        device, compute_type = get_whisper_device_config()  # must not raise
        assert isinstance(device, str) and device
        assert isinstance(compute_type, str) and compute_type

    def test_load_call_asset_tolerates_a_missing_asset(self):
        """ANITA ships no bundled audio, so asset_path returns None.

        `load_call_asset_ulaw` called `None.is_file()` and crashed. The guard
        must test for absence before touching the path.
        """
        from anita.voice import load_call_asset_ulaw

        assert load_call_asset_ulaw("busy_message") is None

    def test_llm_adapter_reports_streaming_on(self):
        from anita import voice_llm

        assert voice_llm.LLM_STREAM is True
        assert issubclass(voice_llm.GenerationFailed, Exception)

    def test_the_engine_declines_when_the_gate_declines(self):
        """BRD-04: no context, no answer.

        This used to assert against a voice-only retrieval adapter. It is now
        a property of the shared engine, which is the stronger statement: the
        voice path holds it *because* voice runs the same tree as chat, not
        because a second adapter was written carefully.
        """
        from anita.answer_engine import AnswerEngine
        from anita.orchestrator import Intent
        from anita.retrieval import Chunk, GroundingGate, InMemoryStore, Retriever

        chunks = [Chunk(chunk_id="c1", text="X-Ray Cassette specification",
                        source_url="https://www.anitamedicalsystems.com/p-1.html")]
        gate = GroundingGate(threshold=0.99)   # nothing passes on similarity
        retriever = Retriever(chunks, _NullEmbedder(), InMemoryStore(), gate)
        engine = AnswerEngine(retriever=retriever)

        class _Conversation:
            conversation_id = "conv-1"
            turns: list = []

        reply = engine.answer(
            Intent.CATALOGUE, "what is the weather in Mumbai?", _Conversation()
        )
        assert reply.declined
        assert "specification" not in reply.text, (
            "declined retrieval must not leak context"
        )

    def test_the_stream_yields_clauses_of_the_installed_answer(self):
        """The streaming path cuts the composed answer, it does not compose one.

        Regression guard for the ImportError that made every streamed turn
        return ("", []): the module defined only a factory while the engine
        imported a name that did not exist.
        """
        import asyncio

        from anita import voice_pipeline, voice_rag

        original = voice_pipeline._ANSWER
        try:
            voice_pipeline._ANSWER = lambda q, *, session_key: "First clause. Second clause."
            pieces = asyncio.run(_collect_stream(voice_rag.query_rag_stream("q", retrieval_query="q")))
        finally:
            voice_pipeline._ANSWER = original

        joined = " ".join(pieces)
        assert "First clause." in joined
        assert "Second clause." in joined

    def test_every_adapter_module_is_importable(self):
        import importlib

        for name in ("voice_admission", "voice_platform", "voice_llm",
                     "voice_rag", "voice_trace", "voice_pipeline"):
            importlib.import_module(f"anita.{name}")


class TestAdmissionSeam:
    """Tests that join the engine to the assembled platform.

    The defects these cover survived a green suite because every test asserted
    one side of a seam. `REGISTRY` was asserted to be an AdmissionController,
    and the engine was asserted to import — but nothing checked that the
    controller the engine calls is the controller the platform built.
    """

    def test_the_engine_registry_is_the_assembled_controller(self):
        """assembly must install its controller into the engine's seam.

        With two controllers, voice capacity was counted twice: the box would
        admit calls the surfaces had refused, and the ceiling would not hold.
        """
        from anita import assembly, voice_admission

        built = assembly.assemble(require_preflight=False)
        # `built.admin.admission` is the controller the surfaces admit
        # against — the same object the engine must see.
        assert voice_admission.REGISTRY is built.admin.admission, (
            "the engine's REGISTRY is not the assembled controller — voice "
            "capacity is being counted against a controller nothing else knows"
        )

    def test_the_four_outcomes_are_counted_separately(self):
        """US-016 AC-2: served, degraded, failed and refused never collapse."""
        from anita.speech import AdmissionController

        controller = AdmissionController(max_concurrent_calls=1)
        assert controller.outcomes == {
            "served": 0, "degraded": 0, "failed": 0, "refused": 0,
        }

        controller.note_served()
        controller.note_failed()
        controller.note_asset_played()

        # A refusal is counted at admission, which is the only place that
        # decides it: fill the single slot, then ask for a second call.
        controller.admit("call-1")
        assert controller.admit("call-2").admitted is False

        assert controller.outcomes == {
            "served": 1, "degraded": 1, "failed": 1, "refused": 1,
        }
        assert controller.snapshot()["outcomes"] == controller.outcomes

    def test_outcomes_are_reported_as_a_group(self):
        """An operator must not see one outcome and read it as the picture."""
        from anita.speech import AdmissionController

        snapshot = AdmissionController().snapshot()
        assert set(snapshot["outcomes"]) == {"served", "degraded", "failed", "refused"}


async def _collect_stream(agen) -> list[str]:
    return [piece async for piece in agen]


class _NullEmbedder:
    def embed(self, texts):
        return [[0.0] * 8 for _ in texts]

    def embed_query(self, text):
        return [0.0] * 8


class TestTheAudioScaleContract:
    """`_prepare_audio` and `_transcribe` are a pair, and must stay one.

    `_prepare_audio` scales to int16; `_transcribe` divides by 32768 immediately
    before calling faster-whisper. Changing either alone presents the model with
    audio that is 32768x too loud or 32768x too quiet — and both fail as a
    *hallucination*, not as an exception, so nothing raises and the call simply
    answers the wrong thing.

    This guard exists because that mistake was made for real: passing int16
    straight to `model.transcribe()` (skipping `_transcribe`) made one
    synthesised utterance come back as "I love you so much!" instead of "how
    much is the ultrasound gel" — which looked exactly like a product bug and
    was an artefact of the test.
    """

    def test_prepare_audio_returns_int16(self):
        from anita import voice

        session = voice.VoiceCallSession()
        pcm = (b"\x00\x10" * 320)
        prepared = session._prepare_audio(voice.pcm_to_ulaw(pcm))
        assert prepared.dtype.name == "int16", (
            "_prepare_audio now returns "
            f"{prepared.dtype}; _transcribe divides by 32768 and expects int16"
        )

    def test_transcribe_normalises_the_int16_it_is_given(self):
        """The other half of the pair, asserted from the source.

        A behavioural test would need a loaded model; this asserts the
        conversion is still there, which is the thing that can silently vanish.
        """
        import inspect

        from anita import voice

        source = inspect.getsource(voice.VoiceCallSession._transcribe)
        assert "/ 32768.0" in source, (
            "_transcribe no longer normalises int16 to float32; if the scaling "
            "moved into _prepare_audio, update both and this test together"
        )


class TestAC7UnintelligibleLadder:
    """AC-7: three unintelligible utterances, then a human, then the call ends.

    Before this, the ladder repeated its last rung forever: a caller who could
    not be heard stayed on the line indefinitely, holding one of the two
    admission slots, and the human-transfer offer was never reached because the
    rung that should have carried it offered WhatsApp instead — a channel this
    product defers (UC-02).
    """

    def _session(self):
        from anita import voice

        return voice.VoiceCallSession()

    def test_the_ladder_has_three_rungs(self):
        from anita import voice

        assert len(voice._NOISE_LADDER) == 3, "AC-7 puts the human offer third"

    def test_the_first_two_misses_do_not_end_the_call(self):
        session = self._session()
        for expected in ("hear", "line"):
            reply, end_call = session._noise_reply()
            assert isinstance(reply, str) and reply
            assert end_call is False, "a caller gets three attempts"

    def test_the_third_miss_offers_a_human_and_ends_the_call(self):
        session = self._session()
        session._noise_reply()
        session._noise_reply()
        reply, end_call = session._noise_reply()

        assert end_call is True, "AC-7 requires the call to close cleanly"
        lowered = reply.lower()
        assert "call you back" in lowered or "someone from our team" in lowered

    def test_every_rung_is_a_different_sentence(self):
        """The defect the ladder exists to prevent: never the identical line
        twice in a row."""
        session = self._session()
        replies = [session._noise_reply()[0] for _ in range(3)]
        assert len(set(replies)) == 3

    def test_the_ladder_does_not_run_past_its_last_rung(self):
        session = self._session()
        for _ in range(10):
            reply, end_call = session._noise_reply()
        assert reply  # no IndexError, and still speakable
        assert end_call is True

    def test_no_rung_promises_a_deferred_channel(self):
        """UC-02 defers inbound WhatsApp; the assistant must not offer it."""
        from anita import voice

        for rung in voice._NOISE_LADDER:
            assert "whatsapp" not in rung.lower()

    def test_real_speech_resets_the_ladder(self):
        """A caller who is heard again is not one step from being cut off."""
        session = self._session()
        session._noise_reply()
        session._noise_reply()
        session._noise_streak = 0  # what process_utterance does on real speech
        _, end_call = session._noise_reply()
        assert end_call is False


class TestTAC6CallCeiling:
    """TAC-6: a call that never reaches end-of-speech is capped and closed."""

    def _session(self, ceiling=1800):
        from anita import voice

        session = voice.VoiceCallSession()
        session._max_call_seconds = ceiling
        return session

    def test_a_fresh_call_has_not_expired(self):
        assert self._session(ceiling=1800).call_expired() is False

    def test_the_ceiling_is_configurable(self):
        from anita.config import SpeechSettings

        assert SpeechSettings.max_call_seconds == 1800

    def test_a_call_past_its_ceiling_has_expired(self):
        session = self._session(ceiling=1800)
        session._call_started -= 1801
        assert session.call_expired() is True

    def test_the_ceiling_is_announced_once(self):
        """Otherwise every subsequent frame would re-trigger the closing line."""
        session = self._session(ceiling=1800)
        session._call_started -= 1801
        assert session.call_expired() is True
        session.mark_capped()
        assert session.call_expired() is False

    def test_a_capped_call_closes_with_the_closing_line(self):
        """Driven through `process_utterance`, so this is the real path.

        The speak step is stubbed: synthesis needs Kokoro, and what is under
        test is the decision to close, not the audio.
        """
        import asyncio

        import numpy as np

        session = self._session(ceiling=1800)
        session._call_started -= 1801

        async def fake_synthesise(text):
            # What Kokoro returns: float32 PCM at 24 kHz. `_pcm_to_ulaw_chunks`
            # resamples and chunks it, so the shape matters even in a stub.
            return np.zeros(24000, dtype=np.float32)

        session._synthesise = fake_synthesise

        chunks, dialogue, end_call = asyncio.run(session.process_utterance())

        assert end_call is True, "a capped call must end"
        assert "goodbye" in dialogue.lower()
        assert chunks, "the caller hears why before the line closes"

    def test_the_cap_fires_even_with_no_audio_to_process(self):
        """The case the ceiling exists for: end-of-speech never succeeds, so
        there is by definition nothing buffered."""
        import asyncio

        session = self._session(ceiling=1800)
        session._call_started -= 1801
        assert not session._audio_buffer

        async def fake_synthesise(text):
            return None  # synthesis also unavailable

        session._synthesise = fake_synthesise
        chunks, dialogue, end_call = asyncio.run(session.process_utterance())

        assert end_call is True, "a silent capped call must still close"
        assert chunks == [], "no audio, but the call ends"

    def test_an_uncapped_call_is_untouched(self):
        import asyncio

        session = self._session(ceiling=1800)
        chunks, dialogue, end_call = asyncio.run(session.process_utterance())
        assert (chunks, dialogue, end_call) == ([], "", False)
