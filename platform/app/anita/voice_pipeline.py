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

import logging
from typing import Callable

logger = logging.getLogger("anita.voice_pipeline")

# Installed once at boot by `assembly.build()`.
_ANSWER: Callable[..., str] | None = None
# US-006 TAC-5: teardown summarises the conversation. A seam rather than a
# direct import because the relays (media_stream, browser_voice) must not
# depend on the orchestrator — they move bytes, they do not own the thread.
_FINISH: Callable[..., None] | None = None


def install(orchestrator, engine) -> None:
    """Point the voice loop at the assembled orchestrator and answer engine.

    The orchestrator is needed, not just the engine: US-001 AC-1 requires the
    call to be *recorded as a conversation* with channel `voice_inbound` and
    linked to a lead. Routing the utterance through `begin_turn` is what creates
    that conversation and classifies the intent the engine's tree branches on.
    """
    global _ANSWER, _FINISH

    def _answer(question: str, *, session_key: str, caller: str = "",
                channel_kind: str = "voice_inbound") -> str:
        from .channels import Channel, InboundEnvelope
        from .orchestrator import QueueFull

        # US-004: the caller's number, when the carrier gave us one. Without
        # this the voice path could never recognise a buyer who had already
        # talked to the web chat — the number was read off the stream and then
        # dropped on the floor.
        channels = {
            "voice_inbound": Channel.VOICE_INBOUND,
            "browser_mic": Channel.BROWSER_MIC,
        }
        envelope = InboundEnvelope(
            channel=channels.get(channel_kind, Channel.VOICE_INBOUND),
            text=question,
            identity=caller or None,
            session_id=session_key,
        )

        # §8.25: the turn runs under the conversation's single slot, the same
        # way the chat surface runs it (`surfaces.ChatSurface.post_message`).
        # The voice leg used to call `begin_turn` directly, so a conversation
        # that a buyer had also touched over web chat could interleave its
        # turns — the defect US-006's queue exists to kill, fixed on the chat
        # side and missed on this one.
        conversation = orchestrator.registry.resolve(envelope)

        def run_turn() -> str:
            record, routing = orchestrator.begin_turn(envelope)
            reply = engine.answer(routing.intent, question, record)
            orchestrator.complete_turn(record, reply.text, failed=reply.unavailable)
            return reply.text

        try:
            return orchestrator.submit_turn(conversation.conversation_id, run_turn)
        except QueueFull:
            # A caller on a live line cannot be re-prompted the way the chat
            # surface re-prompts with a 429: the alternative to answering is
            # silence on a phone call. Depth is 20 turns waiting on ONE
            # conversation, so this is pathological, and answering inline is
            # the least-bad reading — and it is logged, never silent.
            logger.warning(
                "voice: conversation queue full; answering inline (conversation %s)",
                conversation.conversation_id[:8],
            )
            return run_turn()

    def _finish(session_key: str, reason: str = "teardown") -> None:
        conversation = (
            orchestrator.registry.get(session_key)
            or orchestrator.registry.recover(session_key)
        )
        if conversation is not None:
            orchestrator.finalise(conversation, reason=reason)

    _ANSWER = _answer
    _FINISH = _finish


def finish_call(session_key: str, reason: str = "teardown") -> None:
    """Summarise the call's conversation on teardown. Never raises.

    The conversation is summarised, **not closed**: a buyer who calls and then
    messages twenty minutes later should continue one thread, and closing on
    every hang-up would fork it. The idle sweep owns closing.
    """
    if _FINISH is None or not session_key:
        return
    try:
        _FINISH(session_key, reason)
    except Exception:  # noqa: BLE001 - teardown must not depend on the summary
        pass


def run_rag_query_sync(
    prompt: str,
    channel: str = "voice",
    question: str | None = None,
    session_key: str = "",
    caller: str = "",
    channel_kind: str = "voice_inbound",
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
    return _ANSWER(
        question or prompt, session_key=session_key or "voice",
        caller=caller, channel_kind=channel_kind,
    )
