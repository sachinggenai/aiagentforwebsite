"""Adapter: Whisper device selection from ANITA's platform detection.

Deliberately NOT named `platform.py`: a module of that name shadows Python's
stdlib `platform` inside this package, and the source platform's copy is a
hazard worth not repeating.
"""

from __future__ import annotations

from .platform_detect import detect_platform


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
