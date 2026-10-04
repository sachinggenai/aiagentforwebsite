"""Circuit breaker with probe single-flight.

Carried from the source (`rag_mcp.py:348-383`, `:424-463`) with two substantive
fixes, both cited defects:

* **B-8 — a failure threshold.** The source opens the circuit on a *single*
  failure, so one transient timeout disables the primary for a full cooldown.
  ``failure_threshold`` (default 3) now has to be reached.
* **CV-10 — atomic probe claiming.** The source's ``claim_probe`` checks then
  sets with no lock (`rag_mcp.py:448-463`); two callers can both claim the probe
  and the primary is hit twice. The claim here is under a lock.

State is **per instance**. The source's breaker is process-global with no tenant
key, so one product's failures would trip another's circuit if they shared a
process — which is exactly what this package exists to allow.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

CLOSED = "closed"
HALF_OPEN = "half_open"
OPEN = "open"
NOT_APPLICABLE = "off"


@dataclass
class BreakerState:
    """Public snapshot, so a caller never reads mutable internals."""

    state: str
    failure_count: int
    opens: int
    probes: int
    last_error: str | None
    seconds_until_probe: float

    def as_dict(self) -> dict:
        return {
            "breaker": self.state,
            "failure_count": self.failure_count,
            "breaker_opens": self.opens,
            "breaker_probes": self.probes,
            "last_error": self.last_error,
        }


@dataclass
class CircuitBreaker:
    """Three-state breaker over an optional primary transport.

    With no primary there is nothing to break, so the breaker reports
    ``"off"`` and is never consulted — a degeneracy that is **explicit** and
    tested rather than a mechanism that looks present and can never fire.
    """

    failure_threshold: int = 3
    cooldown_s: float = 30.0
    read_timeout_s: float = 6.0
    probe_fraction: float = 0.25
    probe_floor_s: float = 0.5
    enabled: bool = True

    _failures: int = field(default=0, init=False)
    _opens: int = field(default=0, init=False)
    _probes: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _last_error: str | None = field(default=None, init=False)
    _probe_in_flight_at: float | None = field(default=None, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)

    # -- state ------------------------------------------------------------
    def _abandoned_after(self) -> float:
        """A probe slot older than this is treated as abandoned.

        Without it, one path returning without a verdict stops the breaker from
        ever probing again — the source documents exactly this hazard
        (`rag_mcp.py:424-445`).
        """
        return self.read_timeout_s + 1.0

    def _clear_abandoned_locked(self) -> None:
        if self._probe_in_flight_at is None:
            return
        if time.monotonic() - self._probe_in_flight_at > self._abandoned_after():
            self._probe_in_flight_at = None

    @property
    def state(self) -> str:
        if not self.enabled:
            return NOT_APPLICABLE
        with self._lock:
            if self._opened_at is None:
                return CLOSED
            if time.monotonic() - self._opened_at >= self.cooldown_s:
                return HALF_OPEN
            return OPEN

    def snapshot(self) -> BreakerState:
        with self._lock:
            self._clear_abandoned_locked()
            remaining = 0.0
            if self._opened_at is not None:
                remaining = max(0.0, self.cooldown_s - (time.monotonic() - self._opened_at))
            return BreakerState(
                state=self.state,
                failure_count=self._failures,
                opens=self._opens,
                probes=self._probes,
                last_error=self._last_error,
                seconds_until_probe=remaining,
            )

    # -- admission ---------------------------------------------------------
    def admit(self) -> bool:
        """May the primary be tried right now?"""
        if not self.enabled:
            return False
        state = self.state
        if state == CLOSED:
            return True
        if state == HALF_OPEN:
            return self.claim_probe()
        return False

    def claim_probe(self) -> bool:
        """Claim the single half-open probe slot — atomically.

        The check and the set happen under one lock. The source's version does
        the check and the set as separate steps, so concurrent callers can both
        win (CV-10).
        """
        if not self.enabled:
            return False
        with self._lock:
            self._clear_abandoned_locked()
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at < self.cooldown_s:
                return False
            if self._probe_in_flight_at is not None:
                return False
            self._probe_in_flight_at = time.monotonic()
            self._probes += 1
            return True

    def release_probe(self) -> None:
        with self._lock:
            self._probe_in_flight_at = None

    def probe_read_timeout(self) -> float:
        return max(self.read_timeout_s * self.probe_fraction, self.probe_floor_s)

    # -- outcomes ----------------------------------------------------------
    def record_success(self) -> None:
        """A success closes the circuit and clears the failure count.

        Counters ``opens``/``probes`` are lifetime totals and are deliberately
        not reset — they are the evidence that the breaker is doing anything.
        """
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._last_error = None
            self._probe_in_flight_at = None

    def record_failure(self, error: str = "") -> None:
        """Count a failure; open only once the threshold is reached (B-8)."""
        with self._lock:
            self._failures += 1
            self._last_error = error or self._last_error
            self._probe_in_flight_at = None
            if self._failures >= self.failure_threshold and self._opened_at is None:
                self._opened_at = time.monotonic()
                self._opens += 1


__all__ = ["CircuitBreaker", "BreakerState", "CLOSED", "HALF_OPEN", "OPEN", "NOT_APPLICABLE"]
