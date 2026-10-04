"""Adapter: streaming retrieval for the voice loop.

`voice.py` calls `query_rag_stream`, which the source bound to its MCP-first
dispatcher. ANITA has one in-process retriever (:mod:`anita.retrieval`) with a
grounding gate in front of it, so this bridges the shapes rather than the
transport.
"""

from __future__ import annotations

from typing import AsyncIterator

from .retrieval import Retriever


def make_query_rag_stream(retriever: Retriever):
    """Return a callable matching the source's `query_rag_stream` signature."""

    async def query_rag_stream(question: str, **kwargs) -> AsyncIterator[str]:
        result = retriever.retrieve(question)
        if not result.ok:
            # No context means no answer (BRD-04). The voice loop yields the
            # decline and the caller offers a human, rather than the model
            # improvising from nothing.
            yield (
                "I don't have that in our catalogue. "
                "Our team can confirm it for you."
            )
            return

        from .answers import AnitaAnswers, FactKind

        answers = AnitaAnswers(retriever)
        for chunk in result.chunks:
            yield chunk.chunk.text

    return query_rag_stream
