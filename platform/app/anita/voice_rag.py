"""Adapter: the voice loop's streaming path, cut from the shared answer.

The source's `query_rag_stream` was bound to its MCP-first dispatcher and
yielded LLM deltas. ANITA answers deterministic-first, so there are no token
deltas to yield — but the *reason* this path exists still holds: the caller
should hear the first clause while the rest is still being prepared. This cuts
the composed answer at speakable boundaries and yields clause by clause, so
clause-level TTS (`voice.py` `_speak_clause`) still starts early.

`voice.py:1455` previously imported `query_rag_stream` from here while the
module defined only a `make_query_rag_stream()` factory. The import raised
ImportError, a broad `except` swallowed it, and every streamed turn returned
`("", [])` — the caller heard the degraded apology no matter what they asked.

It shares the single seam installed by `voice_pipeline.install()`, so the
batch path and the streaming path can never answer from different engines.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator


async def query_rag_stream(
    prompt: str,
    cancel: "asyncio.Event | None" = None,
    retrieval_query: str | None = None,
    session_key: str = "",
    **kwargs,
) -> AsyncIterator[str]:
    """Yield the answer, clause by clause.

    ``voice.py:1483`` calls this with the assembled prompt, a cancellation
    event, and the caller's actual utterance as `retrieval_query`. The
    utterance — not the prompt — is what is answered, for the same `C3/N1`
    reason the batch path records.
    """
    from .llm import ClauseCutter
    from .voice_pipeline import run_rag_query_sync

    question = retrieval_query or prompt

    # The engine is synchronous and does retrieval plus SQL; run it off the
    # event loop so the media stream keeps flowing while it works.
    answer = await asyncio.to_thread(
        run_rag_query_sync, prompt, "voice", question, session_key
    )

    if cancel is not None and cancel.is_set():
        # The caller interrupted while we were composing. Nothing is owed to
        # them: the engine's own GenerationCancelled contract is that a
        # cancelled turn does not flush a trailing clause.
        return
    if not answer:
        return

    cutter = ClauseCutter()
    for clause in cutter.feed(answer):
        yield clause
    tail = cutter.flush()
    if tail:
        yield tail
