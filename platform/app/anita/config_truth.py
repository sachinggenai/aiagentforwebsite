"""Configuration read-coverage audit (TRD-41, DG-09).

**Stdlib-only by design.** This module must be importable and runnable before
any dependency is installed and before any other ANITA module is imported — it
is the check that runs when the stack will not start, so it cannot be part of
the thing that failed to start.

The question it answers: *is every configured key actually read by the code?*

A key that is written but never read is worse than a missing key. A missing key
fails loudly. An inert key looks configured, appears in the file, gets changed
by an operator during an incident — and does nothing. The source system had
exactly this defect (`FASTAPI_WORKERS` set but never read, and four inert RAG
keys), which is why this check exists here rather than as a later cleanup.

Two false-positive traps are handled explicitly, both learned from the source
implementation:

1. A key read through a typed helper (``_env_int(env, "PORT", ...)``) is a
   **read**. Matching only ``os.environ`` would flag every properly-typed key.
2. ``os.environ["K"] = value`` is a **write**, not a read. Matching the
   subscript form without excluding assignment flags the writer as a reader.

The pattern inventory is reported alongside the verdict. A checker whose rules
are invisible is a checker people learn to work around; showing which shapes
count as reads lets an operator see *why* a key was flagged.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from .secrets import is_sensitive, presence

# --------------------------------------------------------------------------
# Read-detection patterns. Order matters only for reporting clarity.
# --------------------------------------------------------------------------
READ_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Typed helpers: _env(env, "KEY", ...), _env_int(...), _env_bool(...) ...
    (
        "typed_helper",
        re.compile(
            r"""_(?:env|env_int|env_float|env_bool|env_list_int)\(\s*[^,()]+,\s*["']([A-Z][A-Z0-9_]*)["']"""
        ),
    ),
    # os.environ["KEY"] — but NOT when followed by an assignment.
    ("environ_subscript", re.compile(r"""os\.environ\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]""")),
    ("environ_get", re.compile(r"""os\.environ\.get\(\s*["']([A-Z][A-Z0-9_]*)["']""")),
    ("getenv", re.compile(r"""os\.getenv\(\s*["']([A-Z][A-Z0-9_]*)["']""")),
    # A settings-mapping read, e.g. env.get("KEY").
    #
    # The receiver list is deliberately short and explicit rather than a
    # catch-all `\w+\.get(...)`: a pattern that matched *any* mapping would
    # score `payload.get("STATUS")` as a read of a key called STATUS, and a
    # false LIVE is the one error this audit cannot afford — it would hide a
    # key nobody reads, which is precisely what the audit exists to find.
    #
    # `values` and `secrets` are here because the notifier and the extra-channel
    # ingress receive the environment under those names, and until 2026-10-04
    # their reads were invisible: `SMTP_*` and `SALES_NOTIFY_EMAIL` were
    # reported "pending (modules not yet built)" in every boot report (§8.29).
    (
        "mapping_get",
        re.compile(
            r"""\b(?:env|environment|environ|values|secrets)\.get\(\s*["']([A-Z][A-Z0-9_]*)["']"""
        ),
    ),
)

# Assignment to the environment is a write, never a read (false-positive trap 2).
WRITE_PATTERN = re.compile(
    r"""(?:os\.environ\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]|os\.environ\.setdefault\(\s*["']([A-Z][A-Z0-9_]*)["'])"""
    r"""\s*(?:=|\+=)"""
)

# Keys that are genuinely read, but through a level of indirection no regex can
# follow — so they must be named explicitly.
#
# An earlier version of this module treated *every* ``ANITA_``-prefixed key as
# structurally read, which quietly defeated the whole audit: any typo'd or
# abandoned key would have been reported LIVE. A checker that cannot fail is
# worse than no checker, so the exemption is now a short, explicit list that
# grows only when someone adds a genuinely indirect read.
STRUCTURAL_READ_KEYS: frozenset[str] = frozenset(
    {
        # Read via the REQUIRED_SECRETS tuple in boot.py (presence check),
        # which looks the key up by variable rather than by literal.
        "ANITA_ADMIN_TOKEN",
        # Read by the extra-channel ingress as `secrets.get(config.secret_env)`
        # — the key is a *value* from `config/anita.yaml` (`channels.extra.
        # secret_env`, shipped as this name), so no regex can follow it. The
        # read is real: an inbound without this credential is refused by HMAC.
        "ANITA_EXTRA_CHANNEL_SECRET",
    }
)


@dataclass
class KeyStatus:
    key: str
    state: str  # LIVE | INERT | PENDING
    read_by: list[str] = field(default_factory=list)
    presence: str = "unset"
    source: str = "env"

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "state": self.state,
            "read_by": self.read_by,
            "presence": self.presence,
            "source": self.source,
        }


@dataclass
class AuditReport:
    statuses: list[KeyStatus] = field(default_factory=list)
    pattern_inventory: list[str] = field(default_factory=list)
    files_scanned: int = 0

    @property
    def inert(self) -> list[str]:
        """Keys that are set for real but read by nothing — a live defect."""
        return [s.key for s in self.statuses if s.state == "INERT"]

    @property
    def pending(self) -> list[str]:
        """Keys declared in the template for modules not yet built.

        Not a defect. `.env.example` describes the finished platform; while
        modules are still landing, their keys are necessarily unread. Counting
        these as failures would make the audit cry wolf on every partial build,
        and a checker that always fails is a checker people switch off.
        """
        return [s.key for s in self.statuses if s.state == "PENDING"]

    @property
    def live(self) -> list[str]:
        return [s.key for s in self.statuses if s.state == "LIVE"]

    @property
    def ok(self) -> bool:
        """Healthy when nothing set-for-real is inert.

        The distinction that matters: a key present in the *environment* or in
        a real `.env` and read by nothing is a defect — someone configured
        something that does nothing. The same key in `.env.example` is just
        documentation of work not yet done.
        """
        return not self.inert

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "live": self.live,
            "inert": self.inert,
            "pending": self.pending,
            "pattern_inventory": self.pattern_inventory,
            "files_scanned": self.files_scanned,
            "keys": [s.as_dict() for s in self.statuses],
        }


def scan_reads(source_files: Iterable[Path]) -> tuple[dict[str, list[str]], int]:
    """Return ``{key: [pattern names that matched]}`` across the given files."""
    reads: dict[str, list[str]] = {}
    scanned = 0
    for path in source_files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1

        # Blank out assignment targets first so a write cannot masquerade as a
        # read (false-positive trap 2). Replacing with spaces preserves offsets
        # and keeps line-based diagnostics meaningful.
        write_spans: list[tuple[int, int]] = []
        for match in WRITE_PATTERN.finditer(text):
            write_spans.append((match.start(), match.end()))
        if write_spans:
            chars = list(text)
            for start, end in write_spans:
                for index in range(start, end):
                    if chars[index] != "\n":
                        chars[index] = " "
            text = "".join(chars)

        for name, pattern in READ_PATTERNS:
            for match in pattern.finditer(text):
                key = match.group(1)
                reads.setdefault(key, [])
                if name not in reads[key]:
                    reads[key].append(name)
    return reads, scanned


def collect_written_keys(
    env: Mapping[str, str] | None = None,
    env_files: Iterable[Path] | None = None,
) -> dict[str, tuple[str, str]]:
    """Every key written, as ``{key: (presence, source)}``.

    The source matters: a key set in the real environment is a commitment, while
    the same key in the template is a statement of intent. See
    :attr:`AuditReport.pending`.
    """
    written: dict[str, tuple[str, str]] = {}

    for key, value in (os.environ if env is None else env).items():
        if key.startswith("ANITA_") or key in _KNOWN_PLATFORM_KEYS:
            written[key] = (presence(key, value), "env")

    for path in env_files or []:
        # A template is not a commitment.
        source = "template" if path.name.endswith(".example") else "env_file"
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                continue
            # A real env file outranks the template for the same key.
            if written.get(key, ("", "template"))[1] == "env":
                continue
            written[key] = (presence(key, value.strip()), source)

    return written


# Keys the platform legitimately consumes that do not carry the ANITA_ prefix
# (they are shared vocabulary with the sibling product: Ollama, Twilio, SMTP).
_KNOWN_PLATFORM_KEYS: frozenset[str] = frozenset(
    {
        "OLLAMA_URL", "OLLAMA_MODEL", "OLLAMA_NUM_CTX", "OLLAMA_KEEP_ALIVE",
        "EMBED_MODEL", "MLX_BASE_URL", "MLX_MODEL", "MLX_PORT",
        "WHISPER_MODEL", "WHISPER_NUM_THREADS", "KOKORO_VOICE", "KOKORO_SPEED",
        "VAD_SILENCE_MS", "VAD_SPECULATIVE_ADVANCE_MS",
        "BARGE_IN_ENABLED", "TTS_STREAM", "MAX_CONCURRENT_CALLS",
        "SENTIMENT_W1", "SENTIMENT_W2", "SENTIMENT_W3", "SENTIMENT_W4",
        "SENTIMENT_EWMA_LAMBDA", "NEGATIVE_ESCALATION_THRESHOLD",
        "OUTBOUND_MAX_ATTEMPTS", "OUTBOUND_WINDOW_START", "OUTBOUND_WINDOW_END",
        "OUTBOUND_TIMEZONE", "OUTBOUND_POLL_INTERVAL", "OUTBOUND_RETRY_DELAYS_MIN",
        "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER",
        "TWILIO_WHATSAPP_NUMBER", "TUNNEL_HOST",
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SALES_NOTIFY_EMAIL",
        "DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DATABASE_URL",
        "VRAM_SHARED_CARD_MIB", "VRAM_ANITA_BUDGET_MIB", "VRAM_SIBLING_FLOOR_MIB",
    }
)


def audit_config(
    env: Mapping[str, str] | None = None,
    source_dir: Path | str | None = None,
    env_files: Iterable[Path] | None = None,
) -> AuditReport:
    """Audit every written key against the code that reads it."""
    root = Path(source_dir) if source_dir else Path(__file__).resolve().parent

    source_files = sorted(p for p in root.rglob("*.py") if p.is_file())
    reads, scanned = scan_reads(source_files)
    written = collect_written_keys(env, env_files)

    statuses: list[KeyStatus] = []
    for key in sorted(written):
        presence_value, source = written[key]
        readers = list(reads.get(key, []))
        if readers:
            state = "LIVE"
        elif key in STRUCTURAL_READ_KEYS:
            state = "LIVE"
            readers = ["declared_indirect_read"]
        elif source == "template" or key in _KNOWN_PLATFORM_KEYS:
            # Declared for a module that has not landed yet — documented, not
            # broken. Refusing here would make every partial build fail.
            #
            # `_KNOWN_PLATFORM_KEYS` is consulted as well as the source, because
            # where a key was *written* is not what decides whether the platform
            # expects it. The Twilio credentials copied into `.env` came from the
            # environment rather than the template, so they were classified
            # INERT — "set but never read" — when the truth was that the
            # platform declares them and the modules that read them (WhatsApp,
            # voice) are two of the deferred use cases. Where a value came from
            # is an accident of how it was installed; whether the platform
            # declares the key is the fact that matters.
            state = "PENDING"
        else:
            state = "INERT"
        statuses.append(
            KeyStatus(
                key=key,
                state=state,
                read_by=readers,
                presence=presence_value,
                source=source,
            )
        )

    return AuditReport(
        statuses=statuses,
        pattern_inventory=[name for name, _ in READ_PATTERNS] + ["declared_indirect_read"],
        files_scanned=scanned,
    )


def render_audit(report: AuditReport) -> str:
    """Render the audit as a machine-greppable, secret-free block."""
    lines = [
        "CONFIG AUDIT",
        f"  files_scanned : {report.files_scanned}",
        f"  live          : {len(report.live)}",
        f"  inert         : {len(report.inert)}",
        f"  pending       : {len(report.pending)}  (declared for modules not yet built)",
        f"  read patterns : {', '.join(report.pattern_inventory)}",
    ]
    for status in report.statuses:
        if status.state != "LIVE":
            # Presence only — never a value (TRD-44).
            lines.append(f"  {status.state:7} {status.key} ({status.presence})")
    if report.inert:
        lines.append("INERT_KEY: " + ", ".join(report.inert))
    return "\n".join(lines)
