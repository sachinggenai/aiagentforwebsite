"""Adapter: Whisper device selection from ANITA's platform detection.

Deliberately NOT named `platform.py`: a module of that name shadows Python's
stdlib `platform` inside this package, and the source platform's copy is a
hazard worth not repeating.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from .platform_detect import detect_platform

logger = logging.getLogger("anita.voice_platform")


def ensure_ctranslate2_cuda_libs() -> list[str]:
    """Make CUDA 12's cuBLAS findable, because CTranslate2 links it and torch does not ship it.

    The two GPU runtimes on this box want different CUDA majors:

    * **ONNX Runtime** (Kokoro TTS) asks for cuDNN 9 + **CUDA 13**, which is what
      torch's cu130 wheel bundles — and `voice._get_tts_engine()` preloads those.
    * **CTranslate2** (faster-whisper) is built against **CUDA 12** and wants
      `cublas64_12.dll`. Torch cu130 ships `cublas64_13.dll` and nothing else.

    Worse, importing torch makes CTranslate2 *skip* its own DLL loading (its
    message: "Skip loading CUDA and cuDNN DLLs since torch is imported"), so it
    goes looking for a library that is nowhere on the path.

    The failure is deferred, which is what made it expensive to find: the model
    constructor succeeds and `ready on cuda` is logged; the `RuntimeError` only
    fires on the **first transcription**, i.e. on a live call's first utterance.
    Measured 2026-10-04 — see `docs/sdlc/work-status.md` §9.

    The fix is to install CUDA 12's cuBLAS next to torch rather than instead of
    it (`pip install nvidia-cublas-cu12`) and register its directory, so each
    runtime gets the major it was built against. Returns the directories added,
    so a caller can log what actually happened rather than what was intended.
    """
    if sys.platform != "win32":
        # The nvidia-*-cu12 wheels this registers are a Windows arrangement;
        # Linux gets the same effect from the wheels' own RPATH.
        return []

    added: list[str] = []
    try:
        import nvidia  # type: ignore  # namespace package from nvidia-cublas-cu12
    except ImportError:
        return []

    for package in ("cublas", "cudnn"):
        bin_dir = Path(nvidia.__path__[0]) / package / "bin"
        if not bin_dir.is_dir():
            continue
        # BOTH mechanisms, because the two consumers resolve DLLs differently:
        #
        #   * `os.add_dll_directory` is what CT2's *Python* layer and ctypes use
        #     (LOAD_LIBRARY_SEARCH_USER_DIRS). Measured: with this alone,
        #     `ctypes.WinDLL("cublas64_12.dll")` loads fine.
        #   * CTranslate2's *C++* loader goes through the classic search order,
        #     which consults PATH — so with the directory merely added, the
        #     transcription still died with "cublas64_12.dll is not found or
        #     cannot be loaded". Adding it to PATH is what made the difference:
        #     the same call went from RuntimeError to RTF 0.003.
        try:
            os.add_dll_directory(str(bin_dir))
        except (AttributeError, OSError) as exc:  # pragma: no cover - platform-specific
            logger.warning("could not register %s for CTranslate2 (%s)", bin_dir, exc)
        current = os.environ.get("PATH", "")
        if str(bin_dir) not in current.split(os.pathsep):
            os.environ["PATH"] = str(bin_dir) + os.pathsep + current
        added.append(str(bin_dir))
    return added


def get_whisper_device_config() -> tuple[str, str]:
    """Return ``(device, compute_type)`` — the shape the engine unpacks.

    The source returned this same 2-tuple, and `voice.py` still unpacks two
    names:

        device, compute_type = get_whisper_device_config()   # voice.py:72

    This previously returned a 3-key dict, so the unpack raised ValueError.
    The call site catches broadly (voice.py:73-75) and falls back to
    `device = "cuda" if settings.CUDA_VISIBLE_DEVICES else "cpu"` — and
    CUDA_VISIBLE_DEVICES defaults to "0", so the fallback is *always* "cuda".
    The failure was therefore silent, and every machine without a GPU would
    have tried to load Whisper on CUDA.

    `cpu_threads` is deliberately not returned: the engine takes its thread
    count from `settings.WHISPER_NUM_THREADS` and passes it separately
    (voice.py:85), so a third field here would be read by nobody.
    """
    device = detect_platform()
    return device.device, device.compute_type
