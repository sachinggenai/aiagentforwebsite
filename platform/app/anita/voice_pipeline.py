"""Adapter: synchronous retrieval for the voice loop's non-streaming path.

Named `voice_pipeline` rather than `pipeline` so it does not read as the
source's Pipecat pipeline — that one is installed but bypassed on the live path
and is deliberately not carried (REC-20).
"""

from __future__ import annotations

from .retrieval import Retriever


def make_run_rag_query_sync(retriever: Retriever):
    """Return a callable matching the source's `run_rag_query_sync`."""

    def run_rag_query_sync(question: str, **kwargs) -> str:
        result = retriever.retrieve(question)
        if not result.ok:
            return "I don't have that in our catalogue. Our team can confirm it for you."
        return result.context()

    return run_rag_query_sync
