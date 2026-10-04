"""MOD-06 tests — lead capture, dedupe, lifecycle, outbound, mock CRM.

Run:  .venv\\Scripts\\python -m pytest tests -q
"""

from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.crm import (  # noqa: E402
    Handoff,
    Lead,
    LeadState,
    MockCrmStore,
    normalise_email,
    normalise_phone,
)
from anita.leads import (  # noqa: E402
    LEGAL_TRANSITIONS,
    AttemptState,
    LeadService,
    OutboundQueue,
    TransitionError,
    extract_lead_details,
    qualify,
)


@pytest.fixture()
def store() -> MockCrmStore:
    return MockCrmStore(":memory:")


@pytest.fixture()
def service(store) -> LeadService:
    return LeadService(store)


# ---------------------------------------------------------------------------
# Normalisation — the dedupe key depends on it
# ---------------------------------------------------------------------------
class TestNormalisation:
    @pytest.mark.parametrize("raw", [
        "+91 98000 00000", "919800000000", "+91-98000-00000", "(+91) 98000 00000",
    ])
    def test_phone_forms_collapse_to_one_key(self, raw):
        """Without this, three spellings are three leads for one buyer."""
        assert normalise_phone(raw) == "+919800000000"

    def test_a_national_number_gains_the_home_country_code(self):
        """This previously asserted the opposite, as a documented limitation:
        format was normalised but country was not, so "98000 00000" stayed
        separate from "+91 98000 00000". That was a limitation in both
        directions — it also turned the national number into "+980000000000",
        a country code that does not exist, so the record was not merely
        un-merged, it was wrong."""
        assert normalise_phone("98000 00000") == "+919800000000"

    def test_one_buyer_writing_it_four_ways_is_one_lead(self):
        forms = ["98200 11122", "+91 98200 11122", "0091 98200 11122",
                 "9820011122", "098200 11122"]
        assert len({normalise_phone(f) for f in forms}) == 1

    def test_a_landline_with_a_trunk_zero_matches_its_international_form(self):
        """The corpus stores the company's own number as +918045477795; a buyer
        writing it the way it appears on the site must produce the same key."""
        assert normalise_phone("08045477795") == normalise_phone("+918045477795")

    def test_an_explicit_international_number_is_left_alone(self):
        """The one case that is decidable: the caller said which country."""
        assert normalise_phone("+1 555 010 9999") == "+15550109999"
        assert normalise_phone("+44 20 7946 0958") == "+442079460958"

    def test_the_residual_risk_is_stated_not_hidden(self):
        """A foreign number written *without* a prefix is read as domestic.
        There is no way to tell it from a national number using the digits, so
        the default is chosen, not guessed — and this test records what that
        choice costs rather than pretending it does not exist."""
        assert normalise_phone("555 010 9999") == "+915550109999"

    def test_the_home_country_can_be_changed(self):
        assert normalise_phone("98000 00000", country_code="44") == "+449800000000"

    def test_empty_phone(self):
        assert normalise_phone(None) is None
        assert normalise_phone("no digits here") is None

    def test_email_is_lowercased_and_validated(self):
        assert normalise_email("  Buyer@Example.COM ") == "buyer@example.com"
        assert normalise_email("not-an-email") is None

    def test_valid_emails_pass(self):
        assert normalise_email("a.b+tag@hospital.co.in") == "a.b+tag@hospital.co.in"


# ---------------------------------------------------------------------------
# Deduplication — SM-01
# ---------------------------------------------------------------------------
class TestDeduplication:
    def test_same_phone_is_one_lead(self, service):
        a = service.capture("call me on +91 98000 00000", channel="whatsapp")
        b = service.capture("my number is +91-98000-00000", channel="voice_inbound")
        assert a.lead_id == b.lead_id
        assert set(b.channels_seen) == {"whatsapp", "voice_inbound"}

    def test_same_email_is_one_lead(self, service):
        a = service.capture("email me at buyer@hospital.com", channel="web_chat")
        b = service.capture("it's Buyer@Hospital.com", channel="whatsapp")
        assert a.lead_id == b.lead_id

    def test_merge_fills_blanks_without_clobbering(self, service):
        """A buyer who gives phone on WhatsApp and email on web is one lead with
        more information, not the second overwriting the first."""
        first = service.capture("I am Priya, call +91 98000 00000")
        second = service.capture("priya@hospital.com, still +91 98000 00000")
        assert second.name == "Priya"          # preserved from the first
        assert second.email == "priya@hospital.com"  # added by the second

    def test_nothing_usable_creates_no_lead(self, service):
        """An empty lead per greeting would make the North Star metric
        meaningless and the pipeline unusable."""
        assert service.capture("hello") is None
        assert service.capture("what is the price?") is None
        assert store_count(service) == 0

    def test_products_accumulate_across_conversations(self, service):
        a = service.capture("+91 98000 00000", products=["X-Ray Cassette"])
        b = service.capture("also interested in the ultrasound gel +91 98000 00000",
                        products=["Ultrasound Gel"])
        assert set(b.product_interest) == {"X-Ray Cassette", "Ultrasound Gel"}


def store_count(service) -> int:
    return len(service.store.list_leads())


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
class TestExtraction:
    def test_extracts_phone_email_name(self):
        details = extract_lead_details("I am Rajesh, call +91 98000 00000 or raj@clinic.in")
        assert details["phone"] == "+919800000000"
        assert details["email"] == "raj@clinic.in"
        assert details["name"] == "Rajesh"

    def test_extracts_organisation_and_buyer_type(self):
        details = extract_lead_details("We are Sunrise Diagnostics Hospital, need cassettes")
        assert "company" in details
        assert details.get("buyer_type") == "Hospital"

    def test_extracts_quantity_and_urgency(self):
        details = extract_lead_details("We need 50 units urgently")
        assert "50" in details["quantity"]
        assert details["urgency"] == "high"

    def test_a_small_number_is_not_mistaken_for_a_phone(self):
        """A wrong number is worse than a missing one: the missing gets asked
        for, the wrong one gets called."""
        assert "phone" not in extract_lead_details("we need 500 pieces")

    def test_absent_fields_stay_absent(self):
        assert extract_lead_details("hello there") == {}


# ---------------------------------------------------------------------------
# Qualification
# ---------------------------------------------------------------------------
class TestQualification:
    def test_hot_lead(self):
        lead = Lead(lead_id="1", phone="+919800000000", email="a@b.com",
                    company="Sunrise Hospital", buyer_type="Hospital",
                    product_interest=["X-Ray Cassette"], quantity="50 units", urgency="high")
        assert qualify(lead)["tier"] == "hot"

    def test_nurture_lead(self):
        assert qualify(Lead(lead_id="1", name="Priya"))["tier"] == "nurture"

    def test_reasons_are_explainable(self):
        result = qualify(Lead(lead_id="1", phone="+9198"))
        assert "phone" in result["reasons"]

    def test_no_territory_signal_is_invented(self):
        """DG-05: the corpus has no territory data, so a city must not produce a
        territory score — that would be a guess dressed as a number."""
        result = qualify(Lead(lead_id="1", city="Pune", phone="+9198"))
        assert not any("territor" in reason.lower() for reason in result["reasons"])
        assert "postcode" not in result


# ---------------------------------------------------------------------------
# Lifecycle — SM-01
# ---------------------------------------------------------------------------
class TestLifecycle:
    def test_legal_transitions_are_allowed(self, service):
        lead = service.capture("+91 98000 00000 or a@b.com")
        assert lead is not None
        assert service.transition(lead, LeadState.QUALIFIED)

    def test_illegal_transitions_are_refused(self, service):
        """A suppressed buyer must never be re-contacted just because a message
        arrived — an opt-out the system quietly reopens is one nobody trusts."""
        lead = service.capture("+91 98000 00000 or a@b.com")
        service.suppress(lead)
        with pytest.raises(TransitionError, match="illegal lead transition"):
            service.transition(lead, LeadState.CONTACTED)

    def test_suppression_is_terminal(self, service):
        lead = service.capture("+91 98000 00000 or a@b.com")
        service.suppress(lead)
        assert LEGAL_TRANSITIONS[LeadState.SUPPRESSED] == set()
        assert service.suppress(lead).state is LeadState.SUPPRESSED

    def test_a_new_message_does_not_reopen_a_suppressed_lead(self, service, store):
        lead = service.capture("+91 98000 00000 or a@b.com")
        service.suppress(lead)
        again = service.capture("hello again +91 98000 00000")
        assert again.lead_id == lead.lead_id
        assert again.state is LeadState.SUPPRESSED

    def test_erasure_clears_pii_but_keeps_the_record(self, service, store):
        """DG-11: conversations and sentiment must stay coherent after a
        deletion request, so the row survives with the person removed."""
        lead = service.capture("I am Priya, priya@hospital.com, +91 98000 00000")
        assert service.erase(lead.lead_id)
        erased = store.get_lead(lead.lead_id)
        assert erased.is_erased
        assert erased.name == "" and erased.email == "" and erased.phone == ""
        assert not erased.contactable

    def test_erasure_frees_the_dedupe_key(self, service, store):
        """Otherwise the buyer could never be captured again without colliding
        with a tombstone."""
        lead = service.capture("priya@hospital.com")
        service.erase(lead.lead_id)
        recaptured = service.capture("priya@hospital.com")
        assert recaptured is not None
        assert recaptured.lead_id != lead.lead_id


# ---------------------------------------------------------------------------
# Handoffs — SM-06
# ---------------------------------------------------------------------------
class TestHandoffs:
    def test_a_handoff_is_recorded(self, store):
        store.record_handoff(Handoff(handoff_id="h1", lead_id="l1", reason="negative sentiment"))
        assert len(store.list_handoffs(state="open")) == 1

    def test_two_escalations_open_one_handoff(self, store):
        """Two negative turns must not produce two handoffs for one lead."""
        store.record_handoff(Handoff(handoff_id="h1", lead_id="l1", reason="first"))
        second = store.record_handoff(Handoff(handoff_id="h2", lead_id="l1", reason="second"))
        assert second.handoff_id == "h1"
        assert len(store.list_handoffs()) == 1

    def test_a_closed_handoff_can_be_reopened(self, store):
        store.record_handoff(Handoff(handoff_id="h1", lead_id="l1", reason="first", state="closed"))
        second = store.record_handoff(Handoff(handoff_id="h2", lead_id="l1", reason="new issue"))
        assert second.handoff_id == "h2"


# ---------------------------------------------------------------------------
# Outbound — SM-04, DG-04
# ---------------------------------------------------------------------------
class TestOutbound:
    def _lead(self, **kwargs):
        return Lead(lead_id="l1", phone="+919800000000", **kwargs)

    def test_a_lead_is_queued(self):
        queue = OutboundQueue()
        assert queue.enqueue(self._lead()) is not None

    def test_suppressed_lead_is_never_queued(self):
        queue = OutboundQueue()
        assert queue.enqueue(self._lead(state=LeadState.SUPPRESSED)) is None

    def test_uncontactable_lead_is_not_queued(self):
        queue = OutboundQueue()
        assert queue.enqueue(Lead(lead_id="l2")) is None

    def test_only_one_live_attempt_per_lead(self):
        """A duplicate queue row would dial the same buyer twice."""
        queue = OutboundQueue()
        queue.enqueue(self._lead())
        assert queue.enqueue(self._lead()) is None

    def test_outside_the_window_nothing_is_claimed(self):
        """DG-04: a buyer must not be called at 22:00."""
        queue = OutboundQueue(window_start="10:00", window_end="19:00")
        queue.enqueue(self._lead())
        # 22:00 IST == 16:30 UTC
        night = datetime(2026, 10, 3, 16, 30, tzinfo=timezone.utc)
        assert not queue.in_window(night)
        assert queue.claim(now=night) is None

    def test_inside_the_window_a_lead_is_claimed(self):
        queue = OutboundQueue()
        queue.enqueue(self._lead())
        midday = datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)   # 11:30 IST
        assert queue.in_window(midday)
        assert queue.claim(now=midday) is not None

    def test_a_claimed_attempt_cannot_be_claimed_twice(self):
        """The exclusion FOR UPDATE SKIP LOCKED provides, in-process."""
        queue = OutboundQueue()
        queue.enqueue(self._lead())
        midday = datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)
        assert queue.claim(now=midday) is not None
        assert queue.claim(now=midday) is None

    def test_attempts_are_exhausted_at_the_limit(self):
        """R-6: the fourth attempt does not happen."""
        queue = OutboundQueue(max_attempts=3, retry_delays_minutes=(0, 0))
        queue.enqueue(self._lead())
        midday = datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)

        attempt = queue.claim(now=midday)
        assert attempt.attempt_number == 1
        queue.mark_dialing(attempt)
        retry = queue.record_outcome(attempt, "no_answer")
        assert retry.attempt_number == 2

        # A retry is a new row in QUEUED — it must be claimed before dialling.
        claimed = queue.claim(now=midday)
        queue.mark_dialing(claimed)
        third = queue.record_outcome(claimed, "no_answer")
        assert third.attempt_number == 3

        final_claim = queue.claim(now=midday)
        queue.mark_dialing(final_claim)
        final = queue.record_outcome(final_claim, "no_answer")
        assert final.state is AttemptState.EXHAUSTED

    def test_a_completed_call_is_terminal(self):
        queue = OutboundQueue()
        queue.enqueue(self._lead())
        attempt = queue.claim(now=datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc))
        queue.mark_dialing(attempt)
        assert queue.record_outcome(attempt, "completed").state is AttemptState.COMPLETED

    def test_dialing_from_queued_is_refused(self):
        queue = OutboundQueue()
        queue.enqueue(self._lead())
        attempt = queue.claim(now=datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc))
        # claimed, not yet dialing — mark_dialing is correct; a second call is not
        queue.mark_dialing(attempt)
        with pytest.raises(TransitionError):
            queue.mark_dialing(attempt)

    def test_stale_attempts_are_released(self):
        """A worker that dies mid-dial must not strand the lead."""
        queue = OutboundQueue()
        queue.enqueue(self._lead())
        attempt = queue.claim(now=datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc))
        attempt.claimed_at = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        assert queue.release_stale(older_than_minutes=15) == 1
        assert attempt.state is AttemptState.QUEUED


# ---------------------------------------------------------------------------
# The CRM seam (BRD-14)
# ---------------------------------------------------------------------------
class TestCrmSeam:
    def test_the_interface_mentions_no_vendor_vocabulary(self):
        """BRD-14: no Salesforce code, schema, client or credential is reused.
        The seam is generic, so a real CRM is a new implementation of it — not a
        change to every caller."""
        # Read the code, not the prose. crm.py's docstring names Salesforce in
        # order to explain the exclusion — a test forbidding it from saying the
        # name would force the reasoning out of the file.
        tree = ast.parse((PLATFORM / "app" / "anita" / "crm.py").read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)
        literals = [
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value not in docstrings
        ]
        for vendor_term in ("simple_salesforce", "ContentVersion", "SOQL", "picklist",
                            "salesforce", "crm_user_id", "crm_application_no"):
            offenders = [text for text in literals if vendor_term in text]
            assert not offenders, f"vendor vocabulary in code: {vendor_term}"

    def test_no_module_imports_a_salesforce_client(self):
        offenders: list[str] = []
        for path in PLATFORM.rglob("app/anita/*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    if "salesforce" in name.lower() or name.split(".")[0] == "zeep":
                        offenders.append(f"{path.name}: {name}")
        assert not offenders, offenders

    def test_the_store_is_swappable(self, service):
        """Any object satisfying CrmStore works. Proven by substitution."""
        recorded: list[str] = []

        class RecordingStore(MockCrmStore):
            def save_lead(self, lead):
                recorded.append(lead.lead_id)
                return super().save_lead(lead)

        swapped = LeadService(RecordingStore(":memory:"))
        swapped.capture("+91 98000 00000")
        assert recorded, "the service must go through the interface"

    def test_outbox_retries_with_backoff(self, store):
        """REC-07: a CRM outage degrades to a delay, not a lost lead."""
        outbox_id = store.enqueue("push_lead", {"lead_id": "l1"})
        store.mark_failed(outbox_id, "connection refused", delay_seconds=0)
        pending = store.pending_outbox()
        assert pending and pending[0]["attempts"] == 1

    def test_outbox_gives_up_after_ten_attempts(self, store):
        outbox_id = store.enqueue("push_lead", {"lead_id": "l1"})
        for _ in range(10):
            store.mark_failed(outbox_id, "still down", delay_seconds=0)
        assert store.pending_outbox() == []

    def test_a_sent_operation_leaves_the_outbox(self, store):
        outbox_id = store.enqueue("push_lead", {"lead_id": "l1"})
        store.mark_sent(outbox_id)
        assert store.pending_outbox() == []


# ---------------------------------------------------------------------------
# A product question is not a contact detail
#
# The phone pattern allowed spaces, hyphens and parentheses anywhere, so it ran
# across the boundary between two unrelated numbers. "APO 200 (6835421)" — a
# part code and a product id — matched as a phone number, and every price
# question about a product with a model code was answered with "I've passed
# your details to our team", while creating a lead for a buyer who had given
# nothing. The release gate caught it: 3 of 48 cases.
# ---------------------------------------------------------------------------
class TestProductQuestionsAreNotPhoneNumbers:
    @pytest.mark.parametrize("question", [
        "What is the price of the Apollo RT CT Dual Head Pressure Injector APO 200 (6835421)?",
        "What is the price of the Apollo RT CT Single Head Pressure Injector APO 100 (6797484)?",
        "What is the price of the Apollo RT Cath Lab Pressure Injector RT-150 (6835567)?",
        "what is the price of the X-Ray Cassette 270691?",
        "give me 10 pieces of the ultrasound gel",
    ])
    def test_no_phone_is_extracted(self, question):
        assert not extract_lead_details(question).get("phone"), question

    @pytest.mark.parametrize("text,expected", [
        ("call me on 98200 11122", "+919820011122"),
        ("my number is +91 98000 00000", "+919800000000"),
        ("reach me at (022) 4040 5050", "+912240405050"),
        ("you can call me on 080-4547-7795", "+918045477795"),
    ])
    def test_real_numbers_are_still_extracted(self, text, expected):
        assert extract_lead_details(text).get("phone") == expected

    def test_a_parenthesis_never_follows_a_digit(self):
        """A grouping parenthesis only ever opens a group, so it cannot appear
        mid-number — which is the rule that stops the pattern spanning two
        unrelated figures."""
        assert not extract_lead_details("APO 200 (6835421)").get("phone")

    def test_an_implausible_digit_count_is_refused(self):
        """E.164 allows at most 15 digits and this business's shortest number is
        10. Anything outside that is a product id or a quantity."""
        assert normalise_phone("6835421") is None
        assert normalise_phone("12345") is None
        assert normalise_phone("1234567890123456789") is None


# ---------------------------------------------------------------------------
# US-022 — handoffs that can actually be opened, notified and reviewed
# ---------------------------------------------------------------------------
class TestUS022HandoffsAndNotification:
    """The audit found three defects, all live:

    * `handoff_id=""` was inserted verbatim, so the first handoff took the
      empty PRIMARY KEY and every later one died on the UNIQUE constraint,
      swallowed into `dispatched=False` — **one handoff per database lifetime**.
    * the handoff's `lead_id` was the conversation id, so it linked to no lead.
    * nothing notified anyone, and no route exposed the queue.
    """

    def _store(self):
        from anita.crm import MockCrmStore

        return MockCrmStore(":memory:")

    def _record(self, lead_id="", conversation_id="c1"):
        from anita.escalation import EscalationRecord

        return EscalationRecord(
            lead_id=lead_id, conversation_id=conversation_id, reason="emotion=angry",
            score=-1.0, emotion="angry", category="Hot", dispatched=False,
        )

    def test_two_handoffs_for_different_leads_both_persist(self):
        from anita.crm import Handoff

        store = self._store()
        first = store.record_handoff(Handoff(handoff_id="", lead_id="lead-a", reason="angry"))
        second = store.record_handoff(Handoff(handoff_id="", lead_id="lead-b", reason="angry"))

        assert first.handoff_id and second.handoff_id
        assert first.handoff_id != second.handoff_id
        assert len(store.list_handoffs()) == 2

    def test_idempotency_per_lead_is_preserved(self):
        from anita.crm import Handoff

        store = self._store()
        store.record_handoff(Handoff(handoff_id="", lead_id="lead-a", reason="angry"))
        store.record_handoff(Handoff(handoff_id="", lead_id="lead-a", reason="angry"))
        assert len(store.list_handoffs()) == 1

    def test_anonymous_escalations_are_per_conversation(self):
        """No lead? Two conversations must not collapse into one handoff, and
        a conversation id must never be stored as a lead id."""
        from anita.crm import Handoff

        store = self._store()
        store.record_handoff(
            Handoff(handoff_id="", lead_id="", conversation_id="conv-1", reason="angry")
        )
        store.record_handoff(
            Handoff(handoff_id="", lead_id="", conversation_id="conv-2", reason="angry")
        )
        store.record_handoff(
            Handoff(handoff_id="", lead_id="", conversation_id="conv-1", reason="angry")
        )

        rows = store.list_handoffs()
        assert len(rows) == 2, "conv-1 deduped, conv-2 separate"
        assert all(row.lead_id == "" for row in rows)
        assert {row.conversation_id for row in rows} == {"conv-1", "conv-2"}

    def test_a_delivered_notification_moves_the_handoff_to_notified(self):
        from anita.escalation import EscalationSink

        store = self._store()
        notified = []

        def notifier(handoff):
            notified.append(handoff.handoff_id)
            return True

        sink = EscalationSink(store=None, crm=store, notifier=notifier)
        record = self._record(lead_id="lead-a")
        handoff_id = sink._open_handoff(record)   # `__call__` assigns it to the record

        assert record.notified is True
        assert notified == [handoff_id]
        assert store.list_handoffs()[0].state == "notified"
        assert store.list_handoffs()[0].notified_at

    def test_no_notifier_leaves_the_handoff_open_and_quiet(self):
        from anita.escalation import EscalationSink

        store = self._store()
        sink = EscalationSink(store=None, crm=store, notifier=None)
        record = self._record(lead_id="lead-a")
        sink._open_handoff(record)

        assert record.notified is False
        assert store.list_handoffs()[0].state == "open", "recorded, awaiting a human"

    def test_a_broken_notifier_does_not_fail_the_escalation(self):
        from anita.escalation import EscalationSink

        store = self._store()

        def notifier(handoff):
            raise OSError("smtp down")

        sink = EscalationSink(store=None, crm=store, notifier=notifier)
        record = self._record(lead_id="lead-a")
        sink._open_handoff(record)

        assert record.dispatched is False, "the sink flags it honestly"
        assert store.list_handoffs()[0].state == "open"
        assert record.notified is False

    def test_the_notifier_is_absent_without_configuration(self):
        from anita.notify import from_environment

        assert from_environment({}) is None
        assert from_environment({"SMTP_HOST": "smtp.example.com"}) is None, "no recipient"
        built = from_environment(
            {"SMTP_HOST": "smtp.example.com", "SALES_NOTIFY_EMAIL": "sales@example.com"}
        )
        assert built is not None and built.to == "sales@example.com"

    def test_the_admin_queue_lists_handoffs(self):
        from anita.crm import Handoff
        from anita.surfaces import AdminSurface

        store = self._store()
        store.record_handoff(Handoff(handoff_id="", lead_id="lead-a", reason="angry"))

        surface = AdminSurface(crm=store)
        response = surface.handoffs()
        assert response.status == 200
        assert response.body["open"] == 1
        assert response.body["handoffs"][0]["lead_id"] == "lead-a"
        assert response.body["handoffs"][0]["conversation_id"] == ""

    def test_the_admin_queue_says_so_when_the_crm_is_missing(self):
        from anita.surfaces import AdminSurface

        response = AdminSurface(crm=None).handoffs()
        assert response.status == 503
        assert response.body["unavailable"] is True

    def test_the_route_is_registered_and_authenticated(self):
        from anita.server import METHOD_AND_PATH
        from anita.surfaces import ROUTE_TABLE, Route, Surface

        assert METHOD_AND_PATH[Route.ADMIN_HANDOFFS] == ("GET", "/api/admin/handoffs")
        assert ROUTE_TABLE[Route.ADMIN_HANDOFFS] == (Surface.ADMIN_CONSOLE, True)
