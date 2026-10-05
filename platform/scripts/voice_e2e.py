"""Voice end-to-end harness (MOD-05).

Drives the real pipeline stages — STT → answer → TTS — outside a phone call, so
the wiring can be proven before a carrier is involved.

**What this can and cannot prove.** It proves the stages connect, the models
load, and audio survives a full round trip. It does *not* prove a phone call
works: there is no carrier, no media stream, no network jitter, and no
barge-in-under-load. Those need the Twilio path and a real caller.

Run:
    .venv\\Scripts\\python scripts\\voice_e2e.py            # stages only
    .venv\\Scripts\\python scripts\\voice_e2e.py --models   # load real models
"""

from __future__ import annotations

import argparse
import importlib
import sys
import time
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

# The model-stage header contains an arrow and the transcripts contain whatever
# the caller said. On a console whose codepage is not UTF-8 (cp1252 on this
# Windows box) `print` raises UnicodeEncodeError and kills the harness before
# it loads a single model — which is how the first GPU verification run died,
# looking like a model failure when it was a `print`. Same guard as
# `rebuild_index.py` and `carrier_acceptance.py`.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PASS, FAIL, SKIP, INFO = "PASS", "FAIL", "SKIP", "INFO"


def report(status: str, name: str, detail: str = "") -> None:
    print(f"  [{status}] {name}{('  — ' + detail) if detail else ''}")


def check_imports() -> bool:
    print("\n1. Dependencies")
    found = {}
    for module in ("numpy", "faster_whisper", "kokoro_onnx", "onnxruntime"):
        try:
            importlib.import_module(module)
            found[module] = True
            report(PASS, module)
        except Exception as exc:  # noqa: BLE001
            found[module] = False
            report(SKIP, module, f"not installed ({type(exc).__name__}) — model stages will be skipped")
    return all(found.values())


def check_wiring() -> bool:
    print("\n2. Engine and adapters")
    ok = True
    try:
        import anita.voice as voice
        report(PASS, "anita.voice", "VoiceCallSession present" if hasattr(voice, "VoiceCallSession") else "")
    except Exception as exc:  # noqa: BLE001
        report(FAIL, "anita.voice", f"{type(exc).__name__}: {exc}")
        return False

    for name in ("voice_settings", "voice_admission", "voice_platform",
                 "voice_llm", "voice_rag", "voice_pipeline", "voice_trace"):
        try:
            importlib.import_module(f"anita.{name}")
            report(PASS, f"anita.{name}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            report(FAIL, f"anita.{name}", f"{type(exc).__name__}: {exc}")

    from anita.voice_settings import settings
    report(INFO, "settings", f"whisper={settings.WHISPER_MODEL} voice={settings.KOKORO_VOICE} "
                             f"vad={settings.VAD_SILENCE_MS}ms barge_in={settings.BARGE_IN_ENABLED}")
    return ok


def check_answer_path() -> bool:
    """The stage between STT and TTS — the part that must never hallucinate."""
    print("\n3. Answer path (no audio needed)")
    corpus_dir = PLATFORM.parent / "data" / "out"
    if not (corpus_dir / "ams.sqlite").exists():
        report(SKIP, "corpus", "not built — run the scraper in src/ams")
        return False

    from anita.answers import AnitaAnswers, FactKind
    from anita.corpus import Corpus

    corpus = Corpus(corpus_dir)
    answers = AnitaAnswers(corpus)

    product = corpus.get("270694")           # Ultrasound Gel, ₹60
    answer = answers.commercial(FactKind.PRICE, product)
    ok = answer.found and answer.caveat is not None and "60" in answer.text
    report(PASS if ok else FAIL, "deterministic price",
           answer.render(speak=True)[:74])
    return ok


def check_vad() -> bool:
    """End-of-speech timing — the dominant term in the voice latency budget."""
    print("\n4. VAD / end-of-speech")
    from anita.speech import EndOfSpeechTracker

    tracker = EndOfSpeechTracker(silence_ms=600, frame_ms=20)
    tracker.feed(is_speech=True)
    decided = sum(1 for _ in range(40) if tracker.feed(is_speech=False)) > 0
    report(PASS if decided else FAIL, "end-of-speech",
           f"{tracker.frames_needed} silent frames ({tracker.silence_ms} ms)")
    return decided


def check_admission() -> bool:
    print("\n5. Admission and degradation")
    from anita.speech import AdmissionController

    controller = AdmissionController(max_concurrent_calls=2)
    controller.admit("a")
    controller.admit("b")
    refused = not controller.admit("c").admitted
    report(PASS if refused else FAIL, "capacity refusal", "3rd call offered a callback")

    controller.degrade("gpu pressure")
    ok = not controller.voice_available and controller.text_available
    report(PASS if ok else FAIL, "voice sheds before text")
    return refused and ok


def run_model_stages() -> bool:
    """Load the real models and round-trip audio. Requires the dependencies."""
    print("\n6. Model stages (STT → TTS round trip)")
    try:
        import numpy as np  # noqa: F401
        from faster_whisper import WhisperModel
        from kokoro_onnx import Kokoro
    except Exception as exc:  # noqa: BLE001
        report(SKIP, "model round trip", f"dependencies missing ({type(exc).__name__})")
        return False

    from anita.voice_settings import settings

    # The source engine resolves these from the shared HF/pipecat cache. Passing
    # bare filenames made a present model look absent.
    cache = Path.home() / ".cache" / "pipecat" / "kokoro-onnx"
    model_path = cache / "kokoro-v1.0.onnx"
    voices_path = cache / "voices-v1.0.bin"
    if not (model_path.exists() and voices_path.exists()):
        report(SKIP, "TTS model", f"not cached at {cache}")
        return False

    started = time.monotonic()
    try:
        tts = Kokoro(str(model_path), str(voices_path))
        samples, rate = tts.create(
            "The ultrasound gel is listed at sixty rupees per piece.",
            voice=settings.KOKORO_VOICE, speed=settings.KOKORO_SPEED,
        )
        report(PASS, "TTS", f"{len(samples) / rate:.2f}s of audio at {rate} Hz "
                            f"in {time.monotonic() - started:.2f}s")
    except Exception as exc:  # noqa: BLE001
        report(SKIP, "TTS model", f"not available locally ({type(exc).__name__}: {str(exc)[:60]})")
        return False

    # The device the *product* would use, not a hardcoded CPU. It was pinned to
    # "cpu" here, so this harness reported a green STT on a GPU box while the
    # live path was failing at the first transcription with "cublas64_12.dll is
    # not found" — the check agreed with the wrong thing (work-status §9).
    from anita.voice_platform import ensure_ctranslate2_cuda_libs, get_whisper_device_config

    device, compute_type = get_whisper_device_config()
    if device == "cuda":
        ensure_ctranslate2_cuda_libs()

    started = time.monotonic()
    try:
        stt = WhisperModel(settings.WHISPER_MODEL, device=device,
                           compute_type=compute_type)
        segments, _ = stt.transcribe(samples.astype("float32"), language="en", beam_size=1)
        transcript = " ".join(segment.text for segment in segments).strip()
        # The device is named, not implied: "the model loaded" and "the model
        # ran where you think it did" are different claims.
        report(PASS, "STT", f"{time.monotonic() - started:.2f}s on {device}/{compute_type} "
                            f"-> {transcript[:60]!r}")
        # A round trip that loses the price is worse than no round trip.
        ok = "sixty" in transcript.lower() or "60" in transcript
        report(PASS if ok else FAIL, "round trip preserved the figure")
        return ok
    except Exception as exc:  # noqa: BLE001
        # A failure on the configured device is a FAIL, not a SKIP: skipping it
        # is how a GPU box with a broken CUDA link reported green.
        report(FAIL, "STT model", f"{device}/{compute_type} ({type(exc).__name__}: {str(exc)[:60]})")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", action="store_true",
                        help="also load STT/TTS models and round-trip audio")
    args = parser.parse_args()

    print("=" * 84)
    print("VOICE END-TO-END HARNESS")
    print("=" * 84)

    deps = check_imports()
    wiring = check_wiring()
    answer = check_answer_path()
    vad = check_vad()
    admission = check_admission()

    models = False
    if args.models:
        models = run_model_stages()
    else:
        print("\n6. Model stages")
        report(SKIP, "model round trip", "pass --models to load STT/TTS and round-trip audio")

    print("\n" + "=" * 84)
    print("  Wiring and control stages are what this proves. A real call needs a")
    print("  carrier, a media stream and a caller — none of which are present here.")
    print("=" * 84)
    return 0 if (wiring and vad and admission) else 1


if __name__ == "__main__":
    raise SystemExit(main())
