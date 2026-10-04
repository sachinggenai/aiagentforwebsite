"""Adapter: per-turn tracing for the voice loop.

Backed by MOD-08's :class:`~anita.observability.TraceWriter`, which is
best-effort by construction — a trace failure must never drop a call.

The engine calls `new_trace(call_id, turn_no)` **positionally** and then marks
stages on the returned object. Until US-025 this helper accepted `**kwargs`
only, so that call raised `TypeError` into a broad `except` and `self._trace`
was `None` on every turn: the engine was traced in tests and nowhere else.
"""

from __future__ import annotations

from .observability import TraceWriter, TurnTrace

#: Default writer: in-memory, so a session that was never installed (a test, a
#: script) still gets a working trace object rather than an AttributeError.
_WRITER = TraceWriter()


def install(writer: TraceWriter) -> TraceWriter:
    """Point the adapter at the assembled writer. Called once at boot,
    alongside the other seams (`voice_admission.install` is the same shape)."""
    global _WRITER
    _WRITER = writer
    return writer


def writer() -> TraceWriter:
    return _WRITER


def mask_call_id(call_id: str) -> str:
    """Call ids are masked wherever they are written (TAC-8 / REC-14)."""
    if not call_id:
        return ""
    return f"…{call_id[-4:]}" if len(call_id) > 8 else "…"


def new_trace(call_id: str = "", turn_no: int = 0) -> TurnTrace:
    """Open a trace for one turn. Positional, because the engine calls it so."""
    return _WRITER.start(
        conversation_id=mask_call_id(str(call_id)),
        channel="voice",
        turn_no=int(turn_no or 0),
    )


def emit_trace(
    trace: TurnTrace | None, *, outcome: str = "answered", admission_refused: bool = False
) -> bool:
    """Write one turn's trace. Never raises, never blocks the call."""
    if trace is None:
        return False
    try:
        trace.outcome = outcome
        trace.admission_refused = admission_refused
        return _WRITER.emit(trace)
    except Exception:  # noqa: BLE001 - telemetry must never fail a call
        return False
