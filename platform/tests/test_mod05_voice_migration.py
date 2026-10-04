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
        from anita.voice_platform import get_whisper_device_config

        config = get_whisper_device_config()
        assert set(config) >= {"device", "compute_type", "cpu_threads"}

    def test_llm_adapter_reports_streaming_on(self):
        from anita import voice_llm

        assert voice_llm.LLM_STREAM is True
        assert issubclass(voice_llm.GenerationFailed, Exception)

    def test_rag_adapter_produces_no_text_when_the_gate_declines(self):
        """BRD-04 holds inside the voice path too: no context, no answer."""
        import asyncio

        from anita.retrieval import Chunk, GroundingGate, InMemoryStore, Retriever
        from anita.voice_rag import make_query_rag_stream

        chunks = [Chunk(chunk_id="c1", text="X-Ray Cassette specification",
                        source_url="https://www.anitamedicalsystems.com/p-1.html")]
        gate = GroundingGate(threshold=0.99)   # nothing passes on similarity
        retriever = Retriever(chunks, _NullEmbedder(), InMemoryStore(), gate)
        stream = make_query_rag_stream(retriever)

        async def collect():
            return [piece async for piece in stream("what is the weather in Mumbai?")]

        pieces = asyncio.run(collect())
        joined = " ".join(pieces).lower()
        assert "don't have that" in joined
        assert "specification" not in joined, "declined retrieval must not leak context"

    def test_every_adapter_module_is_importable(self):
        import importlib

        for name in ("voice_admission", "voice_platform", "voice_llm",
                     "voice_rag", "voice_trace", "voice_pipeline"):
            importlib.import_module(f"anita.{name}")


class _NullEmbedder:
    def embed(self, texts):
        return [[0.0] * 8 for _ in texts]

    def embed_query(self, text):
        return [0.0] * 8
