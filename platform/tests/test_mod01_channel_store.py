"""US-001 — the channel tables, and the two protections they carry.

These are not schema tests. Each table exists to make a specific failure
impossible, and what is asserted here is that it does:

* a replayed `CallSid` cannot open a second stream (Twilio retries webhooks)
* a call to a number this instance does not own is refused *and recorded*
* a session is keyed on the stream id, so a caller who withheld their number
  still has a stable identity
"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from anita.channel_store import (  # noqa: E402
    ACCEPTED,
    DUPLICATE,
    REJECTED,
    ChannelStore,
)

INSTANCE = "anita"
NUMBER = "+12792365960"
OTHER = "+15550001111"


def _store() -> ChannelStore:
    """A fresh in-memory store with this instance's number declared."""
    store = ChannelStore(":memory:")
    # Mirrors what assembly does at boot, secret_ref included.
    store.register_endpoint(
        channel_kind="voice_inbound", provider="twilio",
        address=NUMBER, instance_id=INSTANCE,
        secret_ref="TWILIO_AUTH_TOKEN",
    )
    return store


class TestEndpoints:
    def test_registering_twice_returns_the_same_endpoint(self):
        """It is called on every boot; it must not accumulate rows."""
        store = _store()
        first = store.register_endpoint(
            channel_kind="voice_inbound", provider="twilio",
            address=NUMBER, instance_id=INSTANCE,
        )
        second = store.register_endpoint(
            channel_kind="voice_inbound", provider="twilio",
            address=NUMBER, instance_id=INSTANCE,
        )
        assert first == second

    def test_the_number_resolves_to_this_instance(self):
        store = _store()
        found = store.find_endpoint(
            channel_kind="voice_inbound", address=NUMBER, instance_id=INSTANCE
        )
        assert found is not None
        assert found["provider"] == "twilio"

    def test_another_instances_number_is_not_found(self):
        """The guard that stops this product answering for the sibling's."""
        store = _store()
        assert store.find_endpoint(
            channel_kind="voice_inbound", address=OTHER, instance_id=INSTANCE
        ) is None

    def test_the_secret_is_stored_as_a_name_never_a_value(self):
        """REC-21: a secret's *name* is configuration; its value is not."""
        store = _store()
        found = store.find_endpoint(
            channel_kind="voice_inbound", address=NUMBER, instance_id=INSTANCE
        )
        assert found["secret_ref"] == "TWILIO_AUTH_TOKEN"


class TestReplayProtection:
    def test_the_first_delivery_is_accepted(self):
        store = _store()
        assert store.claim_event("CA1", channel_kind="voice_inbound") == ACCEPTED

    def test_the_second_delivery_of_the_same_call_is_a_duplicate(self):
        store = _store()
        store.claim_event("CA1", channel_kind="voice_inbound")
        assert store.claim_event("CA1", channel_kind="voice_inbound") == DUPLICATE

    def test_a_different_call_is_not_a_duplicate(self):
        store = _store()
        store.claim_event("CA1", channel_kind="voice_inbound")
        assert store.claim_event("CA2", channel_kind="voice_inbound") == ACCEPTED

    def test_concurrent_deliveries_claim_exactly_once(self):
        """The guarantee has to be the database's.

        Twilio's retry can land while the first delivery is still being
        processed. Reading first and inserting after would leave exactly the
        window a replay arrives in, so the INSERT *is* the check.
        """
        store = _store()
        outcomes: list[str] = []
        barrier = threading.Barrier(20)

        def claim() -> None:
            barrier.wait()
            outcomes.append(store.claim_event("CA-race", channel_kind="voice_inbound"))

        threads = [threading.Thread(target=claim) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert outcomes.count(ACCEPTED) == 1, "a replay could open a second stream"
        assert outcomes.count(DUPLICATE) == 19

    def test_an_empty_event_id_is_never_treated_as_a_duplicate(self):
        """Recording `''` would make the next un-identified event a false
        duplicate, which is worse than not deduplicating at all."""
        store = _store()
        assert store.claim_event("", channel_kind="voice_inbound") == ACCEPTED
        assert store.claim_event("", channel_kind="voice_inbound") == ACCEPTED

    def test_a_rejection_is_recorded_not_dropped(self):
        store = _store()
        store.mark_rejected("CA-bad", channel_kind="voice_inbound")
        assert store.event_disposition("CA-bad") == REJECTED

    def test_an_unknown_event_has_no_disposition(self):
        assert _store().event_disposition("never-seen") is None


class TestSessions:
    def test_a_session_is_keyed_on_the_stream_id(self):
        """AC-6: a caller who withholds their number still has one stable
        identity for the call, and the stream id is it."""
        store = _store()
        session_id = store.open_session(
            channel_kind="voice_inbound", session_key="MZ-1"
        )
        found = store.find_session(channel_kind="voice_inbound", session_key="MZ-1")
        assert found is not None
        assert found["id"] == session_id

    def test_opening_the_same_session_twice_is_idempotent(self):
        """A duplicate `start` must not begin a second conversation."""
        store = _store()
        first = store.open_session(channel_kind="voice_inbound", session_key="MZ-1")
        second = store.open_session(channel_kind="voice_inbound", session_key="MZ-1")
        assert first == second

    def test_closing_is_idempotent_and_records_the_first_close(self):
        store = _store()
        store.open_session(channel_kind="voice_inbound", session_key="MZ-1")
        store.close_session(channel_kind="voice_inbound", session_key="MZ-1")
        closed_at = store.find_session(
            channel_kind="voice_inbound", session_key="MZ-1"
        )["closed_at"]
        store.close_session(channel_kind="voice_inbound", session_key="MZ-1")
        assert store.find_session(
            channel_kind="voice_inbound", session_key="MZ-1"
        )["closed_at"] == closed_at

    def test_open_sessions_lists_what_a_restart_must_reconcile(self):
        """A process that dies mid-call leaves the row open, and an operator
        needs to see that rather than infer it from absence."""
        store = _store()
        store.open_session(channel_kind="voice_inbound", session_key="MZ-open")
        store.open_session(channel_kind="voice_inbound", session_key="MZ-closed")
        store.close_session(channel_kind="voice_inbound", session_key="MZ-closed")

        open_keys = {s["session_key"] for s in store.open_sessions()}
        assert open_keys == {"MZ-open"}


class TestSchemaIsIdempotent:
    def test_reopening_the_same_database_does_not_fail(self, tmp_path):
        """`CREATE TABLE IF NOT EXISTS`, verified against a real file rather
        than an in-memory database that vanishes between connections."""
        path = tmp_path / "channels.sqlite"
        first = ChannelStore(path)
        first.register_endpoint(
            channel_kind="voice_inbound", provider="twilio",
            address=NUMBER, instance_id=INSTANCE,
        )
        second = ChannelStore(path)  # must not raise

        assert second.find_endpoint(
            channel_kind="voice_inbound", address=NUMBER, instance_id=INSTANCE
        ) is not None

    def test_replay_protection_survives_a_restart(self, tmp_path):
        """The point of persisting the claim: a restart mid-retry must not
        accept the replay it already refused."""
        path = tmp_path / "channels.sqlite"
        ChannelStore(path).claim_event("CA1", channel_kind="voice_inbound")

        reopened = ChannelStore(path)
        assert reopened.claim_event("CA1", channel_kind="voice_inbound") == DUPLICATE
