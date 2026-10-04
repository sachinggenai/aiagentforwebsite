"""Adapter: the source engine's admission surface, backed by ANITA's controller.

`voice.py` calls three names from the source's `app.admission`. ANITA's
equivalent is :class:`anita.speech.AdmissionController`, which already owns
capacity, the VRAM budget and the degradation ladder. This module maps one to
the other so the copied engine needs no rewrite.
"""

from __future__ import annotations

from pathlib import Path

from .speech import BUSY_MESSAGE, AdmissionController

# One controller per process, as the source had one REGISTRY per process.
REGISTRY = AdmissionController()

# Pre-synthesised audio the source used when refusing a call. ANITA has none
# yet, so the carrier's own synthesis is used instead (see speech.BUSY_MESSAGE).
FIXED_RESPONSE_ASSET: str | None = None


def asset_path(name: str) -> Path | None:
    """The source returned a bundled WAV path. ANITA ships no bundled audio —
    returning None makes the caller fall back to `<Say>` rather than trying to
    stream a file that does not exist."""
    return None
