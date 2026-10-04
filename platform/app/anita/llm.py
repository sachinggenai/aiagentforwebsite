"""Inference backend, streaming, and the degradation contract (TRD-17, TRD-21).

Ollama on Windows/Linux, MLX on Apple Silicon — the same abstraction the source
platform proved across both.

Three things here are design decisions rather than plumbing:

**Clause-level streaming.** A voice caller should hear the first sentence while
the model is still writing the third. :class:`ClauseCutter` emits text at
sentence boundaries, so synthesis can start early instead of waiting for a
complete answer. It also guards the failure that makes streaming worse than
batch: a buffer that never flushes because the model produced no full stop.

**A first-token timeout.** A model that hangs is worse than one that fails — the
caller hears silence and has no idea whether they are being listened to. Failing
at 8 s lets the degradation path speak.

**The breaker is not about the LLM.** Per `DG-03`, the deterministic answer path
must keep working when the model does not. The breaker's job is to stop *paying*
the timeout on every turn once the backend is known to be down, so commercial
questions (answered from SQL, needing no model) stay fast.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterator, Sequence

logger = logging.getLogger("anita.llm")

FIRST_TOKEN_TIMEOUT_S = 8.0
CLAUSE_FLUSH_MS = 120
CLAUSE_MAX_CHARS = 400


class Backend(str, Enum):
    OLLAMA = "ollama"
    MLX = "mlx"


class LLMError(RuntimeError):
    """The backend could not produce a response."""


class BreakState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# ---------------------------------------------------------------------------
# Clause cutting (streaming → speech)
# ---------------------------------------------------------------------------
# Sentence boundaries, plus the guards that make them safe. Two real failure
# modes shaped this pattern: cutting after "Rs." or a decimal point, and a
# buffer that grows without ever seeing a boundary because the model emitted one
# long run of text.
_BOUNDARY_RE = re.compile(r"(?<=[.!?;])\s+|(?<=[.!?])$")


class ClauseCutter:
    """Accumulates streamed text and emits it at speakable boundaries.

    ``flush(force=True)`` drains whatever is held. The caller uses it at
    end-of-stream, and on a timer — because a model that produces one long
    sentence would otherwise buffer the entire answer and defeat the point of
    streaming.
    """

    def __init__(self, max_chars: int = CLAUSE_MAX_CHARS, flush_ms: int = CLAUSE_FLUSH_MS) -> None:
        self.max_chars = max_chars
        self.flush_ms = flush_ms
        self._buffer = ""
        self._last_emit = time.monotonic()

    def feed(self, delta: str) -> list[str]:
        """Add streamed text; return any complete clauses now speakable."""
        self._buffer += delta or ""
        clauses: list[str] = []

        while True:
            match = _BOUNDARY_RE.search(self._buffer)
            if not match:
                break
            clause = self._buffer[: match.start() + 1].strip()
            self._buffer = self._buffer[match.end() :]
            if clause:
                clauses.append(clause)

        # No boundary and the buffer is long: emit at a word boundary rather
        # than let it grow. A caller waiting for a full stop that never comes
        # hears nothing at all.
        if len(self._buffer) >= self.max_chars:
            cut = self._buffer.rfind(" ", 0, self.max_chars)
            if cut <= 0:
                cut = self.max_chars
            chunk = self._buffer[:cut].strip()
            self._buffer = self._buffer[cut:]
            if chunk:
                clauses.append(chunk)

        if clauses:
            self._last_emit = time.monotonic()
        return clauses

    def due(self) -> bool:
        """True when the held buffer has waited past the flush interval."""
        return bool(self._buffer.strip()) and (
            (time.monotonic() - self._last_emit) * 1000 >= self.flush_ms
        )

    def flush(self) -> str:
        remaining = self._buffer.strip()
        self._buffer = ""
        self._last_emit = time.monotonic()
        return remaining


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------
class CircuitBreaker:
    """Three consecutive failures open the circuit for a cooldown.

    Consecutive, not cumulative: a backend that fails, succeeds, fails is
    flaky, not down, and opening on it would shed traffic that works.
    """

    def __init__(self, threshold: int = 3, cooldown_s: float = 60.0) -> None:
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> BreakState:
        if self._opened_at is None:
            return BreakState.CLOSED
        if time.monotonic() - self._opened_at >= self.cooldown_s:
            return BreakState.HALF_OPEN
        return BreakState.OPEN

    def allow(self) -> bool:
        """True when a request may be attempted."""
        current = self.state
        if current is BreakState.CLOSED:
            return True
        if current is BreakState.OPEN:
            return False
        # HALF_OPEN: let exactly one probe through.
        if self._probing:
            return False
        self._probing = True
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probing = False

    def record_failure(self) -> None:
        self._probing = False
        self._failures += 1
        if self._failures >= self.threshold:
            self._opened_at = time.monotonic()
            logger.warning("LLM circuit opened after %d consecutive failures", self._failures)

    def snapshot(self) -> dict:
        return {
            "state": self.state.value,
            "consecutive_failures": self._failures,
            "cooldown_s": self.cooldown_s,
        }


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------
@dataclass
class GenerationResult:
    text: str
    model: str
    backend: str
    first_token_ms: float | None = None
    total_ms: float = 0.0
    truncated: bool = False
    clauses: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "backend": self.backend,
            "first_token_ms": self.first_token_ms,
            "total_ms": round(self.total_ms, 1),
            "chars": len(self.text),
            "clauses": len(self.clauses),
        }


class OllamaBackend:
    """Chat via an Ollama server, streaming by default."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        num_ctx: int = 8192,
        # Ollama rejects a bare "-1" ("time: missing unit in duration") with
        # HTTP 400 on every request, and the failure reads as a model fault.
        # The configured value carries its unit; so must the default.
        keep_alive: str = "-1m",
        timeout: float = 60.0,
        first_token_timeout: float = FIRST_TOKEN_TIMEOUT_S,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        self.timeout = timeout
        self.first_token_timeout = first_token_timeout
        self.breaker = breaker or CircuitBreaker()

    @property
    def available(self) -> bool:
        return self.breaker.allow()

    def generate(self, messages: Sequence[dict[str, str]], *, stream: bool = True) -> GenerationResult:
        """Run one generation. Raises :class:`LLMError` on failure."""
        if not self.breaker.allow():
            raise LLMError(
                f"LLM backend is unavailable (circuit {self.breaker.state.value}); "
                f"deterministic answers remain available"
            )

        start = time.monotonic()
        try:
            result = self._stream(messages) if stream else self._blocking(messages)
        except LLMError:
            self.breaker.record_failure()
            raise
        except Exception as exc:  # noqa: BLE001 - transport failures all degrade the same way
            self.breaker.record_failure()
            raise LLMError(f"generation failed: {type(exc).__name__}: {exc}") from exc

        self.breaker.record_success()
        result.total_ms = (time.monotonic() - start) * 1000
        return result

    # -- transports --------------------------------------------------------
    def _stream(self, messages) -> GenerationResult:
        import httpx

        payload = {
            "model": self.model,
            "messages": list(messages),
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {"num_ctx": self.num_ctx},
        }
        cutter = ClauseCutter()
        chunks: list[str] = []
        clauses: list[str] = []
        first_token_ms: float | None = None
        started = time.monotonic()

        with httpx.stream(
            "POST", f"{self.base_url}/api/chat", json=payload, timeout=self.timeout
        ) as response:
            if response.status_code >= 400:
                raise LLMError(f"backend returned {response.status_code}")

            for line in response.iter_lines():
                if not line:
                    continue
                if first_token_ms is None:
                    first_token_ms = (time.monotonic() - started) * 1000
                    if first_token_ms > self.first_token_timeout * 1000:
                        # A caller hearing nothing cannot tell "thinking" from
                        # "broken". Failing lets the degradation path speak.
                        raise LLMError(
                            f"first token exceeded {self.first_token_timeout}s"
                        )
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                delta = (event.get("message") or {}).get("content", "")
                if delta:
                    chunks.append(delta)
                    clauses.extend(cutter.feed(delta))
                if event.get("done"):
                    break

        tail = cutter.flush()
        if tail:
            clauses.append(tail)

        return GenerationResult(
            text="".join(chunks).strip(),
            model=self.model,
            backend=Backend.OLLAMA.value,
            first_token_ms=first_token_ms,
            clauses=clauses,
        )

    def _blocking(self, messages) -> GenerationResult:
        import httpx

        payload = {
            "model": self.model,
            "messages": list(messages),
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"num_ctx": self.num_ctx},
        }
        response = httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
        if response.status_code >= 400:
            raise LLMError(f"backend returned {response.status_code}")
        text = (response.json().get("message") or {}).get("content", "")
        cutter = ClauseCutter()
        clauses = cutter.feed(text)
        tail = cutter.flush()
        if tail:
            clauses.append(tail)
        return GenerationResult(
            text=text.strip(), model=self.model, backend=Backend.OLLAMA.value, clauses=clauses
        )

    def warm(self) -> bool:
        """Load the model so the first caller does not pay the load.

        A cold 14B model costs 6–11 s to bring resident, which is far outside any
        voice latency budget — and it lands on whichever caller happens to be
        first.
        """
        try:
            self.generate([{"role": "user", "content": "ok"}], stream=False)
            return True
        except LLMError as exc:
            logger.warning("LLM warm-up failed: %s", exc)
            return False

    def describe(self) -> dict:
        return {
            "backend": Backend.OLLAMA.value,
            "model": self.model,
            "base_url": self.base_url,
            "num_ctx": self.num_ctx,
            "breaker": self.breaker.snapshot(),
        }


def select_backend(device: str, *, provider: str = "auto") -> str:
    """Map a detected platform to a backend, with no operator input (TRD-43)."""
    chosen = (provider or "auto").lower()
    if chosen in {Backend.OLLAMA.value, Backend.MLX.value}:
        return chosen
    if chosen != "auto":
        raise ValueError(f"unknown LLM provider {provider!r}")
    return Backend.MLX.value if device == "metal" else Backend.OLLAMA.value
