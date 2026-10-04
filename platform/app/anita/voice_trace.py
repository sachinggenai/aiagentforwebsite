"""Adapter: per-turn tracing for the voice loop.

Backed by MOD-08's :class:`~anita.observability.TraceWriter`, which is
best-effort by construction — a trace failure must never drop a call.
"""

from __future__ import annotations

from .observability import TraceWriter

_WRITER = TraceWriter()


def new_trace(**kwargs):
    """Return a started trace, matching the source's `new_trace` helper."""
    return _WRITER.start(**kwargs)


def writer() -> TraceWriter:
    return _WRITER
