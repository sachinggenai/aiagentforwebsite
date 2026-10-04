"""Rep notification for escalations (US-022, DG-12).

The audit's finding: escalations reached a real CRM sink and then stopped.
A handoff row was written and **nobody was told** — `state='notified'` was
never set because nothing ever notified anyone, and no admin route exposed
the queue either.

This is the missing last step. It is deliberately **best-effort**: a
notification that cannot be sent must not fail the turn that produced it, and
the handoff stays `open` until a rep has actually been reached. "We escalated"
and "somebody knows" are different facts, and only the second moves the state.

Transport is SMTP over the stdlib. Configuration comes from the environment
(`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SALES_NOTIFY_EMAIL` —
DG-12's decided default of email to the sales rep). With no host or no
recipient the notifier is **absent**, and the sink records that nothing was
attempted rather than reporting a notification that never happened.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any, Mapping

logger = logging.getLogger("anita.notify")

DEFAULT_SMTP_PORT = 587


class SmtpNotifier:
    """Emails the sales rep when a conversation escalates.

    Never raises: `__call__` returns whether the rep was reached, and every
    failure is logged without the credentials and without buyer contact
    details beyond what the handoff itself carries.
    """

    def __init__(
        self,
        *,
        host: str,
        to: str,
        port: int = DEFAULT_SMTP_PORT,
        user: str = "",
        password: str = "",
        sender: str = "",
        timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.to = to
        self.port = port
        self.user = user
        self.password = password
        self.sender = sender or user or "anita@localhost"
        self.timeout = timeout

    def __call__(self, handoff: Any) -> bool:
        try:
            message = EmailMessage()
            message["Subject"] = f"[ANITA] Escalation — {getattr(handoff, 'reason', 'unspecified')}"
            message["From"] = self.sender
            message["To"] = self.to
            message.set_content(
                "\n".join(
                    [
                        "A buyer conversation has escalated and needs a person.",
                        "",
                        f"handoff      : {getattr(handoff, 'handoff_id', '')}",
                        f"lead         : {getattr(handoff, 'lead_id', '') or '(no contact details given)'}",
                        f"conversation : {getattr(handoff, 'conversation_id', '')}",
                        f"reason       : {getattr(handoff, 'reason', '')}",
                        f"score        : {getattr(handoff, 'sentiment_score', None)}",
                        f"raised at    : {getattr(handoff, 'created_at', '')}",
                        "",
                        "The transcript is in the admin console; this message carries no",
                        "buyer text on purpose.",
                    ]
                )
            )
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                if self.user:
                    smtp.starttls()
                    smtp.login(self.user, self.password)
                smtp.send_message(message)
            logger.info("escalation %s notified to %s", getattr(handoff, "handoff_id", "?"), self.to)
            return True
        except Exception:  # noqa: BLE001 - a notification must never fail a turn
            logger.warning("escalation notification failed", exc_info=True)
            return False


def from_environment(env: Mapping[str, str] | None) -> SmtpNotifier | None:
    """Build the notifier from the environment, or None when unconfigured.

    Absent is a legitimate state — a deployment that has not set up SMTP gets
    handoffs that stay `open`, and the boot report says so. Returning a
    half-configured notifier that fails on every escalation would be worse:
    it would look configured.
    """
    values = env or {}
    host = (values.get("SMTP_HOST") or "").strip()
    to = (values.get("SALES_NOTIFY_EMAIL") or "").strip()
    if not host or not to:
        return None
    try:
        port = int((values.get("SMTP_PORT") or "").strip() or DEFAULT_SMTP_PORT)
    except ValueError:
        logger.warning("SMTP_PORT is not a number — falling back to %d", DEFAULT_SMTP_PORT)
        port = DEFAULT_SMTP_PORT
    return SmtpNotifier(
        host=host,
        to=to,
        port=port,
        user=(values.get("SMTP_USER") or "").strip(),
        password=values.get("SMTP_PASS") or "",
    )


__all__ = ["SmtpNotifier", "from_environment"]
