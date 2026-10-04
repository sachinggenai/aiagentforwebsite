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
        experimentally against real phone audio."""
        source = Path("D:/project/universityDemo/app/voice_handler.py")
        if not source.exists():
            pytest.skip("source repo not available")
        copied = (ANITA / "voice.py").read_text(encoding="utf-8")
        original = source.read_text(encoding="utf-8")

        # Only the import lines should differ.
        # Compare the *definitions*, not byte equality: prose legitimately
        # names the source module, and the import lines were retargeted.
        def definitions(text: str) -> list[str]:
            return [
                line.strip() for line in text.splitlines()
                if re.match(r"\s*(def |class |async def )", line)
            ]

        assert definitions(copied) == definitions(original), "the engine was altered in transit"
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
