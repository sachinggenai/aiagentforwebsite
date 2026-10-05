"""MOD-02's store: conversations, identity links and turns (US-004, US-006).

Three tables, and the reason each exists:

* **`conversation`** — the thread a buyer's messages belong to. Keyed by the
  identity the channel could actually supply (`CV-02`): a phone number, a
  WhatsApp handle, a browser session token, or nothing at all. The row is what
  survives a restart, which is what makes `AC-2` ("a WhatsApp conversation
  survived a restart is resumed, not split") possible — the in-memory registry
  cannot answer that question after a deploy.
* **`identity_link`** — the same person arriving on two channels. Unique on
  `(identity_type, identity_value)`, so two channels linking one number
  concurrently converge by **constraint**, not by read-then-write (`TAC-4`).
* **`turn`** — every turn of every conversation, with its classification, route
  and answer reference, and a sequence number that is unique per conversation
  **by constraint**. The transcript is the handoff's payload (`BRD-11`) and the
  quality record behind `BRD-22`; an out-of-order or half-written transcript
  makes both unreliable.

**Where the serialisation actually happens.** `append_turn` runs inside
`BEGIN IMMEDIATE` and assigns `seq = MAX(seq)+1` for the conversation in the
same transaction as the insert. That is the SQLite equivalent of the story's
`SELECT … FOR UPDATE` on the conversation row: two writers cannot interleave
because the second blocks on the write lock, and the `UNIQUE (conversation_id,
seq)` constraint is the backstop that turns a logic error into an
`IntegrityError` rather than a duplicated sequence number.

**Deviations from the LLD, recorded rather than absorbed.** SQLite, not
Postgres (`CR-02` owns that swap); synchronous, not `async def` — every other
store in this platform is synchronous behind a lock, and an `async` facade over
`sqlite3` would be a colouring that buys nothing; and the review read path
shares the store's connection rather than using a separate pool (`TAC-7`).
The last one is a real difference: reads are serialised behind the same lock as
writes, so a 500-turn page read can add latency to a concurrent write. It is
acceptable at this platform's volume — one row lock, one process — and it is
listed here because "reads never block writes" is a claim the tests must not
make on this implementation's behalf.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Conversation lifecycle (`SM-02`).
ACTIVE = "ACTIVE"
IDLE = "IDLE"
CLOSED = "CLOSED"

# Turn lifecycle (`SM-02`): RECEIVED → CLASSIFIED → ANSWERED | FAILED.
RECEIVED = "RECEIVED"
CLASSIFIED = "CLASSIFIED"
ANSWERED = "ANSWERED"
FAILED = "FAILED"

INBOUND = "inbound"
OUTBOUND = "outbound"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation (
    id                     TEXT PRIMARY KEY,
    channel_kind           TEXT NOT NULL,
    identity_type          TEXT NOT NULL,
    identity_value         TEXT,
    lead_id                TEXT,
    parent_conversation_id TEXT,
    state                  TEXT NOT NULL DEFAULT 'ACTIVE',
    summary                TEXT,
    summary_updated_at     TEXT,
    transcript_expired_at  TEXT,
    started_at             TEXT NOT NULL,
    last_activity_at       TEXT NOT NULL,
    closed_at              TEXT
);
CREATE INDEX IF NOT EXISTS conversation_identity_idx
    ON conversation (identity_type, identity_value, last_activity_at DESC);
CREATE INDEX IF NOT EXISTS conversation_state_idx
    ON conversation (state, last_activity_at);

CREATE TABLE IF NOT EXISTS identity_link (
    id             TEXT PRIMARY KEY,
    identity_type  TEXT NOT NULL,
    identity_value TEXT NOT NULL,
    lead_id        TEXT NOT NULL,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    UNIQUE (identity_type, identity_value)
);

CREATE TABLE IF NOT EXISTS turn (
    id                TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL,
    seq               INTEGER NOT NULL,
    direction         TEXT NOT NULL,
    channel_kind      TEXT NOT NULL,
    provider_event_id TEXT,
    role              TEXT NOT NULL,
    text              TEXT,
    state             TEXT NOT NULL,
    classification    TEXT,
    route             TEXT,
    answer_ref        TEXT,
    grounding_refs    TEXT,
    latency_ms        INTEGER,
    failure_reason    TEXT,
    created_at        TEXT NOT NULL,
    UNIQUE (conversation_id, seq),
    UNIQUE (provider_event_id)
);
CREATE INDEX IF NOT EXISTS turn_conversation_seq_idx ON turn (conversation_id, seq);
CREATE INDEX IF NOT EXISTS turn_created_idx ON turn (created_at);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


@dataclass
class ConversationRecord:
    id: str
    channel_kind: str
    identity_type: str
    identity_value: str | None = None
    lead_id: str | None = None
    parent_conversation_id: str | None = None
    state: str = ACTIVE
    summary: str | None = None
    summary_updated_at: str | None = None
    transcript_expired_at: str | None = None
    started_at: str = ""
    last_activity_at: str = ""
    closed_at: str | None = None

    def as_dict(self) -> dict:
        return {
            "conversation_id": self.id,
            "channel": self.channel_kind,
            "identity_type": self.identity_type,
            "state": self.state,
            "lead_id": self.lead_id,
            "parent_conversation_id": self.parent_conversation_id,
            "summary": self.summary,
            "summary_updated_at": self.summary_updated_at,
            "transcript_expired_at": self.transcript_expired_at,
            "started_at": self.started_at,
            "last_activity_at": self.last_activity_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ConversationRecord":
        return cls(
            id=str(row["id"]),
            channel_kind=str(row["channel_kind"]),
            identity_type=str(row["identity_type"]),
            identity_value=row["identity_value"],
            lead_id=row["lead_id"],
            parent_conversation_id=row["parent_conversation_id"],
            state=str(row["state"]),
            summary=row["summary"],
            summary_updated_at=row["summary_updated_at"],
            transcript_expired_at=row["transcript_expired_at"],
            started_at=str(row["started_at"]),
            last_activity_at=str(row["last_activity_at"]),
            closed_at=row["closed_at"],
        )


@dataclass
class NewTurn:
    """A turn about to be written. `seq` is assigned by the store."""

    conversation_id: str
    role: str                       # 'user' | 'assistant'
    text: str
    channel_kind: str = "web_chat"
    state: str = RECEIVED
    classification: str | None = None
    route: str | None = None
    answer_ref: str | None = None
    grounding_refs: tuple[str, ...] = ()
    latency_ms: int | None = None
    failure_reason: str | None = None
    provider_event_id: str | None = None

    @property
    def direction(self) -> str:
        return INBOUND if self.role == "user" else OUTBOUND


@dataclass
class TurnRecord:
    id: str
    conversation_id: str
    seq: int
    direction: str
    role: str
    text: str
    channel_kind: str
    state: str
    classification: str | None = None
    route: str | None = None
    answer_ref: str | None = None
    grounding_refs: tuple[str, ...] = ()
    latency_ms: int | None = None
    failure_reason: str | None = None
    created_at: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "seq": self.seq,
            "role": self.role,
            "content": self.text,
            "channel": self.channel_kind,
            "state": self.state,
            "classification": self.classification,
            "route": self.route,
            "answer_ref": self.answer_ref,
            "grounding_refs": list(self.grounding_refs),
            "latency_ms": self.latency_ms,
            "failure_reason": self.failure_reason,
            "at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TurnRecord":
        try:
            refs = tuple(json.loads(row["grounding_refs"] or "[]"))
        except (TypeError, ValueError):
            refs = ()
        return cls(
            id=str(row["id"]),
            conversation_id=str(row["conversation_id"]),
            seq=int(row["seq"]),
            direction=str(row["direction"]),
            role=str(row["role"]),
            text=row["text"] or "",
            channel_kind=str(row["channel_kind"]),
            state=str(row["state"]),
            classification=row["classification"],
            route=row["route"],
            answer_ref=row["answer_ref"],
            grounding_refs=refs,
            latency_ms=row["latency_ms"],
            failure_reason=row["failure_reason"],
            created_at=str(row["created_at"]),
        )


class DuplicateTurn(Exception):
    """The provider event id already has a turn. Carries the stored one."""

    def __init__(self, turn: TurnRecord | None) -> None:
        super().__init__("duplicate provider event")
        self.turn = turn


class ConversationStore:
    """SQLite-backed, one connection, one process (see the module note)."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        # `isolation_level=None` puts the connection in autocommit mode so the
        # explicit BEGIN IMMEDIATE in `append_turn` is the transaction, rather
        # than a transaction the driver opened at some earlier statement.
        self._connection = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        if self.path != ":memory:":
            # WAL: readers do not block the writer on the same file, which
            # matters the moment the console reads while a turn is committed.
            self._connection.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._connection.executescript(SCHEMA)

    # -- conversations -----------------------------------------------------
    def create_conversation(
        self,
        *,
        conversation_id: str | None = None,
        channel_kind: str,
        identity_type: str,
        identity_value: str | None = None,
        lead_id: str | None = None,
        parent_conversation_id: str | None = None,
        now: datetime | None = None,
    ) -> ConversationRecord:
        moment = _iso(now or _now())
        record = ConversationRecord(
            id=conversation_id or str(uuid.uuid4()),
            channel_kind=channel_kind,
            identity_type=identity_type,
            identity_value=identity_value,
            lead_id=lead_id,
            parent_conversation_id=parent_conversation_id,
            state=ACTIVE,
            started_at=moment,
            last_activity_at=moment,
        )
        with self._lock:
            self._connection.execute(
                "INSERT INTO conversation (id, channel_kind, identity_type, "
                "identity_value, lead_id, parent_conversation_id, state, "
                "started_at, last_activity_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.id, record.channel_kind, record.identity_type,
                 record.identity_value, record.lead_id,
                 record.parent_conversation_id, record.state,
                 record.started_at, record.last_activity_at),
            )
        return record

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM conversation WHERE id = ?", (conversation_id,)
            ).fetchone()
        return ConversationRecord.from_row(row) if row is not None else None

    def find_recent(
        self,
        *,
        identity_type: str,
        identity_value: str,
        within_seconds: float | None = None,
        now: datetime | None = None,
    ) -> ConversationRecord | None:
        """The most recent conversation for this identity, optionally in-window.

        This is the database fallback that makes `AC-2` true: after a restart
        the registry is empty, and the only thing that knows the buyer was here
        20 minutes ago is this row.
        """
        moment = now or _now()
        query = ("SELECT * FROM conversation WHERE identity_type = ? "
                 "AND identity_value = ? ORDER BY last_activity_at DESC LIMIT 1")
        with self._lock:
            row = self._connection.execute(
                query, (identity_type, identity_value)
            ).fetchone()
        if row is None:
            return None
        record = ConversationRecord.from_row(row)
        if within_seconds is not None:
            try:
                last = datetime.fromisoformat(record.last_activity_at)
            except ValueError:
                return None
            if (moment - last) > timedelta(seconds=within_seconds):
                return None
        return record

    # `conversations_for_identity` was removed 2026-10-04 (§8.28): no caller
    # anywhere. `lead_for_identity` and `find_recent` cover the identity
    # lookups the product performs, and a second, unused query beside them is
    # the "looks wired, isn't" surface the sweep exists to remove.

    def touch(self, conversation_id: str, *, now: datetime | None = None) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE conversation SET last_activity_at = ?, "
                "state = CASE WHEN state = 'CLOSED' THEN state ELSE 'ACTIVE' END "
                "WHERE id = ?",
                (_iso(now or _now()), conversation_id),
            )

    def set_state(self, conversation_id: str, state: str, *,
                  now: datetime | None = None) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE conversation SET state = ?, "
                "closed_at = CASE WHEN ? = 'CLOSED' THEN ? ELSE closed_at END "
                "WHERE id = ?",
                (state, state, _iso(now or _now()), conversation_id),
            )

    def close(self, conversation_id: str, *, now: datetime | None = None) -> None:
        self.set_state(conversation_id, CLOSED, now=now)

    def set_lead(self, conversation_id: str, lead_id: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE conversation SET lead_id = ? WHERE id = ?",
                (lead_id, conversation_id),
            )

    def sweep_idle(self, *, idle_window_seconds: float,
                   now: datetime | None = None) -> list[str]:
        """Close conversations idle past the window. Returns the ids closed.

        The caller writes summaries for these — the store closes rows, the
        orchestrator owns what a summary says.
        """
        moment = now or _now()
        cutoff = _iso(moment - timedelta(seconds=idle_window_seconds))
        with self._lock:
            rows = self._connection.execute(
                "SELECT id FROM conversation WHERE state != 'CLOSED' "
                "AND last_activity_at <= ?", (cutoff,),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            for conversation_id in ids:
                self._connection.execute(
                    "UPDATE conversation SET state = 'CLOSED', closed_at = ? "
                    "WHERE id = ?", (_iso(moment), conversation_id),
                )
        return ids

    def write_summary(self, conversation_id: str, summary: str, *,
                      now: datetime | None = None) -> bool:
        """Idempotent by `summary_updated_at` (`TAC-5`).

        Two writers (the idle sweep and a teardown) racing produce one summary:
        the guard is the `WHERE summary_updated_at IS NULL`, so the loser is a
        no-op rather than an overwrite. Returns whether this call wrote it.
        """
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE conversation SET summary = ?, summary_updated_at = ? "
                "WHERE id = ? AND summary_updated_at IS NULL",
                (summary, _iso(now or _now()), conversation_id),
            )
        return cursor.rowcount > 0

    def unsummarised_closed(self, *, limit: int = 50) -> list[str]:
        """Closed conversations with no summary — what a sweep retries."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT id FROM conversation WHERE state = 'CLOSED' "
                "AND summary_updated_at IS NULL ORDER BY closed_at LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def transcripts_due_for_expiry(
        self, *, older_than_days: float, limit: int = 100,
        now: datetime | None = None,
    ) -> list[str]:
        """Conversations whose transcript is past the retention window.

        The read half of `expire_transcript`: that method removes one
        conversation's turns and stamps the date, and until now nothing ever
        called it, so the review's `expired` state was unreachable outside
        tests (§8.28). A conversation with no turns is not selected — there is
        nothing to expire, and stamping it would turn "nobody spoke" into
        "policy deleted this".
        """
        cutoff = _iso((now or _now()) - timedelta(days=float(older_than_days)))
        with self._lock:
            rows = self._connection.execute(
                "SELECT id FROM conversation WHERE last_activity_at < ? "
                "AND transcript_expired_at IS NULL AND EXISTS ("
                "  SELECT 1 FROM turn WHERE turn.conversation_id = conversation.id"
                ") ORDER BY last_activity_at LIMIT ?",
                (cutoff, int(limit)),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def expire_transcript(self, conversation_id: str, *,
                          now: datetime | None = None) -> int:
        """Retention hook for MOD-06 (`DG-11`): remove turns, keep the row.

        The conversation row keeps `transcript_expired_at` so the review screen
        can say the transcript expired *with its date* — an empty transcript and
        a deleted one look identical, and only one of them is a policy
        (`AC-7`, `UC-13` E1). Returns how many turns were removed.
        """
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM turn WHERE conversation_id = ?", (conversation_id,)
            )
            self._connection.execute(
                "UPDATE conversation SET transcript_expired_at = ? WHERE id = ?",
                (_iso(now or _now()), conversation_id),
            )
        return int(cursor.rowcount)

    # -- identity links ----------------------------------------------------
    def upsert_identity_link(
        self, *, identity_type: str, identity_value: str, lead_id: str,
        now: datetime | None = None,
    ) -> str:
        """Link an identity to a lead. The constraint decides conflicts (`TAC-4`).

        `ON CONFLICT DO UPDATE` on `last_seen` keeps the *first* lead — the
        unique constraint means a second channel's insert converges on the row
        that already exists rather than creating a second lead link. Returning
        the winning lead id is what lets the loser merge instead of duplicate.
        """
        moment = _iso(now or _now())
        with self._lock:
            self._connection.execute(
                "INSERT INTO identity_link (id, identity_type, identity_value, "
                "lead_id, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (identity_type, identity_value) DO UPDATE SET "
                "last_seen = excluded.last_seen",
                (str(uuid.uuid4()), identity_type, identity_value, lead_id,
                 moment, moment),
            )
            row = self._connection.execute(
                "SELECT lead_id FROM identity_link WHERE identity_type = ? "
                "AND identity_value = ?", (identity_type, identity_value),
            ).fetchone()
        return str(row["lead_id"]) if row is not None else lead_id

    def lead_for_identity(self, identity_type: str, identity_value: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT lead_id FROM identity_link WHERE identity_type = ? "
                "AND identity_value = ?", (identity_type, identity_value),
            ).fetchone()
        return str(row["lead_id"]) if row is not None else None

    # -- turns -------------------------------------------------------------
    def append_turn(self, turn: NewTurn, *, now: datetime | None = None) -> TurnRecord:
        """Append with an atomically assigned `seq` (see the module note)."""
        moment = _iso(now or _now())
        record_id = str(uuid.uuid4())
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if turn.provider_event_id:
                    existing = self._connection.execute(
                        "SELECT * FROM turn WHERE provider_event_id = ?",
                        (turn.provider_event_id,),
                    ).fetchone()
                    if existing is not None:
                        self._connection.execute("ROLLBACK")
                        raise DuplicateTurn(TurnRecord.from_row(existing))

                row = self._connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM turn "
                    "WHERE conversation_id = ?", (turn.conversation_id,),
                ).fetchone()
                seq = int(row["next"])

                self._connection.execute(
                    "INSERT INTO turn (id, conversation_id, seq, direction, "
                    "channel_kind, provider_event_id, role, text, state, "
                    "classification, route, answer_ref, grounding_refs, "
                    "latency_ms, failure_reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (record_id, turn.conversation_id, seq, turn.direction,
                     turn.channel_kind, turn.provider_event_id, turn.role,
                     turn.text, turn.state, turn.classification, turn.route,
                     turn.answer_ref, json.dumps(list(turn.grounding_refs)),
                     turn.latency_ms, turn.failure_reason, moment),
                )
                self._connection.execute(
                    "UPDATE conversation SET last_activity_at = ?, "
                    "state = CASE WHEN state = 'CLOSED' THEN state ELSE 'ACTIVE' END "
                    "WHERE id = ?", (moment, turn.conversation_id),
                )
                self._connection.execute("COMMIT")
            except DuplicateTurn:
                raise
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

        return TurnRecord(
            id=record_id, conversation_id=turn.conversation_id, seq=seq,
            direction=turn.direction, role=turn.role, text=turn.text,
            channel_kind=turn.channel_kind, state=turn.state,
            classification=turn.classification, route=turn.route,
            answer_ref=turn.answer_ref, grounding_refs=tuple(turn.grounding_refs),
            latency_ms=turn.latency_ms, failure_reason=turn.failure_reason,
            created_at=moment,
        )

    def by_provider_event(self, provider_event_id: str) -> TurnRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM turn WHERE provider_event_id = ?",
                (provider_event_id,),
            ).fetchone()
        return TurnRecord.from_row(row) if row is not None else None

    def page_turns(self, conversation_id: str, *, after_seq: int = 0,
                   limit: int = 100) -> list[TurnRecord]:
        """One page, oldest first. `after_seq` is the paging cursor (`AC-6`)."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM turn WHERE conversation_id = ? AND seq > ? "
                "ORDER BY seq LIMIT ?",
                (conversation_id, int(after_seq), int(limit)),
            ).fetchall()
        return [TurnRecord.from_row(row) for row in rows]

    def recent_turns(self, conversation_id: str, *, limit: int = 6) -> list[TurnRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM turn WHERE conversation_id = ? "
                "ORDER BY seq DESC LIMIT ?", (conversation_id, int(limit)),
            ).fetchall()
        return [TurnRecord.from_row(row) for row in reversed(rows)]

    def recent_turns_for_lead(self, lead_id: str, *, limit: int = 6,
                              exclude_conversation: str | None = None
                              ) -> list[TurnRecord]:
        """The tail of the lead's most recent conversation **that has turns**.

        This is what makes `AC-1`'s second clause true — "the products
        discussed on web chat are available in the voice conversation's
        context". The phone the buyer typed on chat links the two
        conversations through one lead; without this the lead would be shared
        but the conversation would start blank.

        "That has turns" is the load-bearing part: the most recent conversation
        for a lead is often the one being opened right now, or a call that
        ended before anyone said anything. Returning its (empty) transcript
        would silently discard the context this exists to carry.
        """
        with self._lock:
            query = ("SELECT id FROM conversation WHERE lead_id = ? "
                     + ("AND id != ? " if exclude_conversation else "")
                     + "ORDER BY last_activity_at DESC, started_at DESC LIMIT 20")
            params: tuple = ((lead_id, exclude_conversation)
                             if exclude_conversation else (lead_id,))
            candidates = [str(row["id"]) for row in
                          self._connection.execute(query, params).fetchall()]

            for conversation_id in candidates:
                rows = self._connection.execute(
                    "SELECT * FROM turn WHERE conversation_id = ? "
                    "ORDER BY seq DESC LIMIT ?", (conversation_id, int(limit)),
                ).fetchall()
                if rows:
                    return [TurnRecord.from_row(row) for row in reversed(rows)]
        return []

    def turn_count(self, conversation_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM turn WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return int(row["n"])

    def mark_failed(self, turn_id: str, reason: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE turn SET state = 'FAILED', failure_reason = ? WHERE id = ?",
                (str(reason)[:300], turn_id),
            )

    # -- review read model -------------------------------------------------
    def list_conversations(self, *, limit: int = 50, offset: int = 0
                           ) -> list[ConversationRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM conversation ORDER BY last_activity_at DESC "
                "LIMIT ? OFFSET ?", (int(limit), int(offset)),
            ).fetchall()
        return [ConversationRecord.from_row(row) for row in rows]

    def count_conversations(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM conversation"
            ).fetchone()
        return int(row["n"])

    def transcript_state(self, conversation_id: str) -> str:
        """`present` | `empty` | `expired` — three different facts (`AC-7`)."""
        record = self.get_conversation(conversation_id)
        if record is None:
            return "missing"
        if record.transcript_expired_at:
            return "expired"
        return "present" if self.turn_count(conversation_id) else "empty"


class ReplayQueue:
    """Bounded buffer for turns whose write failed (`TAC-3`, `AC-4`).

    Capacity 1,000, **drop-oldest beyond it, counted** — the story's decision.
    The alternative (drop-newest) loses the turn being answered right now,
    which is the one the buyer is looking at.
    """

    def __init__(self, capacity: int = 1000) -> None:
        self.capacity = int(capacity)
        self._items: deque[NewTurn] = deque()
        self._lock = threading.RLock()
        self.dropped = 0

    def put(self, turn: NewTurn) -> None:
        with self._lock:
            self._items.append(turn)
            while len(self._items) > self.capacity:
                self._items.popleft()
                self.dropped += 1

    def drain(self, store: ConversationStore) -> int:
        """Write everything buffered. Stops at the first failure (the store is
        still down); already-written turns are not re-queued."""
        written = 0
        while True:
            with self._lock:
                if not self._items:
                    return written
                turn = self._items[0]
            try:
                store.append_turn(turn)
            except DuplicateTurn:
                pass  # someone else wrote it; it is durable, which is the point
            except Exception:  # noqa: BLE001 - still unavailable; try again later
                return written
            with self._lock:
                self._items.popleft()
            written += 1

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._items)


class TurnRecorder:
    """The write seam the orchestrator uses: store first, buffer on failure.

    A persistence failure must never become a buyer-visible error (`AC-4`):
    the answer has already been composed, and refusing to deliver it because
    the database is down would punish the buyer for the operator's problem.
    """

    def __init__(self, store: ConversationStore | None = None,
                 replay: ReplayQueue | None = None,
                 on_event=None) -> None:
        self.store = store
        self.replay = replay or ReplayQueue()
        self._on_event = on_event
        self.replay_failures = 0

    @property
    def enabled(self) -> bool:
        return self.store is not None

    def record(self, turn: NewTurn) -> TurnRecord | None:
        """Persist, or buffer and return None. Never raises for a store fault."""
        if self.store is None:
            return None
        try:
            return self.store.append_turn(turn)
        except DuplicateTurn as duplicate:
            self._emit("turn.duplicate", {"conversation_id": turn.conversation_id})
            return duplicate.turn
        except Exception as exc:  # noqa: BLE001 - buffer and carry on (`AC-4`)
            self.replay_failures += 1
            self.replay.put(turn)
            self._emit("turn.buffered", {
                "conversation_id": turn.conversation_id,
                "error": type(exc).__name__,
                "pending": self.replay.pending,
            })
            return None

    def drain(self) -> int:
        if self.store is None:
            return 0
        return self.replay.drain(self.store)

    def _emit(self, event: str, payload: dict) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event, payload)
        except Exception:  # noqa: BLE001 - telemetry never fails a turn
            pass


__all__ = [
    "ACTIVE", "ANSWERED", "CLASSIFIED", "CLOSED", "ConversationRecord",
    "ConversationStore", "DuplicateTurn", "FAILED", "IDLE", "INBOUND",
    "NewTurn", "OUTBOUND", "RECEIVED", "ReplayQueue", "TurnRecorder",
    "TurnRecord",
]
