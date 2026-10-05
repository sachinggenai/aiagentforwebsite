"""MOD-05 voice migration — the copied engine and its adapters (REC-01).

`voice.py` is a **copy**, not a rewrite: 1,890 lines of working real-time turn
loop (VAD → STT → RAG → LLM → TTS, barge-in, echo gate) that would be the single
largest cost in this project to rebuild. These tests pin the properties that
make the copy safe to keep.

Run:  .venv\\Scripts\\python -m pytest tests -q
"""

from __future__ import annotations

import ast
import json
import re
import sys
import warnings
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

ANITA = PLATFORM / "app" / "anita"

#: Vendored evidence for the faithful-copy check (CL-11). Written by
#: `scripts/voice_manifest.py`; read here so the check never depends on the
#: sibling repository being on the machine. Under `fixtures/`, not `data/`:
#: `data/` is gitignored as a runtime artefact and this file is source.
MANIFEST_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "voice_engine_manifest.json"
)

_DEFINITION_RE = re.compile(r"\s*(def |class |async def )")


def _definitions(text: str) -> list[str]:
    """The source's function/class signatures, one per line, stripped."""
    return [line.strip() for line in text.splitlines() if _DEFINITION_RE.match(line)]


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


class TestMigrationIntegrity:
    def test_the_manifest_is_present(self):
        """CL-11: the faithful-copy evidence is vendored, and required.

        This check used to read the sibling repo directly and `pytest.skip`
        when it was absent — and a skip reads like a pass, so on a clean
        checkout the engine was unguarded. A missing or empty manifest is now
        a failure, not a skip.
        """
        manifest = _manifest()
        assert manifest["source_definitions"], "the manifest carries no definitions"
        assert len(manifest["source_sha256"]) == 64, "no usable source hash"
        assert manifest["source_line_count"] > 1000, "suspiciously small source"

    def test_the_engine_is_a_faithful_copy(self):
        """The turn loop must not have been edited in transit — a rewritten
        engine would lose the barge-in and echo-gate tuning that was derived
        experimentally against real phone audio.

        The copy is allowed to differ from the source by **exactly** the
        divergences US-001 requires, listed below. Anything else still fails,
        which is the property this test exists for: a deliberate, traceable
        change and an accidental rewrite must not look the same.

        Checked against the vendored manifest (CL-11), so the property holds
        with or without the sibling repository present. When the sibling *is*
        here and has moved on, a warning says the manifest is stale — the
        sibling is a different product and is free to evolve.
        """
        manifest = _manifest()
        source_defs = manifest["source_definitions"]
        copied = (ANITA / "voice.py").read_text(encoding="utf-8")

        copied_defs = _definitions(copied)

        # AC-7 makes the third rung terminal, so `_noise_reply` now reports
        # whether the call should end. TAC-6 adds the call ceiling. Both are
        # requirements the source predates. CL-8 removes the source's
        # `generate_ulaw_greeting`: ANITA greets through TwiML `<Say>` and
        # nothing in this repo ever called the helper (`work-status.md` §10).
        DELIBERATE_GONE = {
            "def _noise_reply(self) -> str:",
            "def generate_ulaw_greeting(text: str) -> list[bytes]:",
        }
        DELIBERATE_NEW = {
            "def _noise_reply(self) -> tuple[str, bool]:",
            "def call_seconds(self) -> float:",
            "def call_expired(self) -> bool:",
            "def mark_capped(self) -> None:",
            "def _max_call_seconds() -> int:",
            # US-025: emits the turn's trace when the turn resolves, whatever
            # the outcome. The source had tracing hooks but no emit path.
            "def finish_trace(self, *, outcome: str = \"answered\") -> None:",
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

        # CL-11 freshness probe: the manifest is the authority for this test;
        # the live source only tells us whether the manifest needs regenerating.
        live_source = Path(manifest["source_path"])
        if live_source.exists():
            live_defs = _definitions(live_source.read_text(encoding="utf-8"))
            if live_defs != source_defs:
                warnings.warn(
                    "the sibling voice engine has changed since the manifest was "
                    "generated — the copy is still checked against the manifest, "
                    "but review the diff and regenerate with "
                    "`python scripts/voice_manifest.py`",
                    UserWarning,
                    stacklevel=1,
                )

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
        # The registry is process-wide, and `assembly` installs the *assembled*
        # controller into it — which US-018 caps on a host without a GPU. So the
        # default is asserted on a fresh instance rather than on whatever the
        # last assembly in this session left behind.
        assert AdmissionController().max_concurrent_calls == 2

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

    def test_the_cuda12_registration_is_a_no_op_without_the_wheels(self):
        """A CPU-only or chat-only deployment installs no `nvidia-*` wheel, and
        the registration must not raise there — it is a GPU convenience, not a
        boot requirement."""
        import sys as _sys

        from anita import voice_platform

        saved = _sys.modules.get("nvidia")
        _sys.modules["nvidia"] = None          # makes `import nvidia` raise ImportError
        try:
            assert voice_platform.ensure_ctranslate2_cuda_libs() == []
        finally:
            if saved is None:
                _sys.modules.pop("nvidia", None)
            else:
                _sys.modules["nvidia"] = saved

    @pytest.mark.skipif(sys.platform != "win32", reason="a Windows DLL-path arrangement")
    def test_it_registers_the_cublas_bin_directory(self, tmp_path, monkeypatch):
        """CTranslate2 wants CUDA 12's cuBLAS while torch ships CUDA 13; the
        fix is to install both and register the 12 — this asserts the
        registration finds the directory the wheel actually ships."""
        import types

        from anita import voice_platform

        bin_dir = tmp_path / "cublas" / "bin"
        bin_dir.mkdir(parents=True)
        fake = types.ModuleType("nvidia")
        fake.__path__ = [str(tmp_path)]  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "nvidia", fake)

        assert str(bin_dir) in voice_platform.ensure_ctranslate2_cuda_libs()

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
            # Accepts the seam's current kwargs (`caller`, `channel_kind` —
            # US-004): the point of this guard is that the streaming path
            # *calls* the installed answer, not the exact arity it uses.
            voice_pipeline._ANSWER = (
                lambda q, *, session_key, caller="", channel_kind="":
                "First clause. Second clause."
            )
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


# ---------------------------------------------------------------------------
# The engine's vocabulary is ANITA's — work-status.md §10, CL-1 / CL-2
# ---------------------------------------------------------------------------

CORPUS_DIR = PLATFORM.parent / "data" / "out"

#: Vocabulary belonging to the sibling product this engine was copied from.
#: These terms are legitimate *elsewhere* — the isolation guards and the
#: corpus gate list them deliberately, so a copy-pasted sibling config is
#: refused at boot — which is why this guard is scoped to the engine's own
#: strings, not the whole tree (CL-10 is the tree-wide net).
SIBLING_TERMS = (
    "meridian", "university", "admissions", "tuition", "mba", "ielts",
    "toefl", "gmat", "campus", "semester", "student", "enrollment",
)


class TestTheEngineSpeaksAnitasVocabulary:
    """CL-1 / CL-2: the copied engine must speak and correct in ANITA's language.

    Both defects this pins were live on every call. The closing line — spoken
    on any polite hang-up — named the sibling product's admissions office, and
    every transcript was run through that product's corrections table
    (`emma`→`MBA`, `intuition`→`tuition`).
    """

    def test_the_closing_line_names_anita(self):
        from anita import voice

        line = voice.CLOSING_REPLY.lower()
        assert "anita" in line
        assert not [t for t in SIBLING_TERMS if t in line], (
            "the closing line names the sibling product again"
        )

    def test_a_sign_off_speaks_the_anita_closing_line(self):
        """Driven through `process_utterance` — the real decision path.

        STT and TTS are stubbed: what is under test is which line the closing
        branch chooses, not the models.
        """
        import asyncio

        import numpy as np

        from anita import voice

        session = voice.VoiceCallSession()
        # Enough non-silent audio to clear the min-burst floor. Staged
        # directly — how the buffer filled is not what this test is about.
        session._audio_buffer = [b"\xff" * 160] * voice.MIN_UTTERANCE_FRAMES
        session._total_frames = voice.MIN_UTTERANCE_FRAMES

        async def fake_transcribe(audio):
            return "bye", False

        async def fake_synthesise(text):
            # What Kokoro returns: float32 PCM at 24 kHz, so the shape matters
            # even in a stub (`_pcm_to_ulaw_chunks` resamples and chunks it).
            return np.zeros(24000, dtype=np.float32)

        session._transcribe = fake_transcribe
        session._synthesise = fake_synthesise

        chunks, dialogue, end_call = asyncio.run(session.process_utterance())

        assert end_call is True, "a sign-off ends the call"
        assert voice.CLOSING_REPLY in dialogue
        assert chunks, "the caller hears the line"
        assert not [t for t in SIBLING_TERMS if t in dialogue.lower()]

    def test_no_correction_carries_sibling_vocabulary(self):
        from anita import voice

        table = " ".join(
            f"{wrong} {right}"
            for wrong, right in voice.VoiceCallSession._CORRECTIONS.items()
        ).lower()
        assert not [t for t in SIBLING_TERMS if t in table], (
            "a sibling product's vocabulary is back in the corrections table"
        )

    def test_every_correction_target_is_catalogue_vocabulary(self):
        """The invariant that keeps CL-2 from rotting.

        Every target must exist in ANITA's own corpus — product names, brands,
        company name. An edit that pastes in another product's words fails
        here, which is exactly how the previous table should have been caught.
        """
        from anita import voice
        from anita.config import load_settings
        from anita.corpus import Corpus

        vocabulary: set[str] = set()
        for product in Corpus(CORPUS_DIR).all_products():
            vocabulary.add(product.name)
            if product.brand:
                vocabulary.add(product.brand)
        vocabulary.add(load_settings(env={}).identity.company_name)
        blob = " | ".join(vocabulary).lower()

        missing = [
            target
            for target in voice.VoiceCallSession._CORRECTIONS.values()
            if target.lower() not in blob
        ]
        assert not missing, (
            f"correction targets are not ANITA catalogue vocabulary: {missing}"
        )

    def test_a_mis_heard_product_name_is_corrected(self):
        from anita import voice

        session = voice.VoiceCallSession()
        corrected = session._post_process_transcript(
            "how much is the ultra sound gel and the ex ray cassette"
        )
        assert "Ultrasound" in corrected
        assert "X-Ray" in corrected
        assert "ultra sound" not in corrected.lower()

    def test_a_correction_does_not_fire_inside_a_longer_word(self):
        """Word boundaries. The previous implementation substituted on
        substrings, so a key rewrote any longer word containing it."""
        from anita import voice

        session = voice.VoiceCallSession()
        assert session._post_process_transcript("annitas") == "annitas"


class TestClarificationAndScrubbingSpeakAnitasVocabulary:
    """CL-3 / CL-4: the loop-breaker's detector and the meta-leak scrubber.

    The clarification pattern decided when the assistant was stuck asking
    admissions questions ("which program?", "fall or spring?") and the scrubber
    dropped sentences containing "university profile". Neither vocabulary
    belongs to this product.
    """

    #: Phrasings the loop-breaker must count — an assistant turn that asks the
    #: caller to pin down a product or an order.
    ANITA_CLARIFICATIONS = (
        "Which specific product are you asking about?",
        "Which one do you mean?",
        "Could you specify the model?",
        "Did you mean the APO 100 or the APO 200?",
        "What quantity do you need?",
        "Just to confirm, are you looking for the cassette or the film?",
    )

    #: Admissions phrasings the detector must no longer recognise. They avoid
    #: the domain-neutral lead-ins ("did you mean", "could you specify"), which
    #: are deliberately kept — what changed is the domain vocabulary.
    ADMISSIONS_PHRASINGS = (
        "Which intake are you applying for?",
        "What programs does the university offer?",
        "Is the fall semester still open?",
        "Do you offer a course in radiology?",
        "When does the new term start?",
    )

    def test_an_anita_clarification_reads_as_one(self):
        from anita import voice

        missed = [q for q in self.ANITA_CLARIFICATIONS if not voice._is_clarification(q)]
        assert not missed, f"clarification questions the loop-breaker would miss: {missed}"

    def test_admissions_phrasing_no_longer_reads_as_a_clarification(self):
        from anita import voice

        false = [q for q in self.ADMISSIONS_PHRASINGS if voice._is_clarification(q)]
        assert not false, f"admissions phrasings still counted as clarifications: {false}"

    def test_the_clarification_pattern_carries_no_admissions_vocabulary(self):
        from anita import voice

        pattern = voice._CLARIFICATION_RE.pattern.lower()
        admissions = ("program", "course", "intake", "semester", "term", "fall")
        found = [word for word in admissions if word in pattern]
        assert not found, f"admissions vocabulary is back in the pattern: {found}"

    def test_the_scrubber_strips_the_meta_prefix_and_keeps_the_answer(self):
        from anita import voice

        scrubbed = voice.scrub_meta_leak(
            "Here's the relevant information from the catalogue profile: "
            "Ultrasound Gel is listed at ₹60/Piece."
        )
        assert scrubbed == "Ultrasound Gel is listed at ₹60/Piece", (
            "the meta lead-in must be stripped while the real content survives"
        )

    def test_the_scrubber_drops_a_pure_meta_sentence(self):
        from anita import voice

        assert voice.scrub_meta_leak("Based on the catalogue, I will answer.") is None

    def test_clean_text_passes_through_byte_identical(self):
        from anita import voice

        clean = "Ultrasound Gel is listed at ₹60/Piece."
        assert voice.scrub_meta_leak(clean) is clean

    def test_the_scrubber_carries_no_sibling_vocabulary(self):
        from anita import voice

        blob = (" ".join(voice._META_MARKERS) + " " + voice._PREFIX_STRIP.pattern).lower()
        assert not [t for t in SIBLING_TERMS if t in blob], (
            "a sibling product's vocabulary is back in the scrubber"
        )


class TestTheVoicePromptNamesTheCatalogue:
    """CL-5: the prompt builder assembled an admissions prompt on every turn.

    It is inert today only because `voice_pipeline.run_rag_query_sync` answers
    the *question*, not the prompt — US-014 wires the prose leg, at which point
    every one of these sentences reaches a model.
    """

    def _session_with(self, turns):
        from anita import voice

        session = voice.VoiceCallSession()
        session._conversation_history = list(turns)
        return session

    def test_the_prompt_names_the_catalogue(self):
        session = self._session_with(
            ["Caller: hello", "Assistant: Hello! How can I help?"]
        )
        prompt = session._build_llm_prompt("how much is the ultrasound gel")
        assert "catalogue" in prompt.lower()
        assert "ultrasound gel" in prompt.lower()
        assert not [t for t in SIBLING_TERMS if t in prompt.lower()]

    def test_the_loop_breaker_prompt_names_the_catalogue(self):
        """Two clarification turns in a row is the branch that rebuilds the
        prompt with caller-only history — the one US-014 will drive hardest."""
        session = self._session_with(
            [
                "Caller: I need a gel",
                "Assistant: Which specific product do you mean?",
                "Caller: the gel",
                "Assistant: Could you specify the model?",
            ]
        )
        prompt = session._build_llm_prompt("the gel")
        assert "Do NOT ask any question" in prompt
        assert "catalogue" in prompt.lower()
        assert not [t for t in SIBLING_TERMS if t in prompt.lower()]


class TestTheEngineLogsUnderItsOwnName:
    """CL-6: the engine logged as `voice_handler` — the source module's name."""

    def test_the_loggers_are_named_under_anita(self):
        from anita import voice

        assert voice.logger.name == "anita.voice"
        assert voice.voice_events.name == "anita.voice.events"

    def test_no_logger_is_named_after_the_source_module(self):
        source = (ANITA / "voice.py").read_text(encoding="utf-8")
        assert 'getLogger("voice_handler")' not in source
        assert 'getLogger("voice_events")' not in source


# ---------------------------------------------------------------------------
# US-017 — immediate barge-in stop
# ---------------------------------------------------------------------------
class TestUS017BargeInStopsTheAnswer:
    """An interruption stops the *remaining* clauses, not just the current one.

    The audit reproduced the defect: interrupting during clause 1 still
    synthesised and sent all four — the cancel flag was read only before the
    first clause and after the last, so barge-in stopped the sentence the
    caller had already heard and nothing else.
    """

    CLAUSES = ("First clause.", "Second clause.", "Third clause.")

    def _session(self):
        from anita import voice

        return voice.VoiceCallSession()

    def _stub_stream(self, monkeypatch, clauses=None):
        from anita import voice_rag

        async def fake_stream(prompt, cancel=None, retrieval_query=None,
                              session_key="", **kwargs):
            for clause in (clauses or self.CLAUSES):
                yield clause

        monkeypatch.setattr(voice_rag, "query_rag_stream", fake_stream)

    def _stub_tts(self, session, spoken, interrupt_after=None):
        async def fake_synthesise_streamed(text, sink):
            spoken.append(text)
            frames = [b"\xff" * 160]
            if sink is not None:
                await sink(frames)
            if interrupt_after is not None and len(spoken) >= interrupt_after:
                session.request_interruption()
            return frames

        session._synthesise_streamed = fake_synthesise_streamed

    def test_an_interruption_stops_the_remaining_clauses(self, monkeypatch):
        import asyncio

        self._stub_stream(monkeypatch)
        session = self._session()
        session.begin_turn()
        spoken: list[str] = []
        self._stub_tts(session, spoken, interrupt_after=1)

        async def sink(frames):
            pass

        answer, _chunks = asyncio.run(
            session._query_llm_streamed("how much is the gel", sink, cancel=session._cancel)
        )

        assert spoken == ["First clause."], (
            f"the caller interrupted after clause 1 but heard {spoken}"
        )
        assert answer == "First clause.", (
            "the history must hold only what was actually spoken"
        )

    def test_an_uninterrupted_stream_speaks_every_clause(self, monkeypatch):
        import asyncio

        self._stub_stream(monkeypatch)
        session = self._session()
        session.begin_turn()
        spoken: list[str] = []
        self._stub_tts(session, spoken, interrupt_after=None)

        async def sink(frames):
            pass

        answer, _chunks = asyncio.run(
            session._query_llm_streamed("how much is the gel", sink, cancel=session._cancel)
        )

        assert spoken == list(self.CLAUSES)
        assert answer == " ".join(self.CLAUSES)

    def test_synthesis_stops_mid_clause(self):
        """The caller cuts in while a clause is still being synthesised: the
        remaining audio of that clause is not sent either."""
        import asyncio

        import numpy as np

        session = self._session()
        session.begin_turn()

        async def fake_stream(text, *, voice=None, speed=None):
            for _ in range(3):
                yield np.zeros(2400, dtype=np.float32), 24000

        session.synthesise_stream = fake_stream
        sent: list[list[bytes]] = []

        async def sink(frames):
            sent.append(frames)
            session.request_interruption()

        chunks = asyncio.run(session._synthesise_streamed("A long clause.", sink))

        assert len(sent) == 1, f"kept sending audio after the interruption: {len(sent)} batches"
        assert chunks == sent[0]

    def test_no_interruption_is_unchanged(self):
        """The guard: with no cancel, the synthesis loop sends everything."""
        import asyncio

        import numpy as np

        session = self._session()
        session.begin_turn()

        async def fake_stream(text, *, voice=None, speed=None):
            for _ in range(3):
                yield np.zeros(2400, dtype=np.float32), 24000

        session.synthesise_stream = fake_stream
        sent: list[list[bytes]] = []

        async def sink(frames):
            sent.append(frames)

        chunks = asyncio.run(session._synthesise_streamed("A long clause.", sink))
        assert len(sent) == 3
        assert chunks is not None and len(chunks) == sum(len(batch) for batch in sent)

    def test_a_failed_clause_is_retried_once_then_offers_a_human(self):
        """AC-4: one retry, then the human-transfer offer — never silence."""
        import asyncio

        session = self._session()
        session.begin_turn()
        calls: list[str] = []

        async def always_fails(text, sink):
            calls.append(text)
            return None

        session._synthesise_streamed = always_fails

        async def fake_stream(prompt, cancel=None, retrieval_query=None,
                              session_key="", **kwargs):
            yield "First clause."

        import anita.voice_rag as voice_rag
        original = voice_rag.query_rag_stream
        voice_rag.query_rag_stream = fake_stream
        try:
            async def sink(frames):
                pass

            answer, chunks = asyncio.run(
                session._query_llm_streamed("a question", sink, cancel=session._cancel)
            )
        finally:
            voice_rag.query_rag_stream = original

        from anita import voice as voice_module

        assert calls == ["First clause.", "First clause.", voice_module.HUMAN_TRANSFER_REPLY], calls
        assert chunks == [], "no audio could be produced, but the offer was attempted"

    def test_a_clause_that_fails_once_recovers_on_the_retry(self):
        import asyncio

        session = self._session()
        session.begin_turn()
        attempts: list[int] = []

        async def fails_then_works(text, sink):
            attempts.append(1)
            if len(attempts) == 1:
                return None
            frames = [b"\xff" * 160]
            if sink is not None:
                await sink(frames)
            return frames

        session._synthesise_streamed = fails_then_works

        async def fake_stream(prompt, cancel=None, retrieval_query=None,
                              session_key="", **kwargs):
            yield "First clause."

        import anita.voice_rag as voice_rag
        original = voice_rag.query_rag_stream
        voice_rag.query_rag_stream = fake_stream
        try:
            async def sink(frames):
                pass

            answer, chunks = asyncio.run(
                session._query_llm_streamed("a question", sink, cancel=session._cancel)
            )
        finally:
            voice_rag.query_rag_stream = original

        assert len(attempts) == 2, "one failure, one retry"
        assert chunks, "the retry's audio is kept"


class TestUS017TruncationMarker:
    """AC-2: the transcript records that the answer was cut short — on the
    batch path only, where the history holds the full composed answer."""

    def test_the_batch_path_marks_the_answer(self):
        from anita import voice

        session = voice.VoiceCallSession()
        session._spoke_streamed = False
        session._conversation_history = ["Caller: hello", "Assistant: The price is 60."]

        session.note_answer_truncated()

        assert session._conversation_history[-1].endswith(voice.TRUNCATION_MARK)

    def test_marking_is_idempotent(self):
        from anita import voice

        session = voice.VoiceCallSession()
        session._conversation_history = ["Assistant: The price is 60."]
        session.note_answer_truncated()
        once = session._conversation_history[-1]
        session.note_answer_truncated()
        assert session._conversation_history[-1] == once

    def test_the_streamed_path_is_not_marked(self):
        """The streamed history already holds only what the caller heard."""
        from anita import voice

        session = voice.VoiceCallSession()
        session._spoke_streamed = True
        session._conversation_history = ["Assistant: The price is 60."]

        session.note_answer_truncated()

        assert not session._conversation_history[-1].endswith(voice.TRUNCATION_MARK)

    def test_a_non_answer_entry_is_untouched(self):
        from anita import voice

        session = voice.VoiceCallSession()
        session._conversation_history = ["Caller: hello"]
        session.note_answer_truncated()
        assert session._conversation_history[-1] == "Caller: hello"
