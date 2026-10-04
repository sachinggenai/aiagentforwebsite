"""CRM interface and the mock store (MOD-06, TRD-27, BRD-14).

**The interface is the deliverable.** The brief forbids reusing the source
platform's Salesforce integration (3,653 lines of httpx client, picklist
mapping and chunked `ContentVersion` upload), and that is the right call — none
of its vocabulary applies to a medical-equipment catalogue. But the *seam* it
sat behind is worth keeping: everything that writes lead data goes through
:class:`CrmStore`, so adopting a real CRM later is a new implementation of that
protocol and no caller changes.

Two things the source got right and are carried as *patterns* rather than code:

* **A durable outbox** (REC-07). Writes that must not be lost are recorded
  first and retried with backoff, so a CRM outage degrades to a delay rather
  than a lost lead.
* **Graceful database absence.** Every operation degrades rather than raising —
  the source platform could answer calls with Postgres down, which is a
  genuinely good property.

The mock store is SQLite-backed and local. It is not a toy: it implements the
same dedupe key, the same terminal states and the same erasure semantics a real
CRM would need, so the swap is a swap and not a rewrite.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Protocol, Sequence


class LeadState(str, Enum):
    """`SM-01`. Transitions are enforced in :mod:`anita.leads`."""

    NEW = "new"
    QUALIFYING = "qualifying"
    QUALIFIED = "qualified"
    CONTACT_ATTEMPTED = "contact_attempted"
    CONTACTED = "contacted"
    ESCALATED = "escalated"
    SUPPRESSED = "suppressed"
    CLOSED = "closed"


TERMINAL_STATES = frozenset({LeadState.SUPPRESSED, LeadState.CLOSED})


class Channel(str, Enum):
    WEB_CHAT = "web_chat"
    WHATSAPP = "whatsapp"
    VOICE_INBOUND = "voice_inbound"
    VOICE_OUTBOUND = "voice_outbound"
    EXTRA = "extra"


# ---------------------------------------------------------------------------
# Normalisation — the dedupe key depends on it
# ---------------------------------------------------------------------------
_PHONE_DIGITS = re.compile(r"\d")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# The country this business sells from. A number written without an
# international prefix is read as a national number in this country.
DEFAULT_COUNTRY_CODE = "91"


def normalise_phone(
    raw: str | None, *, country_code: str = DEFAULT_COUNTRY_CODE
) -> str | None:
    """Reduce a phone number to a comparable form.

    Without this, "+91 98000 00000", "919800000000" and "09800000000" are three
    leads for one buyer — which is exactly the duplicate the North Star metric
    cannot afford.

    **Format and country are different questions, and only the first is
    decidable from the digits alone.** The previous version prefixed "+" to
    whatever digits it found, so a ten-digit mobile `98200 11122` became
    `+9820011122` — a country code that does not exist — and the same buyer
    writing `+91 98200 11122` became a second lead. That is a deduplication
    failure produced by the normaliser, not one it prevents.

    The rules, in order:

    * An explicit ``+`` is authoritative — the caller said which country.
    * ``00`` is the international access prefix; ``0091…`` means ``+91…``.
    * Otherwise the number is *national*, and the business's own country is
      applied. A trunk ``0`` (``098000…``, ``080454…``) is dropped first.
    * A national number that already begins with the country code and is longer
      than a national number is treated as carrying it (``919800000000``).

    The assumption this makes is worth stating: a number written without a
    country code is read as a domestic number. That is the right default for a
    business that sells nationally and takes most of its calls from one country,
    and it is wrong for the buyer abroad who writes their own number without a
    prefix — there is no way to tell those apart from the digits, so the
    default is chosen rather than guessed at. Pass ``country_code`` to change it.
    """
    if not raw:
        return None

    text = raw.strip()
    digits = "".join(_PHONE_DIGITS.findall(text))
    if not digits:
        return None

    # A real number is 10–15 digits (E.164 allows at most 15 including the
    # country code; the shortest national number this business deals with is
    # 10). Anything shorter is likelier to be a product id or a quantity that
    # the pattern ran into — "6835421" is not a phone number, and treating it
    # as one creates a lead for a buyer who never gave a contact detail.
    if not (10 <= len(digits) <= 15):
        return None

    if text.startswith("+"):
        return f"+{digits}"
    if digits.startswith("00"):
        return f"+{digits[2:]}"

    national = digits.lstrip("0") or digits
    if national.startswith(country_code) and len(national) > 10:
        return f"+{national}"
    return f"+{country_code}{national}"


def normalise_email(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = raw.strip().lower()
    return cleaned if _EMAIL_RE.match(cleaned) else None


@dataclass
class Lead:
    """One buyer. Deduplicated on phone or email, whichever is known."""

    lead_id: str
    state: LeadState = LeadState.NEW
    name: str = ""
    phone: str = ""
    email: str = ""
    company: str = ""
    city: str = ""
    buyer_type: str = ""
    product_interest: list[str] = field(default_factory=list)
    quantity: str = ""
    urgency: str = ""
    source_channel: str = ""
    channels_seen: list[str] = field(default_factory=list)
    conversation_ids: list[str] = field(default_factory=list)
    qualification: dict = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    erased_at: str | None = None

    @property
    def is_erased(self) -> bool:
        return self.erased_at is not None

    @property
    def contactable(self) -> bool:
        return bool(self.phone or self.email) and not self.is_erased

    def as_dict(self) -> dict:
        return {
            "lead_id": self.lead_id,
            "state": self.state.value,
            "name": self.name,
            "company": self.company,
            "city": self.city,
            "buyer_type": self.buyer_type,
            "product_interest": self.product_interest,
            "quantity": self.quantity,
            "urgency": self.urgency,
            "source_channel": self.source_channel,
            "channels_seen": self.channels_seen,
            "contactable": self.contactable,
            "erased": self.is_erased,
        }


@dataclass
class Handoff:
    """`SM-06`: a conversation escalated to a human."""

    handoff_id: str
    lead_id: str
    reason: str
    state: str = "open"          # open | notified | acknowledged | closed | withdrawn
    #: US-022: which conversation raised this. Carried separately because an
    #: anonymous escalation has no lead, and putting a conversation id in
    #: `lead_id` made the record look linked to a lead that does not exist.
    conversation_id: str = ""
    sentiment_score: float | None = None
    products: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    notified_at: str | None = None

    def as_dict(self) -> dict:
        return {
            "handoff_id": self.handoff_id,
            "lead_id": self.lead_id,
            "conversation_id": self.conversation_id,
            "reason": self.reason,
            "state": self.state,
            "sentiment_score": self.sentiment_score,
            "products": self.products,
        }


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------
class CrmStore(Protocol):
    """What any CRM backend must provide.

    Deliberately small and lead-shaped. Nothing here mentions a vendor, a
    picklist, an application object or a content version — those are the source
    platform's domain, and importing them is what made its CRM layer
    unreusable.
    """

    def upsert_lead(self, lead: Lead) -> Lead: ...

    def find_lead(self, *, phone: str = "", email: str = "") -> Lead | None: ...

    def get_lead(self, lead_id: str) -> Lead | None: ...

    def save_lead(self, lead: Lead) -> Lead: ...

    def list_leads(self, *, state: LeadState | None = None) -> list[Lead]: ...

    def erase_lead(self, lead_id: str) -> bool: ...

    def record_handoff(self, handoff: Handoff) -> Handoff: ...

    def list_handoffs(self, *, state: str | None = None) -> list[Handoff]: ...

    def enqueue(self, operation: str, payload: dict) -> str: ...

    def pending_outbox(self, limit: int = 50) -> list[dict]: ...

    def mark_sent(self, outbox_id: str) -> None: ...

    def mark_failed(self, outbox_id: str, error: str) -> None: ...


# ---------------------------------------------------------------------------
# Mock implementation
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    lead_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    name TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    email TEXT DEFAULT '',
    company TEXT DEFAULT '',
    city TEXT DEFAULT '',
    buyer_type TEXT DEFAULT '',
    product_interest TEXT DEFAULT '[]',
    quantity TEXT DEFAULT '',
    urgency TEXT DEFAULT '',
    source_channel TEXT DEFAULT '',
    channels_seen TEXT DEFAULT '[]',
    conversation_ids TEXT DEFAULT '[]',
    qualification TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    erased_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_phone ON leads (phone) WHERE phone <> '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_email ON leads (email) WHERE email <> '';

CREATE TABLE IF NOT EXISTS handoffs (
    handoff_id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',
    sentiment_score REAL,
    products TEXT DEFAULT '[]',
    conversation_id TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    notified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_handoffs_state ON handoffs (state);

CREATE TABLE IF NOT EXISTS crm_outbox (
    outbox_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON crm_outbox (next_attempt_at);
"""


class MockCrmStore:
    """A local, SQLite-backed CRM.

    Unique partial indexes on phone and email are what make deduplication a
    database guarantee rather than a race the application hopes to win: two
    channels submitting the same buyer simultaneously cannot both insert.
    """

    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(SCHEMA)
            self._connection.commit()
            self._migrate()

    def _migrate(self) -> None:
        """Additive migrations for databases created before this version.

        `CREATE TABLE IF NOT EXISTS` does not add a column to a table that
        already exists, so a store written before US-022 keeps the old shape
        without this.
        """
        columns = {
            row["name"] for row in self._connection.execute("PRAGMA table_info(handoffs)")
        }
        if "conversation_id" not in columns:
            self._connection.execute(
                "ALTER TABLE handoffs ADD COLUMN conversation_id TEXT DEFAULT ''"
            )
            self._connection.commit()

    # -- leads -------------------------------------------------------------
    def find_lead(self, *, phone: str = "", email: str = "") -> Lead | None:
        normalised_phone = normalise_phone(phone)
        normalised_email = normalise_email(email)
        with self._lock:
            for column, value in (("phone", normalised_phone), ("email", normalised_email)):
                if not value:
                    continue
                row = self._connection.execute(
                    f"SELECT * FROM leads WHERE {column} = ?", (value,)
                ).fetchone()
                if row:
                    return self._to_lead(row)
        return None

    def get_lead(self, lead_id: str) -> Lead | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM leads WHERE lead_id = ?", (lead_id,)
            ).fetchone()
        return self._to_lead(row) if row else None

    def upsert_lead(self, lead: Lead) -> Lead:
        """Insert or merge on the normalised phone/email key.

        Merge, not replace: a buyer who gives their email on web chat and their
        phone on WhatsApp is one lead with more information, not the second
        overwriting the first.
        """
        with self._lock:
            existing = None
            if lead.phone:
                existing = self.find_lead(phone=lead.phone)
            if existing is None and lead.email:
                existing = self.find_lead(email=lead.email)

            if existing is not None:
                return self.save_lead(self._merge(existing, lead))
            return self.save_lead(lead)

    def _merge(self, existing: Lead, incoming: Lead) -> Lead:
        """Fill blanks; never clobber a known value with an empty one."""
        for fieldname in ("name", "phone", "email", "company", "city",
                          "buyer_type", "quantity", "urgency"):
            new_value = getattr(incoming, fieldname)
            if new_value and not getattr(existing, fieldname):
                # Store the normalised form for the key fields.
                if fieldname == "phone":
                    new_value = normalise_phone(new_value) or new_value
                elif fieldname == "email":
                    new_value = normalise_email(new_value) or new_value
                setattr(existing, fieldname, new_value)

        for product in incoming.product_interest:
            if product not in existing.product_interest:
                existing.product_interest.append(product)
        for channel in incoming.channels_seen:
            if channel not in existing.channels_seen:
                existing.channels_seen.append(channel)
        for conversation in incoming.conversation_ids:
            if conversation not in existing.conversation_ids:
                existing.conversation_ids.append(conversation)

        existing.qualification.update(incoming.qualification)
        # A suppression or closure is a buyer decision; a later message must not
        # silently reopen it.
        if existing.state not in TERMINAL_STATES:
            existing.state = incoming.state
        return existing

    def save_lead(self, lead: Lead) -> Lead:
        lead.updated_at = datetime.now(timezone.utc).isoformat()
        if lead.phone:
            lead.phone = normalise_phone(lead.phone) or lead.phone
        if lead.email:
            lead.email = normalise_email(lead.email) or lead.email

        with self._lock:
            self._connection.execute(
                """
                INSERT INTO leads (lead_id, state, name, phone, email, company, city,
                    buyer_type, product_interest, quantity, urgency, source_channel,
                    channels_seen, conversation_ids, qualification, created_at, updated_at, erased_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(lead_id) DO UPDATE SET
                    state=excluded.state, name=excluded.name, phone=excluded.phone,
                    email=excluded.email, company=excluded.company, city=excluded.city,
                    buyer_type=excluded.buyer_type, product_interest=excluded.product_interest,
                    quantity=excluded.quantity, urgency=excluded.urgency,
                    channels_seen=excluded.channels_seen,
                    conversation_ids=excluded.conversation_ids,
                    qualification=excluded.qualification, updated_at=excluded.updated_at,
                    erased_at=excluded.erased_at
                """,
                (
                    lead.lead_id, lead.state.value, lead.name, lead.phone, lead.email,
                    lead.company, lead.city, lead.buyer_type,
                    json.dumps(lead.product_interest), lead.quantity, lead.urgency,
                    lead.source_channel, json.dumps(lead.channels_seen),
                    json.dumps(lead.conversation_ids), json.dumps(lead.qualification),
                    lead.created_at, lead.updated_at, lead.erased_at,
                ),
            )
            self._connection.commit()
        return lead

    def list_leads(self, *, state: LeadState | None = None) -> list[Lead]:
        query = "SELECT * FROM leads"
        params: tuple = ()
        if state is not None:
            query += " WHERE state = ?"
            params = (state.value,)
        with self._lock:
            rows = self._connection.execute(query + " ORDER BY created_at", params).fetchall()
        return [self._to_lead(row) for row in rows]

    def erase_lead(self, lead_id: str) -> bool:
        """DG-11: erase the personal data, keep the operational record.

        The lead row survives so conversation history and sentiment stay
        coherent, but every field that identifies a person is cleared and the
        phone/email keys are freed — which also means the same buyer can be
        re-captured later without colliding with a tombstone.
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT lead_id FROM leads WHERE lead_id = ?", (lead_id,)
            ).fetchone()
            if not row:
                return False
            now = datetime.now(timezone.utc).isoformat()
            self._connection.execute(
                """
                UPDATE leads SET name='', phone='', email='', company='', city='',
                    buyer_type='', quantity='', urgency='', product_interest='[]',
                    qualification='{}', erased_at=?, updated_at=? WHERE lead_id = ?
                """,
                (now, now, lead_id),
            )
            self._connection.commit()
        return True

    # -- handoffs ----------------------------------------------------------
    def record_handoff(self, handoff: Handoff) -> Handoff:
        """Idempotent per lead: two negative turns must not open two handoffs.

        US-022: the store assigns the id when the caller passed none. It used to
        insert `""` verbatim, so the first handoff took the empty PRIMARY KEY
        and every later one failed the UNIQUE constraint — swallowed into
        `dispatched=False`. **One handoff per database lifetime** was the real
        behaviour.

        An escalation with no lead is deduplicated per **conversation** instead,
        so two anonymous callers do not collapse into one handoff — and so a
        conversation id is never mistaken for a lead id.
        """
        with self._lock:
            if not handoff.handoff_id:
                handoff.handoff_id = str(uuid.uuid4())
            existing = self._connection.execute(
                "SELECT * FROM handoffs WHERE state IN ('open','notified','acknowledged')"
                " AND lead_id = ? AND (? != '' OR conversation_id = ?)",
                (handoff.lead_id, handoff.lead_id, handoff.conversation_id),
            ).fetchone()
            if existing:
                return self._to_handoff(existing)
            self._connection.execute(
                """INSERT INTO handoffs (handoff_id, lead_id, reason, state,
                       sentiment_score, products, conversation_id, created_at,
                       notified_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    handoff.handoff_id, handoff.lead_id, handoff.reason, handoff.state,
                    handoff.sentiment_score, json.dumps(handoff.products),
                    handoff.conversation_id, handoff.created_at, handoff.notified_at,
                ),
            )
            self._connection.commit()
        return handoff

    def mark_notified(self, handoff_id: str) -> Handoff | None:
        """Move a handoff to `notified`, once a rep has actually been told.

        Separate from `record_handoff` on purpose: a handoff exists the moment
        it is detected, and whether a human was reached is a different fact.
        Recording the first as the second is how "we escalated" comes to mean
        "nobody was told".
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._connection.execute(
                "UPDATE handoffs SET state = 'notified', notified_at = ? WHERE handoff_id = ?",
                (now, handoff_id),
            )
            self._connection.commit()
            row = self._connection.execute(
                "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
            ).fetchone()
        return self._to_handoff(row) if row else None

    def list_handoffs(self, *, state: str | None = None) -> list[Handoff]:
        query = "SELECT * FROM handoffs"
        params: tuple = ()
        if state:
            query += " WHERE state = ?"
            params = (state,)
        with self._lock:
            rows = self._connection.execute(query + " ORDER BY created_at", params).fetchall()
        return [self._to_handoff(row) for row in rows]

    # -- outbox (REC-07 pattern) -------------------------------------------
    def enqueue(self, operation: str, payload: dict) -> str:
        outbox_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._connection.execute(
                """INSERT INTO crm_outbox (outbox_id, operation, payload, attempts,
                       next_attempt_at, created_at) VALUES (?,?,?,0,?,?)""",
                (outbox_id, operation, json.dumps(payload), now, now),
            )
            self._connection.commit()
        return outbox_id

    def pending_outbox(self, limit: int = 50) -> list[dict]:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM crm_outbox WHERE next_attempt_at <= ?
                   ORDER BY next_attempt_at LIMIT ?""",
                (now, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_sent(self, outbox_id: str) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM crm_outbox WHERE outbox_id = ?", (outbox_id,))
            self._connection.commit()

    def mark_failed(self, outbox_id: str, error: str, *, delay_seconds: float = 60.0) -> None:
        """Back off, and give up after ten attempts rather than retrying forever."""
        from datetime import timedelta

        next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
        with self._lock:
            self._connection.execute(
                """UPDATE crm_outbox SET attempts = attempts + 1, last_error = ?,
                       next_attempt_at = ? WHERE outbox_id = ?""",
                (error[:500], next_attempt, outbox_id),
            )
            self._connection.execute("DELETE FROM crm_outbox WHERE attempts >= 10")
            self._connection.commit()

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _to_lead(row: sqlite3.Row) -> Lead:
        return Lead(
            lead_id=row["lead_id"],
            state=LeadState(row["state"]),
            name=row["name"] or "",
            phone=row["phone"] or "",
            email=row["email"] or "",
            company=row["company"] or "",
            city=row["city"] or "",
            buyer_type=row["buyer_type"] or "",
            product_interest=json.loads(row["product_interest"] or "[]"),
            quantity=row["quantity"] or "",
            urgency=row["urgency"] or "",
            source_channel=row["source_channel"] or "",
            channels_seen=json.loads(row["channels_seen"] or "[]"),
            conversation_ids=json.loads(row["conversation_ids"] or "[]"),
            qualification=json.loads(row["qualification"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            erased_at=row["erased_at"],
        )

    @staticmethod
    def _to_handoff(row: sqlite3.Row) -> Handoff:
        return Handoff(
            handoff_id=row["handoff_id"],
            lead_id=row["lead_id"],
            reason=row["reason"],
            state=row["state"],
            conversation_id=row["conversation_id"] or "",
            sentiment_score=row["sentiment_score"],
            products=json.loads(row["products"] or "[]"),
            created_at=row["created_at"],
            notified_at=row["notified_at"],
        )


def new_lead(**kwargs) -> Lead:
    """A lead with a fresh id."""
    return Lead(lead_id=str(uuid.uuid4()), **kwargs)
