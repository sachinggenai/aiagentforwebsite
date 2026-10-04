"""The channel-facing tables: endpoints, inbound events and sessions (US-001).

Three tables, each earning its place by making a failure impossible rather than
unlikely:

* **`inbound_event`** — replay protection. Twilio retries a webhook it believes
  failed, and a replayed `CallSid` must not open a second media stream or start
  a second conversation. The primary key is the provider's own event id, so the
  guarantee is the database's, not a race the application hopes to win — the
  same reasoning as the unique partial indexes on leads (`MOD-06`).
* **`channel_endpoint`** — attribution. A signature can be valid while the
  number called belongs to a different product; without this the instance
  answers for a number it does not own (`CG-1`, `BRD-03`, edge case 14).
* **`channel_session`** — the conversation a call was, keyed on the stream SID
  so a call whose caller withheld their number still has a stable identity
  (`AC-6`).

**SQLite, not Postgres.** US-001's LLD gives this DDL in Postgres dialect
(`uuid`, `jsonb`, `timestamptz`, `gen_random_uuid()`). The platform does not run
Postgres yet: CR-02 owns that swap and is deliberately blocked on decisions
Q9–Q12, with the recorded constraint that changing the persistence layer and
the modules at once makes every regression ambiguous. So these are the same
tables in the dialect the platform actually runs, behind the same seam the
`CrmStore` Protocol already established — when CR-02 lands, this is one of the
stores that moves, not one that gets rewritten.

Two deliberate differences from the LLD, recorded rather than absorbed:

* `endpoint_id` is **nullable**. The story has it `NOT NULL REFERENCES
  channel_endpoint(id)`, but an inbound event from an unrecognised number is
  exactly the one that must be *recorded as rejected*, and it has no endpoint to
  point at. A placeholder row would put a fictional endpoint in the table.
* `disposition` is the reason the row exists rather than a mutable status.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

# The dispositions an inbound event can settle on. `accepted` means this
# process is handling it; `duplicate` means an earlier delivery already did;
# `rejected` means it was refused and the row is the audit record (there is no
# silent drop).
ACCEPTED = "accepted"
DUPLICATE = "duplicate"
REJECTED = "rejected"

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_endpoint (
    id            TEXT PRIMARY KEY,
    channel_kind  TEXT NOT NULL,
    provider      TEXT NOT NULL,
    address       TEXT NOT NULL,
    secret_ref    TEXT,
    capabilities  TEXT NOT NULL DEFAULT '{}',
    enabled       INTEGER NOT NULL DEFAULT 1,
    instance_id   TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (channel_kind, address, instance_id)
);

CREATE TABLE IF NOT EXISTS inbound_event (
    provider_event_id TEXT PRIMARY KEY,
    endpoint_id       TEXT,
    channel_kind      TEXT NOT NULL,
    received_at       TEXT NOT NULL,
    disposition       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbound_event_received ON inbound_event (received_at);

CREATE TABLE IF NOT EXISTS channel_session (
    id               TEXT PRIMARY KEY,
    channel_kind     TEXT NOT NULL,
    session_key      TEXT NOT NULL,
    conversation_id  TEXT,
    started_at       TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    closed_at        TEXT,
    UNIQUE (channel_kind, session_key)
);
CREATE INDEX IF NOT EXISTS idx_channel_session_open
    ON channel_session (channel_kind, closed_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChannelStore:
    """SQLite-backed, like every other store in the platform today."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(SCHEMA)
            self._connection.commit()

    # -- endpoints ---------------------------------------------------------
    def register_endpoint(
        self,
        *,
        channel_kind: str,
        provider: str,
        address: str,
        instance_id: str,
        secret_ref: str | None = None,
        capabilities: dict | None = None,
    ) -> str:
        """Declare that this instance answers `address` on `channel_kind`.

        Idempotent: registering the same endpoint twice returns the existing id
        rather than raising, because the call happens on every boot.
        """
        with self._lock:
            existing = self._connection.execute(
                "SELECT id FROM channel_endpoint WHERE channel_kind = ? "
                "AND address = ? AND instance_id = ?",
                (channel_kind, address, instance_id),
            ).fetchone()
            if existing is not None:
                return str(existing["id"])

            endpoint_id = str(uuid.uuid4())
            self._connection.execute(
                "INSERT INTO channel_endpoint (id, channel_kind, provider, address, "
                "secret_ref, capabilities, enabled, instance_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    endpoint_id, channel_kind, provider, address, secret_ref,
                    json.dumps(capabilities or {}), instance_id, _now(),
                ),
            )
            self._connection.commit()
            return endpoint_id

    def find_endpoint(
        self, *, channel_kind: str, address: str, instance_id: str
    ) -> dict | None:
        """The endpoint for this address, or None when this instance does not
        answer it. None is the caller's cue to refuse (edge case 14)."""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM channel_endpoint WHERE channel_kind = ? "
                "AND address = ? AND instance_id = ? AND enabled = 1",
                (channel_kind, address, instance_id),
            ).fetchone()
        return dict(row) if row is not None else None

    # -- inbound events (replay protection) --------------------------------
    def claim_event(
        self, provider_event_id: str, *, channel_kind: str, endpoint_id: str | None = None
    ) -> str:
        """Claim a provider event. Returns `accepted` or `duplicate`.

        The `INSERT` is the check and the claim in one statement: two concurrent
        deliveries of the same `CallSid` cannot both succeed, because the primary
        key decides. Reading first and inserting after would leave exactly the
        window that a replay arrives in.
        """
        if not provider_event_id:
            # Nothing to deduplicate against. Recording an empty key would make
            # the *next* un-identified event a false duplicate.
            return ACCEPTED

        with self._lock:
            try:
                self._connection.execute(
                    "INSERT INTO inbound_event (provider_event_id, endpoint_id, "
                    "channel_kind, received_at, disposition) VALUES (?, ?, ?, ?, ?)",
                    (provider_event_id, endpoint_id, channel_kind, _now(), ACCEPTED),
                )
                self._connection.commit()
                return ACCEPTED
            except sqlite3.IntegrityError:
                return DUPLICATE

    def mark_rejected(self, provider_event_id: str, *, channel_kind: str) -> None:
        """Record a refused inbound event.

        The audit record for a rejection. Written even when the caller is told
        nothing, because "no silent drop" is the point of the table.
        """
        if not provider_event_id:
            return
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO inbound_event (provider_event_id, endpoint_id, "
                "channel_kind, received_at, disposition) VALUES (?, NULL, ?, ?, ?)",
                (provider_event_id, channel_kind, _now(), REJECTED),
            )
            self._connection.commit()

    def event_disposition(self, provider_event_id: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT disposition FROM inbound_event WHERE provider_event_id = ?",
                (provider_event_id,),
            ).fetchone()
        return str(row["disposition"]) if row is not None else None

    # -- sessions ----------------------------------------------------------
    def open_session(
        self, *, channel_kind: str, session_key: str, conversation_id: str | None = None
    ) -> str:
        """Key a call on its stream id. Idempotent (`AC-6`).

        For a caller who withholds their number this is the *only* stable
        identity the call has, which is why it is the session key rather than
        the phone number.
        """
        with self._lock:
            existing = self._connection.execute(
                "SELECT id FROM channel_session WHERE channel_kind = ? AND session_key = ?",
                (channel_kind, session_key),
            ).fetchone()
            if existing is not None:
                return str(existing["id"])

            session_id = str(uuid.uuid4())
            now = _now()
            self._connection.execute(
                "INSERT INTO channel_session (id, channel_kind, session_key, "
                "conversation_id, started_at, last_activity_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, channel_kind, session_key, conversation_id, now, now),
            )
            self._connection.commit()
            return session_id

    def touch_session(self, *, channel_kind: str, session_key: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE channel_session SET last_activity_at = ? "
                "WHERE channel_kind = ? AND session_key = ?",
                (_now(), channel_kind, session_key),
            )
            self._connection.commit()

    def close_session(self, *, channel_kind: str, session_key: str) -> None:
        """Idempotent: a call can end by `stop`, by socket drop, or by both."""
        with self._lock:
            self._connection.execute(
                "UPDATE channel_session SET closed_at = ? "
                "WHERE channel_kind = ? AND session_key = ? AND closed_at IS NULL",
                (_now(), channel_kind, session_key),
            )
            self._connection.commit()

    def find_session(self, *, channel_kind: str, session_key: str) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM channel_session WHERE channel_kind = ? AND session_key = ?",
                (channel_kind, session_key),
            ).fetchone()
        return dict(row) if row is not None else None

    def open_sessions(self, *, channel_kind: str | None = None) -> list[dict]:
        """Sessions that have not been closed — what a restart must reconcile.

        A closed socket does not always mean a closed session: a process that
        dies mid-call leaves the row open, and an operator needs to see that
        rather than infer it from absence.
        """
        query = "SELECT * FROM channel_session WHERE closed_at IS NULL"
        params: tuple = ()
        if channel_kind:
            query += " AND channel_kind = ?"
            params = (channel_kind,)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]
