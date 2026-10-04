"""Adapter: the source engine's LLM surface, backed by ANITA's backends."""

from __future__ import annotations

from .llm import (
    FIRST_TOKEN_TIMEOUT_S,
    ClauseCutter,
    LLMError,
)

# The source gated streaming behind an env flag. ANITA streams by default —
# first-audio latency dominates perceived responsiveness in voice.
LLM_STREAM = True


class GenerationCancelled(Exception):
    """The caller interrupted; the stream should stop."""


class GenerationFailed(LLMError):
    """Generation could not complete."""
