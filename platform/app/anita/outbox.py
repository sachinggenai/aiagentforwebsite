"""The outbound send queue (US-002, TRD-04).

Every channel answers by *sending* something — a WhatsApp message, an extra-
channel webhook reply — and a send can fail. The failure that matters is not
the one where the provider says no: it is the one where the provider is
unreachable for forty seconds and the buyer's answer is dropped on the floor.

So a send is a row before it is a request (`BRD-14`, `REC-07`):

* **enqueue → claim → dispatch → settle.** The row exists before the carrier is
  touched, so a process that dies mid-send leaves a `QUEUED` row for the next
  boot rather than an answer nobody can prove was ever composed.
* **Five attempts, then `FAILED`.** 5 s, 30 s, 2 m, 8 m, 15 m. A failed row is
  never deleted (`TAC-4`) — it is the record that a buyer asked something and
  the carrier would not carry the answer. The console reads it (`AC-5`).
* **Transient and permanent are different verdicts.** A 503 is retried; an
  undeliverable `4xx` is `FAILED` immediately, because retrying a number that
  has blocked us is just noise addressed to someone who will never hear it
  (edge case 5, `T-26`).

**SQLite, not Postgres.** The story's DDL is Postgres dialect; this is the
dialect the platform actually runs (`channel_store` carries the same note).
The partial index translates: SQLite supports partial indexes since 3.8.

**One worker, one process.** `claim_due` has no `FOR UPDATE SKIP LOCKED` to
lean on, so the claim is safe because exactly one worker thread drains the
queue (`OutboxWorker`). If a second process is ever pointed at this store, the
claim moves to `UPDATE … RETURNING` inside a transaction — the seam is here,
named, rather than discovered later by a duplicate send.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

logger = logging.getLogger("anita.outbox")

QUEUED = "QUEUED"
RETRY = "RETRY"
SENT = "SENT"
FAILED = "FAILED"

# The documented schedule (`TAC-4`): 5 s, 30 s, 2 m, 8 m, 15 m. Its length is
# the retry budget — a sixth constant here and a sixth attempt there would be
# two ways to express one number, so the budget is derived from the schedule.
# The documented schedule (`TAC-4`, `CG-4`): exponential backoff from 5 s to a
# 15-minute ceiling, with a budget of 5 attempts.
#
# Five attempts means four waits, so the 15-minute value is the ceiling the
# series approaches — not a wait any send performs. The old tuple put the
# ceiling where a wait belonged, and `mark_retry` fails the row at the attempt
# budget anyway, so the fifth rung was scheduled by nothing (§8.26). The
# constants now say which is which.
BACKOFF_SECONDS: tuple[int, ...] = (5, 30, 120, 480)   # the waits actually applied
BACKOFF_CEILING_SECONDS = 900                          # 5 s → 15 min: the documented ceiling
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1                # 5 attempts: the send + 4 retries

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbound_send (
    id                  TEXT PRIMARY KEY,
    channel_kind        TEXT NOT NULL,
    conversation_id     TEXT,
    lead_id             TEXT,
    attempt             INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'QUEUED',
    payload             TEXT NOT NULL,
    next_attempt_at     TEXT NOT NULL,
    last_error          TEXT,
    provider_message_id TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outbound_send_due_idx
    ON outbound_send (next_attempt_at)
    WHERE status IN ('QUEUED', 'RETRY');
CREATE INDEX IF NOT EXISTS outbound_send_status_idx
    ON outbound_send (status, updated_at);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


def backoff(attempt: int) -> timedelta:
    """How long to wait after `attempt` failed.

    `attempt` is the 1-based number of the attempt that just failed. The series
    is exponential — 5 s, 30 s, 2 m, 8 m — and past its end the documented
    15-minute ceiling is held, so a caller asking about attempt 9 gets an
    answer rather than an IndexError. Within the budget the ceiling is never
    scheduled: the fourth wait is the last one before `mark_retry` fails the
    row at `MAX_ATTEMPTS`.
    """
    index = max(1, int(attempt)) - 1
    if index >= len(BACKOFF_SECONDS):
        return timedelta(seconds=BACKOFF_CEILING_SECONDS)
    return timedelta(seconds=BACKOFF_SECONDS[index])


class SendFailed(RuntimeError):
    """A dispatch failed. `transient` decides retry vs. dead-letter."""

    def __init__(self, detail: str, *, transient: bool = True,
                 status: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.transient = transient
        self.status = status


class Sender(Protocol):
    def send(self, payload: dict) -> str: ...


@dataclass
class OutboundSend:
    id: str
    channel_kind: str
    payload: dict
    attempt: int = 0
    status: str = QUEUED
    conversation_id: str | None = None
    lead_id: str | None = None
    next_attempt_at: str = ""
    last_error: str | None = None
    provider_message_id: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "OutboundSend":
        return cls(
            id=str(row["id"]),
            channel_kind=str(row["channel_kind"]),
            payload=json.loads(row["payload"]),
            attempt=int(row["attempt"]),
            status=str(row["status"]),
            conversation_id=row["conversation_id"],
            lead_id=row["lead_id"],
            next_attempt_at=str(row["next_attempt_at"]),
            last_error=row["last_error"],
            provider_message_id=row["provider_message_id"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "channel": self.channel_kind,
            "status": self.status,
            "attempt": self.attempt,
            "next_attempt_at": self.next_attempt_at,
            "last_error": self.last_error,
            "provider_message_id": self.provider_message_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class SendOutbox:
    """The durable send queue. One store, one worker (see the module note)."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(SCHEMA)
            self._connection.commit()

    # -- enqueue -----------------------------------------------------------
    def enqueue(
        self,
        *,
        channel_kind: str,
        payload: dict,
        conversation_id: str | None = None,
        lead_id: str | None = None,
        now: datetime | None = None,
    ) -> str:
        """Write the send down before any attempt to make it.

        `now` is injectable because the tests drive the schedule through
        simulated time; production never passes it.
        """
        moment = now or _now()
        send_id = str(uuid.uuid4())
        with self._lock:
            self._connection.execute(
                "INSERT INTO outbound_send (id, channel_kind, conversation_id, lead_id, "
                "attempt, status, payload, next_attempt_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
                (
                    send_id, channel_kind, conversation_id, lead_id, QUEUED,
                    json.dumps(payload), _iso(moment), _iso(moment), _iso(moment),
                ),
            )
            self._connection.commit()
        return send_id

    # -- claim -------------------------------------------------------------
    def claim_due(self, *, limit: int = 20, now: datetime | None = None) -> list[OutboundSend]:
        """Rows that are due. Claimed by returning them; see the module note."""
        moment = _iso(now or _now())
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM outbound_send WHERE status IN (?, ?) "
                "AND next_attempt_at <= ? ORDER BY next_attempt_at LIMIT ?",
                (QUEUED, RETRY, moment, int(limit)),
            ).fetchall()
        return [OutboundSend.from_row(row) for row in rows]

    # -- settle ------------------------------------------------------------
    def mark_sent(self, send_id: str, provider_message_id: str) -> None:
        self._update(
            send_id,
            status=SENT,
            provider_message_id=str(provider_message_id or ""),
            next_attempt_at="",
        )

    def mark_retry(self, send_id: str, error: str, *, now: datetime | None = None) -> str:
        """Record a failed attempt. Returns the status the row settled on.

        The attempt counter increments first, so "5 attempts" is counted by the
        store rather than by the caller — a worker that retried on a fresh
        counter after a restart would retry forever, and the bug would look
        like a busy queue rather than a bug.
        """
        moment = now or _now()
        with self._lock:
            row = self._connection.execute(
                "SELECT attempt FROM outbound_send WHERE id = ?", (send_id,)
            ).fetchone()
            if row is None:
                return ""
            attempt = int(row["attempt"]) + 1

            if attempt >= MAX_ATTEMPTS:
                self._connection.execute(
                    "UPDATE outbound_send SET attempt = ?, status = ?, last_error = ?, "
                    "updated_at = ? WHERE id = ?",
                    (attempt, FAILED, str(error)[:500], _iso(moment), send_id),
                )
                self._connection.commit()
                logger.warning(
                    "outbound send %s exhausted %d attempts; marked FAILED",
                    send_id[:8], MAX_ATTEMPTS,
                )
                return FAILED

            self._connection.execute(
                "UPDATE outbound_send SET attempt = ?, status = ?, last_error = ?, "
                "next_attempt_at = ?, updated_at = ? WHERE id = ?",
                (
                    attempt, RETRY, str(error)[:500],
                    _iso(moment + backoff(attempt)), _iso(moment), send_id,
                ),
            )
            self._connection.commit()
        return RETRY

    def mark_failed(self, send_id: str, error: str, *, now: datetime | None = None) -> None:
        """A permanent refusal. Never retried (`T-26`), never deleted."""
        self._update(send_id, status=FAILED, last_error=str(error)[:500],
                     next_attempt_at="", now=now)

    def _update(self, send_id: str, *, now: datetime | None = None, **fields) -> None:
        moment = _iso(now or _now())
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values())
        with self._lock:
            self._connection.execute(
                f"UPDATE outbound_send SET {assignments}, updated_at = ? WHERE id = ?",
                (*values, moment, send_id),
            )
            self._connection.commit()

    # -- read --------------------------------------------------------------
    def get(self, send_id: str) -> OutboundSend | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM outbound_send WHERE id = ?", (send_id,)
            ).fetchone()
        return OutboundSend.from_row(row) if row is not None else None

    def by_status(self, status: str, *, limit: int = 50) -> list[OutboundSend]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM outbound_send WHERE status = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (status, int(limit)),
            ).fetchall()
        return [OutboundSend.from_row(row) for row in rows]

    def counts(self) -> dict[str, int]:
        """How many sends sit in each state. Absence is not zero here either:
        a status with no rows is absent from the mapping, not a fabricated 0."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT status, COUNT(*) AS n FROM outbound_send GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}


def dispatch_now(
    outbox: "SendOutbox",
    sender: "Sender | None",
    *,
    channel_kind: str,
    payload: dict,
    conversation_id: str | None = None,
    lead_id: str | None = None,
) -> str:
    """Enqueue a send, then attempt it once. Returns the send row's id.

    The one place both ingress paths (WhatsApp, the extra channel) settle a
    reply, so "the row is written before the carrier is called" is a property
    of a single function rather than a habit two call sites have to keep.

    A `None` sender is not an error: it means dispatch is not configured, and
    the row correctly stays `QUEUED` for the worker and the console.
    """
    send_id = outbox.enqueue(
        channel_kind=channel_kind, payload=payload,
        conversation_id=conversation_id, lead_id=lead_id,
    )
    if sender is None:
        return send_id

    try:
        provider_id = sender.send(payload)
    except SendFailed as exc:
        if exc.transient:
            outbox.mark_retry(send_id, exc.detail)
        else:
            outbox.mark_failed(send_id, exc.detail)
    except Exception as exc:  # noqa: BLE001 - unknown ⇒ transient until proven otherwise
        outbox.mark_retry(send_id, f"{type(exc).__name__}: {exc}")
    else:
        outbox.mark_sent(send_id, provider_id)
    return send_id


class OutboxWorker:
    """Drains due sends. One thread, one store, no lock contention.
    `drain` is the whole behaviour and is what the tests call; `start`/`stop`
    only put it on a timer. That split is deliberate — a test that had to
    sleep for a poll interval to assert a retry would be testing the clock,
    not the queue.
    """

    def __init__(
        self,
        outbox: SendOutbox,
        sender: Sender,
        *,
        interval_s: float = 10.0,
        limit: int = 20,
        now: Callable[[], datetime] | None = None,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.outbox = outbox
        self.sender = sender
        self.interval_s = float(interval_s)
        self.limit = int(limit)
        self._now = now or _now
        self._on_event = on_event
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def drain(self) -> dict:
        """Dispatch every due send once. Returns per-outcome counts."""
        outcome = {"sent": 0, "retry": 0, "failed": 0, "skipped": 0}
        for send in self.outbox.claim_due(limit=self.limit, now=self._now()):
            try:
                provider_id = self.sender.send(send.payload)
            except SendFailed as exc:
                # The verdict is the sender's: only it knows whether the
                # carrier said "later" or "never".
                if exc.transient:
                    status = self.outbox.mark_retry(send.id, exc.detail, now=self._now())
                    outcome["retry" if status == RETRY else "failed"] += 1
                else:
                    self.outbox.mark_failed(send.id, exc.detail, now=self._now())
                    outcome["failed"] += 1
                self._emit("outbox.send_failed", {
                    "channel": send.channel_kind,
                    "attempt": send.attempt + 1,
                    "transient": exc.transient,
                })
            except Exception as exc:  # noqa: BLE001 - unknown is transient until proven otherwise
                self.outbox.mark_retry(
                    send.id, f"{type(exc).__name__}: {exc}", now=self._now()
                )
                outcome["retry"] += 1
            else:
                self.outbox.mark_sent(send.id, provider_id)
                outcome["sent"] += 1
                self._emit("outbox.sent", {"channel": send.channel_kind})
        return outcome

    def _emit(self, event: str, payload: dict) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event, payload)
        except Exception:  # noqa: BLE001 - telemetry never fails a send
            pass

    # -- the timer ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="anita-outbox", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.drain()
            except Exception:  # noqa: BLE001 - the queue survives a bad cycle
                logger.exception("outbox: drain failed; the next cycle retries")

    def stop(self) -> None:
        self._stop.set()
        self._thread = None


__all__ = [
    "BACKOFF_SECONDS", "FAILED", "MAX_ATTEMPTS", "QUEUED", "RETRY", "SENT",
    "OutboundSend", "OutboxWorker", "SendFailed", "SendOutbox", "Sender",
    "backoff", "dispatch_now",
]
