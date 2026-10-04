"""Adapter: Whisper device selection from ANITA's platform detection.

Deliberately NOT named `platform.py`: a module of that name shadows Python's
stdlib `platform` inside this package, and the source platform's copy is a
hazard worth not repeating.
"""

from __future__ import annotations

from .platform_detect import detect_platform


def get_whisper_device_config() -> dict:
    """The shape `voice.py` expects: device, compute_type, cpu_threads."""
    device = detect_platform()
    return {
        "device": device.device,
        "compute_type": device.compute_type,
        "cpu_threads": 4,
    }
