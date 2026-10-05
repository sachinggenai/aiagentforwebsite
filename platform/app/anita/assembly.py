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
from .observability import JsonlSink, MetricsRegistry, TraceWriter
from .orchestrator import (
    ConversationJanitor,
    ConversationRegistry,
    Orchestrator,
)
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
    # The Twilio ingress decision (US-001). Set by `assemble`; None when the
    # instance has no public address, in which case no call can legitimately
    # arrive and there is nothing to decide.
    voice: Any = None

    @property
    def degraded(self) -> bool:
        return bool(self.notes)

    def describe(self) -> str:
        lines = [f"instance  : {self.settings.identity.agent_name}"]
        # The collection the process actually serves, which after a promotion
        # is not the configured name — reporting the config made the boot
        # report disagree with the running system, the exact class of drift
        # this file exists to catch.
        configured = self.settings.isolation.chroma_collection
        live = ""
        try:
            store = getattr(getattr(self.chat, "retriever", None), "store", None)
            live = store.name() if store is not None and hasattr(store, "name") else ""
            live = str(live).split(":", 1)[-1]   # "chroma:anita_ams__v4" -> the name
        except Exception:  # noqa: BLE001 - a report must never fail the boot
            live = ""
        if live and live != configured:
            lines.append(f"collection: {live}  (promoted; config says {configured})")
        else:
            lines.append(f"collection: {configured}")
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

        # US-010: the collection this process queries is the one the build
        # store says is LIVE — that pointer is what makes promotion atomic
        # (the swap is a committed row, and every process reads it). With no
        # promoted build it is the configured name, which is the
        # pre-lifecycle behaviour.
        from .index import live_collection_name

        index_store = settings.log_dir_path / "anita_index.sqlite"
        collection = live_collection_name(index_store, settings.isolation.chroma_collection)
        if collection != settings.isolation.chroma_collection:
            logger.info("index pointer: serving promoted collection %s", collection)

        adapter = build_shared_retriever(
            chunks=chunks,
            embedder=embedder,
            gate=gate,
            corpus_dir=settings.corpus_dir,
            collection=collection,
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


def _build_release_gate(settings, notes: list[str]):
    """Load the frozen evaluation set and build the gate — or disable it loudly.

    US-027: the set is a versioned repo file with a pinned hash, not a set
    regenerated from the catalogue at boot. Regeneration meant a corpus refresh
    silently changed the test set — the opposite of frozen — and it made every
    "70/70 passed" a statement about a different set than the previous run's.

    Fails closed at every step. A missing file, an empty set, or a hash that
    does not match the pin leaves the gate absent, and `release_gate.py`
    refuses promotion when there is no gate ("a gate with nothing to assert
    certifies nothing").
    """
    try:
        from .observability import QualityGate, eval_set_sha256, load_eval_set

        eval_path = settings.eval_set_path
        cases = load_eval_set(eval_path)
        if not cases:
            raise ValueError(f"the frozen set at {eval_path} is empty")

        digest = eval_set_sha256(eval_path)
        expected = settings.release.eval_set_sha256
        if expected and digest != expected:
            raise ValueError(
                f"eval set hash mismatch — file {digest[:12]}…, config pins "
                f"{expected[:12]}…; regenerate and re-pin deliberately "
                f"(scripts/freeze_eval_set.py)"
            )
        if not expected:
            notes.append(
                "release gate running an UNPINNED eval set — pin "
                "release.eval_set_sha256 in config/anita.yaml"
            )
        return QualityGate(cases, set_sha256=digest)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"release gate unavailable — {type(exc).__name__}: {exc}")
        return None


def deployed_environment() -> dict[str, str]:
    """The environment a *deployed* process runs with: `.env` overlaid by the
    real environment, exactly what the boot preflight uses.

    `assemble(require_preflight=False)` deliberately keeps `env=None` meaning
    "an empty environment", so a developer's `.env` can never change a test's
    result. That default is right for tests and wrong for tooling that runs
    *against the instance* — the release gate, the system check, Streamlit —
    which used to evaluate a default assembly while printing "TWILIO_AUTH_TOKEN
    unset" beside a `.env` that sets it (work-status §8.5).

    Those callers pass this explicitly: the isolation is opt-out, not a
    surprise.
    """
    from .config import load_settings

    return dict(load_settings().env)


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

    # US-012: one definition of "what the catalogue has published", used by the
    # answer path to say "not published" instead of declining for the seven
    # products the source site leaves blank (`DG-13`).
    coverage_probe = None
    try:
        from .coverage import CoverageProbe

        coverage_probe = CoverageProbe(corpus)
    except Exception as exc:  # noqa: BLE001 - absence handling degrades to the old decline
        notes.append(f"coverage probe unavailable — {type(exc).__name__}: {exc}")

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
    # US-004/US-006: the conversation thread, its identity links and every
    # turn. Built before the registry because the registry resumes from it —
    # that is what makes "a WhatsApp conversation survived a restart" true
    # rather than aspirational.
    conversation_store = None
    turn_recorder = None
    try:
        from .conversation_store import ConversationStore, ReplayQueue, TurnRecorder

        log_dir = Path(settings.isolation.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        conversation_store = ConversationStore(log_dir / "anita_conversations.sqlite")
        turn_recorder = TurnRecorder(
            conversation_store,
            ReplayQueue(capacity=settings.conversation.replay_capacity),
            on_event=lambda event, payload: logger.info("%s %s", event, payload),
        )
    except Exception as exc:  # noqa: BLE001 - a store failure must not stop the boot
        notes.append(
            f"transcripts unavailable — {type(exc).__name__}: {exc}; conversations "
            f"work in memory only and are lost on restart"
        )

    registry = ConversationRegistry(
        idle_window_seconds=settings.conversation.idle_window_hours * 3600,
        store=conversation_store,
        recorder=turn_recorder,
        context_turns=settings.conversation.context_turns,
    )
    orchestrator = Orchestrator(registry=registry, queue_depth=settings.conversation.queue_depth)

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

    # The public base URL Twilio signs against. Configured, never derived from
    # the request: a Cloudflare tunnel rewrites `Host`, so validating against
    # the delivered header would refuse every webhook and the product would
    # simply appear dead (`MOD-01` §6.1). `TUNNEL_HOST` is the hostname the
    # tunnel publishes. Without it, signature validation has nothing to check
    # against and inbound calls cannot be trusted.
    tunnel_host = (settings.env.get("TUNNEL_HOST", "") or "").strip()
    public_base_url = f"https://{tunnel_host}".rstrip("/") if tunnel_host else ""
    if not public_base_url:
        notes.append(
            "TUNNEL_HOST unset — Twilio has no address to call back, so inbound "
            "voice cannot be signature-verified"
        )

    gateway = default_gateway(
        twilio_auth_token=twilio_token, public_base_url=public_base_url
    )

    # US-018: voice capacity is the *device's* capacity. Two gaps closed here.
    #
    # First, the configured VRAM numbers were never passed to the controller —
    # it ran on its dataclass defaults, so `vram.anita_budget_mib` and
    # `sibling_floor_mib` in `anita.yaml` were read by nothing. A capacity key
    # the code does not read is decoration (TAC-7).
    #
    # Second, a host without a CUDA device admitted calls as if it had one,
    # next to a boot report claiming voice "will be refused at admission" —
    # a claim no code implemented. CPU transcription and synthesis demonstrably
    # work (proven on a simulated call), so voice is not refused; it is
    # **capped**, which is what one CPU can honour.
    from .platform_detect import detect_platform, pick_model
    from .speech import CPU_CONCURRENT_CALLS

    device = detect_platform()
    try:
        voice_capable = pick_model(device, settings).voice_capable
    except ValueError:
        voice_capable = False

    max_calls = settings.speech.max_concurrent_calls
    if not voice_capable:
        max_calls = min(max_calls, CPU_CONCURRENT_CALLS)
        notes.append(
            f"voice capped at {max_calls} concurrent call(s) — no CUDA device "
            f"({device.reason}); CPU synthesis is ~0.6x real time and contends "
            f"with everything else"
        )

    admission = AdmissionController(
        max_concurrent_calls=max_calls,
        anita_budget_mib=settings.vram.anita_budget_mib,
        shared_card_mib=settings.vram.shared_card_mib,
        sibling_floor_mib=settings.vram.sibling_floor_mib,
    )


    # The voice engine reads its admission registry from `voice_admission.REGISTRY`
    # (voice.py imports that name inside each call). Install the controller
    # assembled above, so voice capacity *is* the controller the surfaces admit
    # against — with two separate controllers the box would admit calls the rest
    # of the app had refused, and the ceiling would not hold.
    from .voice_admission import install as install_voice_registry

    install_voice_registry(admission)

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
        # `JsonlSink(path)`, not the bare path: a Path is not a sink, so this
        # previously built a writer whose every emit raised `AttributeError`
        # into the swallow — traces were dropped silently, and the file was
        # never created.
        traces = TraceWriter(JsonlSink(log_dir / "perf_turns.jsonl"))
        # US-025: point the voice adapter at the assembled writer, the same
        # seam shape as `voice_admission.install`.
        from .voice_trace import install as install_trace_writer

        install_trace_writer(traces)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"trace writing disabled — {type(exc).__name__}: {exc}")

    # -- capacity monitor (US-018) -----------------------------------------
    # The sampler that makes the degradation ladder live. Started only on a
    # host where ANITA actually occupies the device — `voice_capable`, not
    # merely "nvidia-smi exists".
    #
    # That distinction is not academic: gating on nvidia-smi alone put a
    # monitor on a box whose speech stack runs entirely on CPU (torch absent),
    # and it degraded the ladder on the first samples. Shedding voice releases
    # *ANITA's* VRAM — on a CPU host there is none to release, so the only
    # effect was to disable a working feature on a card that was never ours to
    # fix. The metric is still the point on a GPU host: an operator needs to
    # see pressure before it crosses a threshold.
    if voice_capable:
        from .capacity import CapacityMonitor, CapacityPolicy, nvidia_smi_free_mib

        monitor = CapacityMonitor(
            CapacityPolicy(admission, source=nvidia_smi_free_mib), metrics=metrics
        )
        monitor.start()

    # -- the release gate, built from the FROZEN evaluation set ------------
    gate = _build_release_gate(settings, notes)

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
    # Bound before the try: the admin surface below needs it whether or not
    # sentiment wiring succeeds, and a NameError there would take down boot.
    crm = getattr(leads, "store", None) if leads is not None else None
    try:
        from shared.sentiment import SentimentConfig, SentimentCore

        from .escalation import EscalationSink, SentimentObserver
        from .sentiment import anita_sentiment_config

        # US-022: the last step of an escalation is telling a person. With no
        # SMTP configured the handoff is still opened and stays `open` in the
        # admin queue — recorded, visible, and honestly not yet notified.
        from .notify import from_environment as build_notifier

        notifier = build_notifier(settings.env)
        sink = EscalationSink(store=None, crm=crm, notifier=notifier)
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
        elif notifier is None:
            notes.append(
                "escalation notifications unconfigured — handoffs are recorded and "
                "stay open for the admin queue; set SMTP_HOST and SALES_NOTIFY_EMAIL "
                "to email the rep"
            )
    except Exception as exc:  # noqa: BLE001
        notes.append(f"sentiment observation disabled — {type(exc).__name__}: {exc}")

    # -- inference backend (US-014 / US-015) -------------------------------
    # Constructed here and injected into the answer engine, so the prose leg
    # and its circuit breaker are live rather than a library island. The
    # backend is only *called* for open questions — every deterministic path
    # (price, MOQ, delivery, spec, company facts) answers before it is
    # reached — and every failure path (breaker open, timeout, a grounding
    # rejection) falls back to the verbatim answer, so a dead model degrades
    # prose and never the commercial path.
    llm = None
    try:
        from .llm import CircuitBreaker, OllamaBackend

        if settings.model.llm_provider in ("auto", "ollama"):
            llm = OllamaBackend(
                settings.model.ollama_url,
                settings.model.ollama_model,
                num_ctx=settings.model.ollama_num_ctx,
                keep_alive=settings.model.ollama_keep_alive,
                breaker=CircuitBreaker(),
            )
    except Exception as exc:  # noqa: BLE001 - prose is optional, boot is not
        notes.append(f"prose backend unavailable — {type(exc).__name__}: {exc}")

    chat = ChatSurface(orchestrator, answers=answers, retriever=retriever, leads=leads,
                       sentiment=observer, traces=traces, llm=llm, probe=coverage_probe)

    # The voice path answers through the *same* engine instance as chat — one
    # answer tree, so a caller and a web buyer asking the same question cannot
    # be told different things. Installed into the engine's module-level seam
    # because voice.py imports the name inside the method that uses it.
    from .voice_pipeline import install as install_voice_answer

    install_voice_answer(orchestrator, chat.engine)

    # The Twilio ingress (US-001): signature, then admission, then TwiML — in
    # that order. Built from the same admission controller the rest of the app
    # uses, so a call refused here is a call the platform knows it refused.
    from .voice_webhook import VoiceWebhook

    # The channel tables (US-001). Best-effort: a platform that cannot open its
    # own database should still answer, and the two protections they carry are
    # reported as missing rather than silently absent.
    channel_store = None
    voice_number = (settings.env.get("TWILIO_PHONE_NUMBER", "") or "").strip()
    try:
        from pathlib import Path as _Path

        from .channel_store import ChannelStore

        log_dir = _Path(settings.isolation.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        channel_store = ChannelStore(log_dir / "anita_channels.sqlite")
        if voice_number:
            # Declaring the number is what makes attribution possible: without
            # the row, every inbound call is unattributable and the webhook
            # cannot tell our number from the sibling's.
            channel_store.register_endpoint(
                channel_kind="voice_inbound",
                provider="twilio",
                address=voice_number,
                instance_id=settings.isolation.db_schema,
                secret_ref="TWILIO_AUTH_TOKEN",
            )
    except Exception as exc:  # noqa: BLE001 - a store failure must not stop the boot
        logger.warning("channel store unavailable: %s", exc)
        notes.append(
            f"channel store unavailable ({exc}) — inbound calls cannot be "
            f"deduplicated or attributed to this number"
        )

    # -- session tokens (browser mic + Twilio media streams) ----------------
    # The signer the browser ticket route mints with, and the signer the
    # Twilio webhook stamps into its TwiML so the media-stream socket can
    # prove the stream belongs to an admitted call (§8.6). A configured
    # secret survives restarts (a browser reconnect inside the 60 s window
    # resumes); a generated one does not, and the note says so rather than
    # leaving an operator to discover it as "the mic stops working after a
    # deploy".
    import secrets as _secrets

    from .browser_voice import SessionTokens

    session_secret = (settings.env.get("ANITA_SESSION_SECRET", "") or "").strip()
    if not session_secret:
        session_secret = _secrets.token_urlsafe(32)
        notes.append(
            "ANITA_SESSION_SECRET unset — browser-mic and voice-stream session "
            "tokens are signed with a per-process secret and will not survive a "
            "restart; set it to keep the 60 s reconnect window across deploys"
        )
    voice_tokens = SessionTokens(session_secret)

    voice = VoiceWebhook(
        auth_token=twilio_token,
        public_base_url=public_base_url,
        admission=admission,
        callback_number=settings.env.get("ANITA_CALLBACK_NUMBER", ""),
        store=channel_store,
        instance_id=settings.isolation.db_schema,
        address=voice_number,
        stream_tokens=voice_tokens,
    )

    # -- outbound sends (US-002) -------------------------------------------
    # The queue exists whenever the channel store does; the *sender* only when
    # Twilio messaging credentials do. Without a sender, sends stay QUEUED and
    # visible in the console — a buyer waiting is better represented than
    # erased.
    outbox = None
    sender = None
    outbox_worker = None
    try:
        from .outbox import OutboxWorker, SendOutbox

        outbox = SendOutbox(Path(settings.isolation.log_dir) / "anita_outbox.sqlite")
    except Exception as exc:  # noqa: BLE001 - a queue failure must not stop the boot
        notes.append(f"outbound send queue unavailable — {type(exc).__name__}: {exc}")

    account_sid = (settings.env.get("TWILIO_ACCOUNT_SID", "") or "").strip()
    messaging_number = (settings.env.get("TWILIO_WHATSAPP_NUMBER", "") or voice_number).strip()
    if outbox is not None and account_sid and twilio_token and messaging_number:
        from .messaging import TwilioMessagesClient

        sender = TwilioMessagesClient(
            account_sid=account_sid,
            auth_token=twilio_token,
            from_number=messaging_number,
        )
        outbox_worker = OutboxWorker(
            outbox, sender, interval_s=settings.outbound.poll_interval_seconds
        )
        outbox_worker.start()
    elif outbox is not None:
        notes.append(
            "Twilio messaging credentials unset — WhatsApp replies and queued "
            "sends stay QUEUED and visible in the console; set TWILIO_ACCOUNT_SID, "
            "TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_NUMBER to dispatch them"
        )

    # -- messaging ingress (US-002) ----------------------------------------
    messaging = None
    try:
        from .messaging import MessagingWebhook

        messaging = MessagingWebhook(
            auth_token=twilio_token,
            public_base_url=public_base_url,
            answer=_messaging_answerer(chat),
            gateway=gateway,
            outbox=outbox,
            sender=sender,
            store=channel_store,
            instance_id=settings.isolation.db_schema,
            address=messaging_number,
            callback_number=settings.env.get("ANITA_CALLBACK_NUMBER", ""),
        )
    except Exception as exc:  # noqa: BLE001
        notes.append(f"WhatsApp ingress unavailable — {type(exc).__name__}: {exc}")

    # -- the extra channel (US-002, DG-07) ---------------------------------
    # Declared from config, enabled from config, and enabled only if its
    # secret exists. `enable` can refuse; a refusal leaves every other channel
    # exactly as it was (`AC-7`).
    extra_registry = None
    extra_ingress = None
    try:
        from .extra_channel import (
            AdapterConfigInvalid,
            AdapterRegistry,
            ExtraChannelConfig,
            ExtraChannelIngress,
        )

        extra_raw = settings.channels.extra
        extra_registry = AdapterRegistry()
        if extra_raw.id:
            extra_registry.declare(ExtraChannelConfig(
                id=extra_raw.id,
                provider=extra_raw.provider,
                address=extra_raw.address,
                secret_env=extra_raw.secret_env,
                capabilities=tuple(extra_raw.capabilities),
                inbound=extra_raw.inbound,
                outbound=extra_raw.outbound,
            ))
            if extra_raw.enabled:
                if not settings.env.get(extra_raw.secret_env, ""):
                    notes.append(
                        f"extra channel {extra_raw.id!r} declared enabled but "
                        f"{extra_raw.secret_env} is unset — it refuses to enable "
                        f"and names the credential"
                    )
                else:
                    try:
                        result = extra_registry.enable(extra_raw.id)
                        logger.info("extra channel %r: %s", extra_raw.id,
                                    "enabled" if result.enabled else result.detail)
                    except (AdapterConfigInvalid, Exception) as exc:  # noqa: BLE001
                        notes.append(
                            f"extra channel {extra_raw.id!r} refused to enable — {exc}"
                        )
        extra_ingress = ExtraChannelIngress(
            registry=extra_registry,
            gateway=gateway,
            answer=_messaging_answerer(chat),
            secrets=dict(settings.env),
            outbox=outbox,
            sender=sender,
            store=channel_store,
        )
    except Exception as exc:  # noqa: BLE001
        notes.append(f"extra channel unavailable — {type(exc).__name__}: {exc}")

    admin = AdminSurface(
        registry=registry, gateway=gateway, admission=admission,
        metrics=metrics, gate=gate, traces=traces, crm=crm, outbox=outbox,
        store=conversation_store,
    )

    # The periodic duties (US-006): close idle conversations with a summary,
    # and drain any turns buffered while the store was unavailable. Started
    # only when the store exists — a janitor over an in-memory registry would
    # be a thread that closes conversations nobody can review.
    if conversation_store is not None:
        janitor = ConversationJanitor(orchestrator=orchestrator, interval_s=60.0)
        janitor.start()

    router = build_router(
        chat=chat,
        mic=BrowserMicSurface(
            admission,
            tokens=voice_tokens,
            callback_number=settings.env.get("ANITA_CALLBACK_NUMBER", ""),
        ),
        admin=admin,
        admin_token=settings.env.get("ANITA_ADMIN_TOKEN", ""),
        gate_answerer=_gate_answerer(chat, retriever),
        voice=voice,
        voice_sessions=_voice_session_factory(),
        voice_tokens=voice_tokens,
        browser_voice=_voice_session_factory(),
        messaging=messaging,
        extra_channel=extra_ingress,
    )

    for note in notes:
        logger.warning("degraded: %s", note)

    built = Assembly(
        settings=settings, router=router, chat=chat, admin=admin, notes=notes,
        voice=voice,
    )
    built.sentiment = observer
    return built


def _voice_session_factory():
    """One engine session per call.

    A factory, not an instance: `VoiceCallSession` holds the audio buffer, the
    VAD counter and the history of a *single* call. Sharing one between calls
    would put two callers' audio through one end-of-speech decision and produce
    a transcript that is neither of theirs.
    """
    from .voice import VoiceCallSession

    def make(_call_id: str = "") -> VoiceCallSession:
        # `call_id` is assigned by the media-stream relay from Twilio's `start`
        # event, which arrives after the socket opens — so the factory cannot
        # take it, and does not need to.
        return VoiceCallSession(direction="inbound")

    return make


def _messaging_answerer(chat: ChatSurface):
    """The messaging channels' answer path — the *same* engine as web chat.

    One engine per channel would be one product per channel: the WhatsApp
    answer and the web answer to the same question would drift the first time
    either was touched. This is the seam that makes them one.
    """
    def answer(message: str, *, session_id: str) -> str:
        response = chat.post_message(message=message, session_id=session_id)
        body = response.body if isinstance(response.body, dict) else {}
        return str(body.get("text") or "")

    return answer


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
