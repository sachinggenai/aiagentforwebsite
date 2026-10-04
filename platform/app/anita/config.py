"""The single source of runtime configuration (TRD-41).

Two layers, deliberately separated:

* ``config/anita.yaml`` — non-secret structure, isolation coordinates, model and
  tuning values. Safe to commit and to print.
* the environment — anything secret, plus operator overrides.

The result is a **frozen** :class:`AnitaSettings`. Nothing downstream re-reads
``os.environ`` (TRD-41 ``M9-7``): a value that can change under a running
process is a value that cannot be reasoned about, and isolation coordinates in
particular must not move mid-flight.

Every typed parser names the offending key on failure. An operator diagnosing a
boot refusal should never have to read source to find out which variable is
wrong — they should be told.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError

# --------------------------------------------------------------------------
# Typed environment readers. Each raises ConfigError(key, reason) — never
# falls back to a default on a malformed value, because a silent fallback on
# an isolation coordinate is exactly how two products end up sharing a database.
# --------------------------------------------------------------------------
def _env(env: Mapping[str, str], key: str, default: str | None = None) -> str | None:
    value = env.get(key)
    if value is None or value == "":
        return default
    return value


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(key, f"expected an integer, got {raw!r}") from exc


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(key, f"expected a number, got {raw!r}") from exc


def _env_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(key, f"expected a boolean (1/0/true/false), got {raw!r}")


def _env_list_int(env: Mapping[str, str], key: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return tuple(int(part.strip()) for part in raw.split(",") if part.strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(key, f"expected comma-separated integers, got {raw!r}") from exc


# --------------------------------------------------------------------------
# Settings groups
# --------------------------------------------------------------------------
DEFAULT_DB_NAME = "anita_ams"
DEFAULT_DB_SCHEMA = "anita"
DEFAULT_CHROMA_PATH = "./data/chroma_anita"
DEFAULT_CHROMA_COLLECTION = "anita_ams__v1"
DEFAULT_API_PORT = 8100
DEFAULT_CHAT_PORT = 8601
DEFAULT_ADMIN_PORT = 8602
# The Streamlit surface. Declared here rather than passed as a command-line
# flag: a port chosen on the command line is a port nothing checks, and the
# sibling product already occupies 8501 and 8502.
DEFAULT_STREAMLIT_PORT = 8085
DEFAULT_LOG_DIR = "./logs/anita"


@dataclass(frozen=True)
class IsolationSettings:
    """The four resource coordinates the boot guard compares (TRD-42)."""

    db_name: str = DEFAULT_DB_NAME
    db_schema: str = DEFAULT_DB_SCHEMA
    chroma_path: str = DEFAULT_CHROMA_PATH
    chroma_collection: str = DEFAULT_CHROMA_COLLECTION
    api_port: int = DEFAULT_API_PORT
    chat_port: int = DEFAULT_CHAT_PORT
    admin_port: int = DEFAULT_ADMIN_PORT
    streamlit_port: int = DEFAULT_STREAMLIT_PORT
    log_dir: str = DEFAULT_LOG_DIR

    @property
    def ports(self) -> tuple[int, ...]:
        return (self.api_port, self.chat_port, self.admin_port, self.streamlit_port)


@dataclass(frozen=True)
class ForbiddenValues:
    """What the sibling university deployment owns.

    Config, not code: the sibling's coordinates are the operator's knowledge.
    Seeded with the known values so the guard is useful out of the box rather
    than silently empty until someone remembers to fill it in.
    """

    db_name: tuple[str, ...] = ("admissions",)
    chroma_collection: tuple[str, ...] = ("langchain", "meridian_kb__v1")
    # Read from the sibling's own start_services.ps1 and .env, not guessed:
    #   8000 FastAPI · 8010 RAG MCP · 8098 CRM API · 8501 Streamlit (main)
    #   8502 Streamlit (dashboard) · 1234 MLX · 5432 Postgres
    # 8010 and 5432 were missing until this list was checked against the
    # sibling's service script — the guard would have let ANITA bind the MCP
    # port and the Postgres port without complaint.
    # 11434 (Ollama) is deliberately absent: both products share one Ollama.
    port: tuple[int, ...] = (8000, 8010, 1234, 5432, 8098, 8501, 8502)
    log_dir: tuple[str, ...] = ("./logs", "logs")


@dataclass(frozen=True)
class IdentitySettings:
    company_name: str = "ANITA MEDICAL SYSTEMS PVT. LTD."
    agent_name: str = "Anita Assistant"


@dataclass(frozen=True)
class CorpusGateSettings:
    """Which corpus belongs to this product (CR-01 US-102).

    Both lists are required. An empty acceptance list accepts any corpus — the
    gate becomes a no-op while still looking present — and an empty rejection
    list rejects nothing. `RagConfig` refuses either at construction; these
    defaults keep the guard honest even if the config file loses the section.
    """

    expected_markers: tuple[str, ...] = ("anitamedicalsystems",)
    blocked_markers: tuple[str, ...] = (
        "meridian", "university of maryland", "terrapin", "fafsa", "mddcs",
        "mhec", "fairleigh dickinson", "umd", "fdu", "admissions",
    )


@dataclass(frozen=True)
class RagSettings:
    """Which retrieval backend to build.

    `shared` is CR-01's migrated path (shared/rag + Chroma persistence).
    `memory` is the pre-migration path, kept as the rollback: a regression is
    reversed by configuration, not by reverting code.
    """

    backend: str = "shared"


@dataclass(frozen=True)
class ReleaseSettings:
    """What this release does not include, and why.

    A use case that cannot be triggered is not a shipped feature, so the
    release gate refuses promotion while one is unreachable. That is correct
    and it is also unlivable if it can never pass: a gate that is always red is
    a gate people learn to route around, and then it protects nothing.

    Deferral is the honest middle. A use case listed here is a **decision
    someone made and wrote down**, with a reason — not a gap nobody noticed.
    The gate then reports it as deferred rather than failing on it, and the
    list is reviewable because it is values in a config file rather than
    silence.
    """

    # Empty by default, which is the strict position: nothing is deferred, so
    # every declared use case must be reachable. A missing or unreadable config
    # therefore makes the gate harder to pass, not easier — the failure
    # direction that cannot silently ship an unfinished feature.
    deferred_use_cases: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CorpusSettings:
    out_dir: str = "./data/out"
    chunks_file: str = "chunks.jsonl"
    sqlite_file: str = "ams.sqlite"


@dataclass(frozen=True)
class ModelSettings:
    llm_provider: str = "auto"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:14b"
    ollama_num_ctx: int = 8192
    ollama_keep_alive: str = "-1m"
    embed_model: str = "nomic-embed-text"
    mlx_model: str = "mlx-community/Qwen2.5-14B-Instruct-4bit"
    mlx_base_url: str = "http://127.0.0.1:1234"
    mlx_port: int = 1234


@dataclass(frozen=True)
class RetrievalSettings:
    top_k: int = 5
    fetch_k: int = 20
    similarity_threshold: float = 0.62


@dataclass(frozen=True)
class SpeechSettings:
    whisper_model: str = "small.en"
    whisper_num_threads: int = 4
    kokoro_voice: str = "af_heart"
    kokoro_speed: float = 1.0
    vad_silence_ms: int = 600
    vad_speculative_advance_ms: int = 220
    barge_in_enabled: bool = True
    tts_stream: bool = True
    max_concurrent_calls: int = 2


@dataclass(frozen=True)
class SentimentSettings:
    w1: float = 0.30
    w2: float = 0.30
    w3: float = 0.25
    w4: float = 0.15
    ewma_lambda: float = 0.35
    negative_escalation_threshold: float = -0.20


@dataclass(frozen=True)
class OutboundSettings:
    max_attempts: int = 3
    window_start: str = "10:00"
    window_end: str = "19:00"
    timezone: str = "Asia/Kolkata"
    poll_interval_seconds: int = 10
    retry_delays_minutes: tuple[int, ...] = (240, 1440)


@dataclass(frozen=True)
class VramSettings:
    shared_card_mib: int = 16311
    anita_budget_mib: int = 8192
    sibling_floor_mib: int = 6000


@dataclass(frozen=True)
class AnitaSettings:
    """Immutable, validated configuration for one ANITA instance."""

    isolation: IsolationSettings
    forbidden: ForbiddenValues
    identity: IdentitySettings
    corpus: CorpusSettings
    model: ModelSettings
    retrieval: RetrievalSettings
    speech: SpeechSettings
    sentiment: SentimentSettings
    outbound: OutboundSettings
    vram: VramSettings
    project_root: Path
    env: Mapping[str, str]
    # Last, because it carries a default and a defaulted field may not precede
    # the ones without. The default is the strict position: nothing deferred.
    corpus_gate: CorpusGateSettings = CorpusGateSettings()
    rag: RagSettings = RagSettings()
    release: ReleaseSettings = ReleaseSettings()

    # -- convenience ------------------------------------------------------
    @property
    def log_dir_path(self) -> Path:
        return (self.project_root / self.isolation.log_dir).resolve()

    @property
    def chroma_path_resolved(self) -> Path:
        return (self.project_root / self.isolation.chroma_path).resolve()

    @property
    def corpus_dir(self) -> Path:
        return (self.project_root / self.corpus.out_dir).resolve()

    def env_presence(self) -> dict[str, str]:
        """Key -> ``set``/``unset`` for every configured environment key.

        The only safe rendering of the environment: presence, never value.
        """
        from .secrets import presence

        return {key: presence(key, value) for key, value in sorted(self.env.items())}


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name) or {}
    if not isinstance(value, Mapping):
        raise ConfigError(name, "expected a mapping in the config file")
    return value


def _read_yaml(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        raise ConfigError(str(path), "config file not found")
    try:
        import yaml  # imported lazily so the auditor path stays stdlib-only
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ConfigError("PyYAML", "PyYAML is required to read the config file") from exc
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, Mapping):
        raise ConfigError(str(path), "config file must contain a mapping")
    return data


def load_settings(
    env: Mapping[str, str] | None = None,
    config_path: Path | str | None = None,
    project_root: Path | str | None = None,
) -> AnitaSettings:
    """Build frozen settings from the config file plus the environment.

    The environment wins over the file for operator overrides; secrets exist
    only in the environment. Raises :class:`ConfigError` naming the key on any
    malformed value — never falls back silently.
    """
    environment = dict(os.environ if env is None else env)
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[2]
    path = Path(config_path) if config_path else root / "config" / "anita.yaml"

    data = _read_yaml(path)

    iso_raw = _section(data, "isolation")
    forb_raw = _section(data, "isolation_forbidden")
    id_raw = _section(data, "identity")
    corpus_raw = _section(data, "corpus")
    model_raw = _section(data, "model")
    retr_raw = _section(data, "retrieval")
    speech_raw = _section(data, "speech")
    sent_raw = _section(data, "sentiment")
    out_raw = _section(data, "outbound")
    vram_raw = _section(data, "vram")
    release_raw = _section(data, "release")
    gate_raw = _section(data, "corpus_gate")
    rag_raw = _section(data, "rag")

    isolation = IsolationSettings(
        db_name=_env(environment, "ANITA_DB_NAME", str(iso_raw.get("db_name", DEFAULT_DB_NAME))),
        db_schema=_env(environment, "ANITA_DB_SCHEMA", str(iso_raw.get("db_schema", DEFAULT_DB_SCHEMA))),
        chroma_path=_env(environment, "ANITA_CHROMA_PATH", str(iso_raw.get("chroma_path", DEFAULT_CHROMA_PATH))),
        chroma_collection=_env(
            environment,
            "ANITA_CHROMA_COLLECTION",
            str(iso_raw.get("chroma_collection", DEFAULT_CHROMA_COLLECTION)),
        ),
        api_port=_env_int(environment, "ANITA_API_PORT", int(iso_raw.get("api_port", DEFAULT_API_PORT))),
        chat_port=_env_int(environment, "ANITA_CHAT_PORT", int(iso_raw.get("chat_port", DEFAULT_CHAT_PORT))),
        admin_port=_env_int(environment, "ANITA_ADMIN_PORT", int(iso_raw.get("admin_port", DEFAULT_ADMIN_PORT))),
        streamlit_port=_env_int(
            environment, "ANITA_STREAMLIT_PORT",
            int(iso_raw.get("streamlit_port", DEFAULT_STREAMLIT_PORT)),
        ),
        log_dir=_env(environment, "ANITA_LOG_DIR", str(iso_raw.get("log_dir", DEFAULT_LOG_DIR))),
    )

    forbidden = ForbiddenValues(
        db_name=tuple(forb_raw.get("db_name", ForbiddenValues.db_name)),
        chroma_collection=tuple(forb_raw.get("chroma_collection", ForbiddenValues.chroma_collection)),
        port=tuple(int(p) for p in forb_raw.get("port", ForbiddenValues.port)),
        log_dir=tuple(forb_raw.get("log_dir", ForbiddenValues.log_dir)),
    )

    settings = AnitaSettings(
        isolation=isolation,
        forbidden=forbidden,
        identity=IdentitySettings(
            company_name=_env(environment, "ANITA_COMPANY_NAME", str(id_raw.get("company_name", IdentitySettings.company_name))),
            agent_name=_env(environment, "ANITA_AGENT_NAME", str(id_raw.get("agent_name", IdentitySettings.agent_name))),
        ),
        corpus=CorpusSettings(
            out_dir=str(corpus_raw.get("out_dir", CorpusSettings.out_dir)),
            chunks_file=str(corpus_raw.get("chunks_file", CorpusSettings.chunks_file)),
            sqlite_file=str(corpus_raw.get("sqlite_file", CorpusSettings.sqlite_file)),
        ),
        model=ModelSettings(
            llm_provider=_env(environment, "ANITA_LLM_PROVIDER", str(model_raw.get("llm_provider", ModelSettings.llm_provider))),
            ollama_url=_env(environment, "OLLAMA_URL", str(model_raw.get("ollama_url", ModelSettings.ollama_url))),
            ollama_model=_env(environment, "OLLAMA_MODEL", str(model_raw.get("ollama_model", ModelSettings.ollama_model))),
            ollama_num_ctx=_env_int(environment, "OLLAMA_NUM_CTX", int(model_raw.get("ollama_num_ctx", ModelSettings.ollama_num_ctx))),
            ollama_keep_alive=str(model_raw.get("ollama_keep_alive", ModelSettings.ollama_keep_alive)),
            embed_model=_env(environment, "EMBED_MODEL", str(model_raw.get("embed_model", ModelSettings.embed_model))),
            mlx_model=_env(environment, "MLX_MODEL", str(model_raw.get("mlx_model", ModelSettings.mlx_model))),
            mlx_base_url=_env(environment, "MLX_BASE_URL", str(model_raw.get("mlx_base_url", ModelSettings.mlx_base_url))),
            mlx_port=_env_int(environment, "MLX_PORT", int(model_raw.get("mlx_port", ModelSettings.mlx_port))),
        ),
        retrieval=RetrievalSettings(
            top_k=int(retr_raw.get("top_k", RetrievalSettings.top_k)),
            fetch_k=int(retr_raw.get("fetch_k", RetrievalSettings.fetch_k)),
            similarity_threshold=float(retr_raw.get("similarity_threshold", RetrievalSettings.similarity_threshold)),
        ),
        speech=SpeechSettings(
            whisper_model=_env(environment, "WHISPER_MODEL", str(speech_raw.get("whisper_model", SpeechSettings.whisper_model))),
            whisper_num_threads=_env_int(environment, "WHISPER_NUM_THREADS", int(speech_raw.get("whisper_num_threads", SpeechSettings.whisper_num_threads))),
            kokoro_voice=_env(environment, "KOKORO_VOICE", str(speech_raw.get("kokoro_voice", SpeechSettings.kokoro_voice))),
            kokoro_speed=_env_float(environment, "KOKORO_SPEED", float(speech_raw.get("kokoro_speed", SpeechSettings.kokoro_speed))),
            vad_silence_ms=_env_int(environment, "VAD_SILENCE_MS", int(speech_raw.get("vad_silence_ms", SpeechSettings.vad_silence_ms))),
            vad_speculative_advance_ms=_env_int(
                environment,
                "VAD_SPECULATIVE_ADVANCE_MS",
                int(speech_raw.get("vad_speculative_advance_ms", SpeechSettings.vad_speculative_advance_ms)),
            ),
            barge_in_enabled=_env_bool(environment, "BARGE_IN_ENABLED", bool(speech_raw.get("barge_in_enabled", SpeechSettings.barge_in_enabled))),
            tts_stream=_env_bool(environment, "TTS_STREAM", bool(speech_raw.get("tts_stream", SpeechSettings.tts_stream))),
            max_concurrent_calls=_env_int(environment, "MAX_CONCURRENT_CALLS", int(speech_raw.get("max_concurrent_calls", SpeechSettings.max_concurrent_calls))),
        ),
        sentiment=SentimentSettings(
            w1=_env_float(environment, "SENTIMENT_W1", float(sent_raw.get("w1", SentimentSettings.w1))),
            w2=_env_float(environment, "SENTIMENT_W2", float(sent_raw.get("w2", SentimentSettings.w2))),
            w3=_env_float(environment, "SENTIMENT_W3", float(sent_raw.get("w3", SentimentSettings.w3))),
            w4=_env_float(environment, "SENTIMENT_W4", float(sent_raw.get("w4", SentimentSettings.w4))),
            ewma_lambda=_env_float(environment, "SENTIMENT_EWMA_LAMBDA", float(sent_raw.get("ewma_lambda", SentimentSettings.ewma_lambda))),
            negative_escalation_threshold=_env_float(
                environment,
                "NEGATIVE_ESCALATION_THRESHOLD",
                float(sent_raw.get("negative_escalation_threshold", SentimentSettings.negative_escalation_threshold)),
            ),
        ),
        outbound=OutboundSettings(
            max_attempts=_env_int(environment, "OUTBOUND_MAX_ATTEMPTS", int(out_raw.get("max_attempts", OutboundSettings.max_attempts))),
            window_start=_env(environment, "OUTBOUND_WINDOW_START", str(out_raw.get("window_start", OutboundSettings.window_start))),
            window_end=_env(environment, "OUTBOUND_WINDOW_END", str(out_raw.get("window_end", OutboundSettings.window_end))),
            timezone=_env(environment, "OUTBOUND_TIMEZONE", str(out_raw.get("timezone", OutboundSettings.timezone))),
            poll_interval_seconds=_env_int(environment, "OUTBOUND_POLL_INTERVAL", int(out_raw.get("poll_interval_seconds", OutboundSettings.poll_interval_seconds))),
            retry_delays_minutes=_env_list_int(
                environment,
                "OUTBOUND_RETRY_DELAYS_MIN",
                tuple(int(x) for x in out_raw.get("retry_delays_minutes", OutboundSettings.retry_delays_minutes)),
            ),
        ),
        corpus_gate=CorpusGateSettings(
            expected_markers=tuple(gate_raw.get(
                "expected_markers", CorpusGateSettings.expected_markers)),
            blocked_markers=tuple(gate_raw.get(
                "blocked_markers", CorpusGateSettings.blocked_markers)),
        ),
        rag=RagSettings(
            backend=str(rag_raw.get("backend", RagSettings.backend)),
        ),
        release=ReleaseSettings(
            deferred_use_cases=tuple(
                (str(uc), str(reason))
                for uc, reason in (release_raw.get("deferred_use_cases") or {}).items()
            ),
        ),
        vram=VramSettings(
            shared_card_mib=_env_int(environment, "VRAM_SHARED_CARD_MIB", int(vram_raw.get("shared_card_mib", VramSettings.shared_card_mib))),
            anita_budget_mib=_env_int(environment, "VRAM_ANITA_BUDGET_MIB", int(vram_raw.get("anita_budget_mib", VramSettings.anita_budget_mib))),
            sibling_floor_mib=_env_int(environment, "VRAM_SIBLING_FLOOR_MIB", int(vram_raw.get("sibling_floor_mib", VramSettings.sibling_floor_mib))),
        ),
        project_root=root,
        env=environment,
    )
    _validate(settings)
    return settings


def _validate(settings: AnitaSettings) -> None:
    """Post-load invariants that are configuration errors, not parsing errors.

    These are values that parse perfectly well and are still wrong — the kind
    that would not crash anything, just quietly make the product behave badly.
    Raising here means they surface at boot with a named key rather than as a
    puzzling runtime behaviour.
    """
    from .retrieval import assert_threshold

    try:
        assert_threshold(settings.retrieval.similarity_threshold)
    except ValueError as exc:
        raise ConfigError("retrieval.similarity_threshold", str(exc)) from exc
