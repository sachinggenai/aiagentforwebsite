"""MOD-07 → MOD-06 — escalation reaches a real sink.

`anita.sentiment` could detect a negative turn, and had nowhere to put it. UC-10
("Negative sentiment detected") was declared, implemented, tested, and
**deferred** — `system_check.py` reported it unreachable, and `anita.yaml` said
so in writing:

    UC-10: Negative sentiment escalation. anita.sentiment is complete and tested
    and referenced by no surface. Deferred until there is an escalation
    destination — escalating to nobody is worse than not detecting it.

This module is the destination. A negative turn now opens a `Handoff` against
the lead, which is the same sink a "request a human" already uses — so an
escalation and a callback request arrive in one queue rather than two, and a rep
sees one list.

**The trap this avoids.** Deleting the `UC-10` line from the deferral list in
`anita.yaml` on its own does not make the use case pass — `system_check` then
fails it, because nothing drives it. Both are required: the capability, and a
check that exercises it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("anita.escalation")


@dataclass
class EscalationRecord:
    """What an escalation observed. Kept separately from the Handoff so the
    decision is auditable even when dispatch is suppressed."""

    lead_id: str
    conversation_id: str
    reason: str
    score: float
    emotion: str
    category: str
    dispatched: bool
    handoff_id: str | None = None
    #: US-022: whether a rep was actually reached. `dispatched` says a handoff
    #: was opened; this says somebody knows. Keeping them separate is what
    #: stops "we escalated" from quietly meaning "nobody was told".
    notified: bool = False


@dataclass
class EscalationSink:
    """Turns a sentiment decision into a Handoff against the CRM.

    Deliberately thin. The decision belongs to `SentimentCore`; the routing
    belongs here; the record belongs to the CRM. Merging the three is how
    "detected a problem" becomes indistinguishable from "told someone".
    """

    store: Any
    crm: Any = None
    config: Any = None
    #: US-022: called with the recorded `Handoff`; returns True when a rep was
    #: reached. None means the deployment has no notifier — handoffs stay
    #: `open` for the admin queue, which is a state, not a failure.
    notifier: Any = None
    records: list[EscalationRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.crm is None:
            # The CRM is where a Handoff belongs. Without one, escalations are
            # still *recorded* — silence must not erase the evidence — but no
            # handoff is opened, and `dispatched` says so.
            logger.info("escalation: no CRM attached; escalations will be recorded only")

    def _open_handoff(self, record: EscalationRecord) -> str | None:
        if self.crm is None:
            return None
        from .crm import Handoff

        try:
            handoff = self.crm.record_handoff(Handoff(
                handoff_id="",           # the store assigns; idempotent per lead
                lead_id=record.lead_id,
                conversation_id=record.conversation_id,
                reason=record.reason,
                sentiment_score=record.score,
            ))
            self._notify(handoff, record)
            return getattr(handoff, "handoff_id", None)
        except Exception:  # noqa: BLE001 - a failed sink must not fail the turn
            logger.exception("could not open a handoff for lead %s", record.lead_id)
            return None

    def _notify(self, handoff: Any, record: EscalationRecord) -> None:
        """Tell a rep, and record whether that actually happened (US-022).

        The handoff exists either way; only a delivered notification moves it to
        `notified`. A missing notifier is a deployment that has not configured
        SMTP, not an error — the handoff stays `open` in the admin queue.
        """
        if self.notifier is None:
            return
        try:
            if self.notifier(handoff):
                record.notified = True
                mark = getattr(self.crm, "mark_notified", None)
                if mark is not None:
                    mark(handoff.handoff_id)
        except Exception:  # noqa: BLE001 - notifying must never fail the turn
            logger.exception("escalation notification raised for %s", handoff.handoff_id)

    def __call__(self, state: Any, reason: str, extraction: Any) -> None:
        """The `on_escalate` callback `SentimentCore.observe` invokes.

        Signature matches the core's contract: ``(state, reason, extraction)``.
        """
        lead_id = getattr(state, "lead_id", "") or ""
        record = EscalationRecord(
            lead_id=lead_id,
            conversation_id=getattr(state, "conversation_id", "") or "",
            reason=reason,
            score=float(getattr(state, "latest", 0.0) or 0.0),
            emotion=getattr(getattr(extraction, "primary_emotion", None), "value", ""),
            category=getattr(state, "category", ""),
            dispatched=False,
        )
        record.handoff_id = self._open_handoff(record)
        record.dispatched = record.handoff_id is not None
        self.records.append(record)

    # -- reporting ---------------------------------------------------------
    def count(self) -> int:
        return len(self.records)

    def open_handoffs(self) -> int:
        if self.crm is None:
            return 0
        try:
            return len(self.crm.list_handoffs(state="open"))
        except Exception:  # noqa: BLE001
            return 0

    def status(self) -> dict:
        return {
            "escalations": len(self.records),
            "dispatched": sum(1 for r in self.records if r.dispatched),
            "crm_attached": self.crm is not None,
            "open_handoffs": self.open_handoffs(),
            "last": self.records[-1].__dict__ if self.records else None,
        }


__all__ = ["EscalationSink", "EscalationRecord"]


class SentimentObserver:
    """`ChatSurface`'s sentiment dependency: score a turn and escalate if due.

    One object so the surface depends on a single seam rather than on the core
    and the sink separately — and so a product can substitute a no-op observer
    without the surface knowing.
    """

    def __init__(self, core, sink: EscalationSink | None = None) -> None:
        self.core = core
        self.sink = sink
        # `SentimentCore.observe` dispatches through its own `on_escalate`, so
        # the sink has to reach the core and not only this object. Attaching it
        # here rather than in the caller keeps the pair together — the first
        # version passed the sink to the observer alone and every escalation was
        # decided and then silently dropped.
        if sink is not None:
            core.on_escalate = sink
        self._baselines: dict[str, float] = {}

    def score_and_observe(self, message: str, *, lead_id: str = "",
                          conversation_id: str = ""):
        from shared.sentiment import LeadSentimentState

        baseline = self._baselines.get(lead_id)
        extraction, composite = self.core.score_turn(
            message, lead_id=lead_id, previous_latest=baseline, baseline=baseline
        )
        # The EWMA the composite computed becomes this lead's next baseline —
        # which is what makes "a sharp decline" detectable at all. Without it,
        # every turn is a first turn and momentum is always zero.
        self._baselines[lead_id] = composite.baseline

        state = LeadSentimentState(
            lead_id=lead_id,
            baseline=composite.baseline,
            latest=composite.latest,
            turn_count=1,
            category=self.core.categorise(composite, extraction).value,
        )
        if self.sink is not None:
            self.core.observe(state, extraction, composite)
        return state

    def status(self) -> dict:
        return {
            "core": self.core.status(),
            "sink": self.sink.status() if self.sink else None,
            "leads_observed": len(self._baselines),
        }
