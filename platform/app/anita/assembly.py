"""The composition root (MOD-10).

Everything the platform needs is constructed here, in one place, in an order
that can refuse. Nothing else in the codebase builds a live object graph — the
modules define *what* each piece is, this file decides *which* pieces exist and
what they are wired to.

Two rules this file exists to enforce:

**Refuse before listening.** The isolation guard and the configuration audit run
before a socket is opened. A process that starts serving and *then* discovers it
shares a database with a sibling product has already accepted traffic it cannot
serve correctly.

**Degrade honestly.** If the corpus is missing, or Ollama is not running, or the
index has not been built, the platform still starts — and the surfaces decline
questions rather than answering them from a model's memory. An assistant that
says "I don't have that in our catalogue" is useful; one that invents a price is
not. Every degradation is logged with what specifically is missing, because a
silent fallback is indistinguishable from a working system until a buyer is
misinformed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .channels import default_gateway
from .config import AnitaSettings, load_settings
from .observability import MetricsRegistry, QualityGate, TraceWriter, cases_from_corpus
from .orchestrator import ConversationRegistry, Orchestrator
from .speech import AdmissionController
from .surfaces import (
    ROUTE_TABLE,
    AdminSurface,
    BrowserMicSurface,
    ChatSurface,
    Router,
    build_router,
)

logger = logging.getLogger("anita.assembly")


@dataclass
class Assembly:
    """What was built, and what was not. The second half is the useful half."""

    settings: AnitaSettings
    router: Router
    chat: ChatSurface
    admin: AdminSurface
    notes: list[str] = field(default_factory=list)
    # Set by `assemble`; None when sentiment observation is unavailable.
    sentiment: Any = None

    @property
    def degraded(self) -> bool:
        return bool(self.notes)

    def describe(self) -> str:
        lines = [f"instance  : {self.settings.identity.agent_name}"]
        lines.append(f"collection: {self.settings.isolation.chroma_collection}")
        lines.append(f"corpus    : {self.settings.corpus_dir}")
        lines.append(f"routes    : {len(ROUTE_TABLE)}")
        if self.notes:
            lines.append("degraded  :")
            lines.extend(f"  - {note}" for note in self.notes)
        else:
            lines.append("degraded  : no")
        return "\n".join(lines)


def _rag_backend(settings: AnitaSettings) -> str:
    """Which retrieval backend to construct: ``shared`` or ``memory``.

    ``ANITA_RAG_BACKEND`` wins over the config file so the rollback can be
    exercised without editing a file — the drill is "restart with the variable
    set", not "change the config and redeploy".
    """
    import os

    chosen = (os.environ.get("ANITA_RAG_BACKEND")
              or getattr(getattr(settings, "rag", None), "backend", "")
              or "shared").strip().lower()
    return "memory" if chosen == "memory" else "shared"


def _build_retriever(settings: AnitaSettings, chunks: list[Any], notes: list[str]):
    """Hybrid retrieval, or ``None`` with a reason.

    Returns ``None`` rather than raising: a platform that cannot retrieve can
    still take a message, decline it correctly and capture the lead. That is a
    working product with a missing feature, not an outage.
    """
    from .embeddings import EmbeddingError, OllamaEmbedder
    from .retrieval import GroundingGate, InMemoryStore, Retriever, assert_threshold

    try:
        assert_threshold(settings.retrieval.similarity_threshold)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"retrieval disabled — {exc}")
        return None

    try:
        embedder = OllamaEmbedder(
            base_url=settings.model.ollama_url,
            model=settings.model.embed_model,
            keep_alive=settings.model.ollama_keep_alive,
        )
    except Exception as exc:  # noqa: BLE001
        notes.append(f"retrieval disabled — embedder unavailable: {type(exc).__name__}")
        return None

    gate = GroundingGate.from_chunks(
        chunks, threshold=settings.retrieval.similarity_threshold
    )

    backend = _rag_backend(settings)
    if backend == "memory":
        # The rollback. Constructs exactly the pre-migration graph, so a
        # regression is reversed by configuration rather than by reverting code.
        logger.info("retrieval backend: memory (InMemoryStore, pre-migration path)")
        return Retriever(chunks, embedder, InMemoryStore(), gate,
                         top_k=settings.retrieval.top_k, fetch_k=settings.retrieval.fetch_k)

    try:
        from .rag_adapter import build_shared_retriever

        adapter = build_shared_retriever(
            chunks=chunks,
            embedder=embedder,
            gate=gate,
            corpus_dir=settings.corpus_dir,
            collection=settings.isolation.chroma_collection,
            persist_path=Path(settings.isolation.chroma_path),
            expected_markers=settings.corpus_gate.expected_markers,
            blocked_markers=settings.corpus_gate.blocked_markers,
            top_k=settings.retrieval.top_k,
            fetch_k=settings.retrieval.fetch_k,
        )
        logger.info("retrieval backend: shared (shared.rag + Chroma persistence)")
        return adapter
    except Exception as exc:  # noqa: BLE001
        # A shared backend that cannot be constructed must not take retrieval
        # down: fall back to the known-good path and say so. The alternative is
        # that a configuration error looks like a broken product.
        notes.append(
            f"shared retrieval backend unavailable, using in-memory — "
            f"{type(exc).__name__}: {exc}"
        )
        return Retriever(chunks, embedder, InMemoryStore(), gate,
                         top_k=settings.retrieval.top_k, fetch_k=settings.retrieval.fetch_k)


def _index_available(settings: AnitaSettings) -> bool:
    """Whether a built index exists for the configured collection.

    Checked rather than assumed: an unbuilt index makes every dense query return
    nothing, which the gate would then correctly report as not-relevant — and a
    buyer would be told the catalogue does not contain a product it does
    contain.
    """
    path = Path(settings.isolation.chroma_path)
    return path.exists() and any(path.iterdir()) if path.exists() else False


def assemble(
    *, env: dict[str, str] | None = None, require_preflight: bool = True
) -> Assembly:
    """Build the platform, or refuse.

    Raises whatever the boot sequence raises when the instance is not safe to
    run. With ``require_preflight=False`` the isolation audit is skipped, which
    exists only so tests can build an assembly without a full environment.
    """
    notes: list[str] = []

    if require_preflight:
        from .boot import preflight

        report = preflight(env=env)
        if not report.ok:
            from .boot import render_report

            raise RuntimeError("preflight refused:\n" + render_report(report))
        settings = report.settings
    else:
        settings = load_settings(env or {})

    assert settings is not None

    # -- corpus ------------------------------------------------------------
    from .corpus import Corpus

    try:
        corpus = Corpus(settings.corpus_dir)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"corpus not available at {settings.corpus_dir} — the platform will "
            f"not start without it, because every answer is grounded in it: {exc}"
        ) from exc

    # -- answer engine -----------------------------------------------------
    from .answers import AnitaAnswers

    answers = AnitaAnswers(corpus)

    # -- retrieval ---------------------------------------------------------
    chunks: list[Any] = []
    retriever = None
    from .retrieval import load_chunks

    if not corpus.chunks_path.exists():
        notes.append("retrieval disabled — chunks.jsonl not present")
    else:
        chunks = load_chunks(corpus.chunks_path)
        if not _index_available(settings):
            notes.append(
                "index not built — dense retrieval will return nothing until "
                "build_index() has run for this collection"
            )
        retriever = _build_retriever(settings, chunks, notes)

    # -- conversation, channels, capacity ----------------------------------
    registry = ConversationRegistry()
    orchestrator = Orchestrator(registry=registry)

    # Secrets are read from the settings' environment mapping, never from
    # os.environ directly — that is what makes the same code testable and what
    # keeps a stray process environment from changing behaviour. A missing
    # Twilio token leaves signature validation unable to verify anything, so the
    # gateway is told plainly rather than being handed an empty string that
    # looks like a configured value.
    twilio_token = settings.env.get("TWILIO_AUTH_TOKEN", "")
    if not twilio_token:
        notes.append(
            "TWILIO_AUTH_TOKEN unset — inbound webhooks cannot be signature-verified"
        )
    gateway = default_gateway(twilio_auth_token=twilio_token)

    admission = AdmissionController(
        max_concurrent_calls=settings.speech.max_concurrent_calls
    )

    # -- observability -----------------------------------------------------
    metrics = MetricsRegistry()
    # Absence is registered up front: a metric that is only *set* once something
    # is measured reads as zero before the first measurement, and an operator
    # cannot tell "nothing queued" from "we never counted".
    for name in ("queue_depth", "gpu_free_mib", "llm_ttfb_ms", "retrieval_ms"):
        metrics.mark_unavailable(name)

    traces = None
    try:
        log_dir = Path(settings.isolation.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        traces = TraceWriter(log_dir / "traces.jsonl")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"trace writing disabled — {type(exc).__name__}: {exc}")

    # -- the release gate, built from the catalogue's own FAQs -------------
    gate = None
    try:
        cases = cases_from_corpus(corpus)
        if cases:
            gate = QualityGate(cases)
        else:
            notes.append("release gate has no cases — the catalogue contains no FAQs")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"release gate unavailable — {type(exc).__name__}: {exc}")

    # -- surfaces ----------------------------------------------------------
    # -- leads (MOD-06) ----------------------------------------------------
    # A local SQLite CRM standing in for Salesforce. Built here so the chat
    # surface can actually take a lead: until now LeadService, the mock store,
    # the outbound queue and the qualification rules existed and were
    # referenced by nothing, so the assistant offered a callback it had no way
    # to record.
    leads = None
    try:
        from .crm import MockCrmStore
        from .leads import LeadService

        crm_path = Path(settings.isolation.log_dir).parent / "anita_crm.sqlite"
        crm_path.parent.mkdir(parents=True, exist_ok=True)
        leads = LeadService(MockCrmStore(crm_path))
    except Exception as exc:  # noqa: BLE001
        notes.append(f"lead capture disabled — {type(exc).__name__}: {exc}")

    # -- sentiment + escalation (MOD-07, closing UC-10) --------------------
    # The core detects a negative turn; the sink turns it into a Handoff against
    # the same CRM a "request a human" uses, so escalations and callback
    # requests arrive in one queue rather than two.
    observer = None
    try:
        from shared.sentiment import SentimentConfig, SentimentCore

        from .escalation import EscalationSink, SentimentObserver
        from .sentiment import anita_sentiment_config

        crm = getattr(leads, "store", None) if leads is not None else None
        sink = EscalationSink(store=None, crm=crm)
        observer = SentimentObserver(
            SentimentCore(anita_sentiment_config(
                negative_threshold=settings.sentiment.negative_escalation_threshold,
            )),
            sink,
        )
        if crm is None:
            notes.append(
                "escalation has no CRM attached — negatives are recorded but no "
                "handoff is opened"
            )
    except Exception as exc:  # noqa: BLE001
        notes.append(f"sentiment observation disabled — {type(exc).__name__}: {exc}")

    chat = ChatSurface(orchestrator, answers=answers, retriever=retriever, leads=leads,
                       sentiment=observer)

    admin = AdminSurface(
        registry=registry, gateway=gateway, admission=admission,
        metrics=metrics, gate=gate, traces=traces,
    )

    router = build_router(
        chat=chat,
        mic=BrowserMicSurface(admission),
        admin=admin,
        admin_token=settings.env.get("ANITA_ADMIN_TOKEN", ""),
        gate_answerer=_gate_answerer(chat, retriever),
    )

    for note in notes:
        logger.warning("degraded: %s", note)

    built = Assembly(settings=settings, router=router, chat=chat, admin=admin, notes=notes)
    built.sentiment = observer
    return built


def _gate_answerer(chat: ChatSurface, retriever):
    """The answer path the release gate judges.

    It is the **same** path a buyer's question takes: retrieval, then the gate,
    then the deterministic answer engine. Running the gate against anything else
    would measure a system that does not exist. When retrieval is not wired the
    answerer returns the decline — and the gate then fails, which is the correct
    outcome for a platform that cannot ground its answers.
    """
    def answer(case) -> str:
        question = getattr(case, "question", None) or getattr(case, "query", "")
        if retriever is None:
            from .surfaces import DECLINE_TEXT

            return DECLINE_TEXT
        response = chat.post_message(message=question, session_id=f"gate-{case.case_id}")
        return str(response.body.get("text", ""))

    return answer


__all__ = ["Assembly", "assemble"]
