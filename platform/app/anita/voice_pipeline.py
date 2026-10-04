"""Adapter: the voice loop's answer path, backed by the shared AnswerEngine.

The source's `run_rag_query_sync` went to an LLM with retrieved context. ANITA
answers deterministic-first (`answer_engine.AnswerEngine`), and the voice path
now runs **that same tree** rather than a second composition of its own — so a
caller and a web buyer asking the same question get the same answer, and the
frozen evaluation set covers both.

That is also what keeps `BRD-05` true on the voice path: prices and
specifications come from the deterministic engine, where the caveat is appended
by the same call that produces the figure, rather than from generated text.

One seam, mirroring `voice_admission.install`: `voice.py` imports
`run_rag_query_sync` *inside* the method that uses it (voice.py:1413), so it
re-reads this module attribute on every turn and `install()` takes effect
without rewriting the engine's call sites.
"""

from __future__ import annotations

from typing import Callable

# Installed once at boot by `assembly.build()`.
_ANSWER: Callable[..., str] | None = None


def install(orchestrator, engine) -> None:
    """Point the voice loop at the assembled orchestrator and answer engine.

    The orchestrator is needed, not just the engine: US-001 AC-1 requires the
    call to be *recorded as a conversation* with channel `voice_inbound` and
    linked to a lead. Routing the utterance through `begin_turn` is what creates
    that conversation and classifies the intent the engine's tree branches on.
    """
    global _ANSWER

    def _answer(question: str, *, session_key: str) -> str:
        from .channels import Channel, InboundEnvelope

        envelope = InboundEnvelope(
            channel=Channel.VOICE_INBOUND, text=question, session_id=session_key
        )
        conversation, routing = orchestrator.begin_turn(envelope)
        reply = engine.answer(routing.intent, question, conversation)
        orchestrator.complete_turn(conversation, reply.text, failed=reply.unavailable)
        return reply.text

    _ANSWER = _answer


def run_rag_query_sync(
    prompt: str,
    channel: str = "voice",
    question: str | None = None,
    session_key: str = "",
    **kwargs,
) -> str:
    """Answer one spoken turn.

    ``voice.py:1429`` calls this with three positionals — `(prompt, "voice",
    question)` — while the previous factory returned a one-argument callable,
    so the call raised TypeError into a broad `except` and every turn returned
    the empty string. Both the arity and the answer are fixed here.

    The **question**, not the prompt, is what gets answered. The prompt is the
    assembled instruction text (history plus style rules); retrieval on it
    embeds the instructions and dilutes the actual ask, which is the `C3/N1`
    finding the call site records.
    """
    if _ANSWER is None:
        # Never silently answer "" — that reads as a failed turn and hides the
        # real cause. `assembly.build()` calls `install()`; reaching here means
        # the voice path was driven without a platform assembled.
        raise RuntimeError(
            "voice answer engine is not installed — assembly.build() must call "
            "voice_pipeline.install() before a call can be answered"
        )
    return _ANSWER(question or prompt, session_key=session_key or "voice")
