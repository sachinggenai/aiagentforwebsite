"""Adapter: the source engine's admission surface, backed by ANITA's controller.

`voice.py` calls three names from the source's `app.admission`. ANITA's
equivalent is :class:`anita.speech.AdmissionController`, which already owns
capacity, the VRAM budget and the degradation ladder. This module maps one to
the other so the copied engine needs no rewrite.
"""

from __future__ import annotations

from pathlib import Path

from .speech import BUSY_MESSAGE, AdmissionController

# The engine imports this name *inside* the functions that use it
# (voice.py:292 `from .voice_admission import REGISTRY`), so it re-reads the
# module attribute on every call. That is what makes `install()` below work
# without rewriting the engine's call sites.
#
# It starts life as a default controller so the module is usable standalone
# (and so tests can import it), but whichever process assembles a platform
# must call `install()` — otherwise voice capacity is counted against a
# controller nothing else knows about, and the box would admit calls the rest
# of the app believes it has refused. See assembly.py.
REGISTRY = AdmissionController()


def install(controller: AdmissionController) -> AdmissionController:
    """Point the engine's REGISTRY at the assembled controller.

    Called once at boot by `assembly.build()`. Idempotent, so a second call
    with the same controller is harmless. Returns the controller installed, so
    a caller can assert the seam took.
    """
    global REGISTRY
    REGISTRY = controller
    return controller

# Pre-synthesised audio the source used when refusing a call. ANITA has none
# yet, so the carrier's own synthesis is used instead (see speech.BUSY_MESSAGE).
FIXED_RESPONSE_ASSET: str | None = None


def asset_path(name: str) -> Path | None:
    """The source returned a bundled WAV path. ANITA ships no bundled audio —
    returning None makes the caller fall back to `<Say>` rather than trying to
    stream a file that does not exist."""
    return None
