"""Lead lifecycle and outbound follow-up (MOD-06, TRD-26 … TRD-31).

`SM-01` (lead) and `SM-04` (outbound attempt), plus qualification.

Two rules are enforced here rather than documented:

**Illegal transitions are refused, not coerced.** `SM-01` says a suppressed lead
can never be contacted again and a closed one cannot be re-contacted without a
new enquiry. Those are buyers' decisions, and a system that quietly reopened
them because a message arrived would be one nobody could trust to honour an
opt-out.

**The outbound window and attempt limit are conditions of dialling**, not advice
to the worker. A lead outside 10:00–19:00 IST is not dialled, and the seventh
attempt does not happen, because the check is in the claim query rather than in
the caller's judgement (`DG-04`, `R-6`).
"""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from .crm import Lead, LeadState, MockCrmStore, TERMINAL_STATES, normalise_email, normalise_phone


# Allowed transitions. Anything absent is refused.
LEGAL_TRANSITIONS: dict[LeadState, set[LeadState]] = {
    LeadState.NEW: {LeadState.QUALIFYING, LeadState.SUPPRESSED, LeadState.CLOSED},
    LeadState.QUALIFYING: {LeadState.QUALIFIED, LeadState.CLOSED, LeadState.SUPPRESSED},
    LeadState.QUALIFIED: {LeadState.CONTACT_ATTEMPTED, LeadState.ESCALATED,
                          LeadState.CLOSED, LeadState.SUPPRESSED},
    LeadState.CONTACT_ATTEMPTED: {LeadState.CONTACTED, LeadState.CONTACT_ATTEMPTED,
                                  LeadState.CLOSED, LeadState.SUPPRESSED},
    LeadState.CONTACTED: {LeadState.ESCALATED, LeadState.CLOSED, LeadState.SUPPRESSED},
    LeadState.ESCALATED: {LeadState.CLOSED, LeadState.SUPPRESSED},
    LeadState.SUPPRESSED: set(),   # terminal
    LeadState.CLOSED: set(),       # terminal
}


class TransitionError(ValueError):
    """An illegal `SM-01` transition was attempted."""


# ---------------------------------------------------------------------------
# Extraction and qualification
# ---------------------------------------------------------------------------
# Groups, not a free-for-all character class. The previous pattern allowed
# spaces, hyphens and parentheses anywhere, so it ran straight through the
# boundary between two unrelated numbers: "APO 200 (6835421)" — a part code and
# a product id — matched as a phone number, and every price question about a
# product with a model code was answered with "I've passed your details to our
# team" while creating a lead for a buyer who had given nothing.
#
# A parenthesis only ever opens a group, so it may not follow a digit. Length is
# checked separately: this pattern still matches a bare seven-digit product id,
# and a phone number is never that short.
_PHONE_RE = re.compile(
    r"(?:\+\d{1,3}[\s.-]?)?"              # optional country code
    r"(?:\(\d{2,5}\)[\s.-]?)?"            # optional area code, at the start only
    r"\d{2,5}(?:[\s.-]?\d{2,5}){1,3}"     # two to four digit groups
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# The case-insensitivity is scoped to the *prefix* only. Applied to the whole
# pattern it made [A-Z] match lowercase as well, so the capitalisation rule that
# defines where a name starts and stops did nothing: "i am Dr Rao from Apollo
# Hospital" captured "Dr Rao from Apollo". The prefix must be case-insensitive
# ("I am" / "i am"); the name must not.
_NAME_RE = re.compile(
    r"\b(?:(?i:i am|i'm|this is|my name is|name[:\s]+))\s+"
    r"([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,2})"
)
_COMPANY_RE = re.compile(
    r"\b(?:from|at|we are|company is)\s+([A-Z][A-Za-z0-9&.'-]+(?:\s+[A-Z][A-Za-z0-9&.'-]+){0,3}"
    r"\s+(?:Hospital|Clinic|Diagnostics?|Medical|Healthcare|Institute|Centre|Center|Labs?|Pvt|Limited|Ltd))",
    re.IGNORECASE,
)
_QUANTITY_RE = re.compile(r"\b(\d{1,6})\s*(?:units?|pieces?|pcs|sets?|boxes?|litres?|liters?|kg)\b", re.I)
_URGENT_RE = re.compile(r"\b(urgent\w*|asap|immediately|this week|right away|emergency)\b", re.I)

BUYER_TYPES = (
    ("hospital", "Hospital"),
    ("clinic", "Clinic"),
    ("diagnostic", "Diagnostic centre"),
    ("nursing home", "Nursing home"),
    ("distributor", "Distributor"),
    ("dealer", "Dealer"),
    ("service engineer", "Service engineer"),
    ("engineer", "Service engineer"),
)


def extract_lead_details(text: str) -> dict:
    """Pull what a message actually states. Absent fields stay absent.

    Extraction is deliberately conservative: a wrong phone number is worse than
    a missing one, because the missing one gets asked for and the wrong one gets
    called.
    """
    details: dict = {}
    message = text or ""

    email = _EMAIL_RE.search(message)
    if email:
        normalised = normalise_email(email.group(0))
        if normalised:
            details["email"] = normalised

    phone = _PHONE_RE.search(message)
    if phone:
        normalised = normalise_phone(phone.group(0))
        # A bare 6-digit order quantity must not read as a phone number.
        if normalised and len(normalised) >= 10:
            details["phone"] = normalised

    name = _NAME_RE.search(message)
    if name:
        # Cut the capture at the first connective. The pattern takes up to
        # three capitalised words, so "i am Dr Rao from Apollo Hospital"
        # captured "Dr Rao from" and "this is Anand at Ruby Clinic" captured
        # "Anand at Ruby" — a rep would read either as the contact's name.
        # Nothing before the first connective is ever part of a person's name.
        captured = name.group(1).strip()
        details["name"] = re.split(
            r"\s+(?:from|at|of|in|with|and|for)\s+", captured, maxsplit=1, flags=re.I
        )[0].strip()

    company = _COMPANY_RE.search(message)
    if company:
        details["company"] = company.group(1).strip()

    lowered = message.lower()
    for needle, label in BUYER_TYPES:
        if needle in lowered:
            details["buyer_type"] = label
            break

    quantity = _QUANTITY_RE.search(message)
    if quantity:
        details["quantity"] = quantity.group(0)

    if _URGENT_RE.search(message):
        details["urgency"] = "high"

    return details


def qualify(lead: Lead) -> dict:
    """Score how ready this lead is for a sales rep.

    Signals the catalogue and the conversation actually provide. Territory
    (postcode, state) is deliberately **not** a signal — the corpus has no
    vehicle or territory data (`DG-05`), and inventing a territory score from a
    city name would be a guess dressed as a number.
    """
    score = 0
    reasons: list[str] = []

    if lead.phone:
        score += 30
        reasons.append("phone")
    if lead.email:
        score += 15
        reasons.append("email")
    if lead.company:
        score += 15
        reasons.append("organisation named")
    if lead.buyer_type:
        score += 10
        reasons.append(f"buyer type: {lead.buyer_type}")
    if lead.product_interest:
        score += 15
        reasons.append(f"{len(lead.product_interest)} product(s) identified")
    if lead.quantity:
        score += 10
        reasons.append("quantity stated")
    if lead.urgency == "high":
        score += 15
        reasons.append("urgent")

    if score >= 60:
        tier = "hot"
    elif score >= 35:
        tier = "warm"
    else:
        tier = "nurture"

    return {"score": min(score, 100), "tier": tier, "reasons": reasons}


# ---------------------------------------------------------------------------
# Lead service
# ---------------------------------------------------------------------------
class LeadService:
    """Capture, deduplicate, qualify and transition leads."""

    def __init__(self, store: MockCrmStore) -> None:
        self.store = store
        self._lock = threading.RLock()

    def capture(
        self,
        text: str,
        *,
        channel: str = "",
        conversation_id: str | None = None,
        products: list[str] | None = None,
        explicit: dict | None = None,
    ) -> Lead | None:
        """Capture whatever the message states. Returns None if nothing usable.

        Returning None matters: creating an empty lead for every greeting would
        fill the pipeline with rows a rep cannot act on and make the North Star
        metric meaningless.
        """
        details = extract_lead_details(text)
        details.update({k: v for k, v in (explicit or {}).items() if v})
        if not details.get("phone") and not details.get("email") and not details.get("name"):
            return None

        with self._lock:
            existing = None
            if details.get("phone"):
                existing = self.store.find_lead(phone=details["phone"])
            if existing is None and details.get("email"):
                existing = self.store.find_lead(email=details["email"])

            lead = existing or Lead(lead_id=str(uuid.uuid4()))
            for key, value in details.items():
                if key == "product_interest":
                    continue
                if not getattr(lead, key, None):
                    setattr(lead, key, value)

            if channel and channel not in lead.channels_seen:
                lead.channels_seen.append(channel)
            if not lead.source_channel:
                lead.source_channel = channel
            if conversation_id and conversation_id not in lead.conversation_ids:
                lead.conversation_ids.append(conversation_id)
            for product in products or []:
                if product not in lead.product_interest:
                    lead.product_interest.append(product)

            if lead.state is LeadState.NEW and lead.contactable:
                lead.state = LeadState.QUALIFYING

            lead.qualification = qualify(lead)
            if lead.state is LeadState.QUALIFYING and lead.qualification["tier"] in {"hot", "warm"}:
                lead.state = LeadState.QUALIFIED

            return self.store.save_lead(lead)

    def transition(self, lead: Lead, new_state: LeadState) -> Lead:
        """Apply an `SM-01` transition, refusing anything illegal."""
        if new_state is lead.state:
            return lead
        if new_state not in LEGAL_TRANSITIONS.get(lead.state, set()):
            raise TransitionError(
                f"illegal lead transition {lead.state.value} -> {new_state.value} "
                f"(allowed: {sorted(s.value for s in LEGAL_TRANSITIONS.get(lead.state, set()))})"
            )
        lead.state = new_state
        return self.store.save_lead(lead)

    def suppress(self, lead: Lead) -> Lead:
        """Honour an opt-out. Reachable from any non-terminal state."""
        if lead.state in TERMINAL_STATES:
            return lead
        lead.state = LeadState.SUPPRESSED
        return self.store.save_lead(lead)

    def erase(self, lead_id: str) -> bool:
        """DG-11: honour a deletion request."""
        return self.store.erase_lead(lead_id)

    def by_tier(self, tier: str) -> list[Lead]:
        return [
            lead for lead in self.store.list_leads()
            if lead.qualification.get("tier") == tier and not lead.is_erased
        ]


# ---------------------------------------------------------------------------
# Outbound (SM-04)
# ---------------------------------------------------------------------------
class AttemptState(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    DIALING = "dialing"
    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    FAILED = "failed"
    EXHAUSTED = "exhausted"


@dataclass
class OutboundAttempt:
    attempt_id: str
    lead_id: str
    state: AttemptState = AttemptState.QUEUED
    attempt_number: int = 1
    scheduled_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    claimed_at: str | None = None
    finished_at: str | None = None
    outcome: str = ""

    def as_dict(self) -> dict:
        return {
            "attempt_id": self.attempt_id,
            "lead_id": self.lead_id,
            "state": self.state.value,
            "attempt_number": self.attempt_number,
            "outcome": self.outcome,
        }


class OutboundQueue:
    """Dialling queue with a window and an attempt limit (`DG-04`, `R-6`).

    The window and limit are checked **at claim time**, in one place, so a
    worker cannot dial at 22:00 or make a seventh attempt by calling a different
    method. Claiming a row also prevents a second worker from taking it —
    the same exclusion `SELECT … FOR UPDATE SKIP LOCKED` provides on Postgres,
    implemented here with a lock because the mock store is in-process.
    """

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        window_start: str = "10:00",
        window_end: str = "19:00",
        retry_delays_minutes: tuple[int, ...] = (240, 1440),
        timezone_offset_hours: float = 5.5,   # Asia/Kolkata
    ) -> None:
        self.max_attempts = max_attempts
        self.window_start = window_start
        self.window_end = window_end
        self.retry_delays_minutes = retry_delays_minutes
        self.timezone_offset_hours = timezone_offset_hours
        self._attempts: dict[str, OutboundAttempt] = {}
        self._lock = threading.RLock()

    # -- window ------------------------------------------------------------
    def _local_minutes(self, now: datetime | None = None) -> int:
        moment = now or datetime.now(timezone.utc)
        shifted = moment + timedelta(hours=self.timezone_offset_hours)
        return shifted.hour * 60 + shifted.minute

    def _to_minutes(self, hhmm: str) -> int:
        hour, _, minute = hhmm.partition(":")
        return int(hour) * 60 + int(minute or 0)

    def in_window(self, now: datetime | None = None) -> bool:
        """True inside the approved calling window (IST by default)."""
        current = self._local_minutes(now)
        return self._to_minutes(self.window_start) <= current < self._to_minutes(self.window_end)

    # -- queue -------------------------------------------------------------
    def enqueue(self, lead: Lead) -> OutboundAttempt | None:
        """Queue a lead, unless it is suppressed, closed or already queued."""
        if lead.state in TERMINAL_STATES or lead.is_erased:
            return None
        if not lead.contactable:
            return None
        with self._lock:
            active = [
                attempt for attempt in self._attempts.values()
                if attempt.lead_id == lead.lead_id
                and attempt.state in {AttemptState.QUEUED, AttemptState.CLAIMED, AttemptState.DIALING}
            ]
            if active:
                return None   # deduplicate; one live attempt per lead
            attempt = OutboundAttempt(attempt_id=str(uuid.uuid4()), lead_id=lead.lead_id)
            self._attempts[attempt.attempt_id] = attempt
            return attempt

    def claim(self, *, now: datetime | None = None) -> OutboundAttempt | None:
        """Claim the next dialable attempt, or None if there is nothing legal.

        Returns None outside the window — the caller must treat that as "wait",
        not "try harder".
        """
        if not self.in_window(now):
            return None
        with self._lock:
            for attempt in self._attempts.values():
                if attempt.state is not AttemptState.QUEUED:
                    continue
                if attempt.attempt_number > self.max_attempts:
                    attempt.state = AttemptState.EXHAUSTED
                    continue
                attempt.state = AttemptState.CLAIMED
                attempt.claimed_at = datetime.now(timezone.utc).isoformat()
                return attempt
        return None

    def mark_dialing(self, attempt: OutboundAttempt) -> None:
        with self._lock:
            if attempt.state is not AttemptState.CLAIMED:
                raise TransitionError(
                    f"cannot dial from {attempt.state.value} (expected claimed)"
                )
            attempt.state = AttemptState.DIALING

    def record_outcome(self, attempt: OutboundAttempt, outcome: str) -> OutboundAttempt:
        """Record what happened and decide whether to retry."""
        with self._lock:
            attempt.outcome = outcome
            attempt.finished_at = datetime.now(timezone.utc).isoformat()

            if outcome == "completed":
                attempt.state = AttemptState.COMPLETED
            elif outcome in {"no_answer", "busy"}:
                attempt.state = AttemptState.NO_ANSWER
            else:
                attempt.state = AttemptState.FAILED

            if attempt.state is AttemptState.NO_ANSWER:
                if attempt.attempt_number >= self.max_attempts:
                    attempt.state = AttemptState.EXHAUSTED
                else:
                    # A retry is a new row, not a reopened one, so the attempt
                    # history stays readable.
                    self._attempts[attempt.attempt_id] = attempt
                    return self._schedule_retry(attempt)
            return attempt

    def _schedule_retry(self, previous: OutboundAttempt) -> OutboundAttempt:
        delay_index = min(previous.attempt_number - 1, len(self.retry_delays_minutes) - 1)
        delay = self.retry_delays_minutes[delay_index]
        retry = OutboundAttempt(
            attempt_id=str(uuid.uuid4()),
            lead_id=previous.lead_id,
            attempt_number=previous.attempt_number + 1,
            scheduled_at=(datetime.now(timezone.utc) + timedelta(minutes=delay)).isoformat(),
        )
        self._attempts[retry.attempt_id] = retry
        return retry

    def release_stale(self, *, older_than_minutes: int = 15) -> int:
        """Return abandoned CLAIMED/DIALING rows to QUEUED after a worker dies."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
        released = 0
        with self._lock:
            for attempt in self._attempts.values():
                if attempt.state not in {AttemptState.CLAIMED, AttemptState.DIALING}:
                    continue
                if not attempt.claimed_at:
                    continue
                if datetime.fromisoformat(attempt.claimed_at) < cutoff:
                    attempt.state = AttemptState.QUEUED
                    attempt.claimed_at = None
                    released += 1
        return released

    def counts(self) -> dict[str, int]:
        tallies: dict[str, int] = {}
        for attempt in self._attempts.values():
            tallies[attempt.state.value] = tallies.get(attempt.state.value, 0) + 1
        return tallies
