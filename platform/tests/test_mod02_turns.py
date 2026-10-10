"""US-006 — turn serialisation, persistence and the review read model.

The failures this story exists to prevent are all reproduced here: an
interleaved transcript, a duplicated sequence number, a turn lost when the
store is down, a summary written twice, and a transcript that expired being
rendered as if the conversation never happened.
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.channels import Channel, InboundEnvelope, InboundEnvelope as _Env  # noqa: E402
from anita.conversation_store import (  # noqa: E402
    ANSWERED,
    CLASSIFIED,
    CLOSED,
    FAILED,
    ConversationStore,
    DuplicateTurn,
    NewTurn,
    ReplayQueue,
    TurnRecorder,
)
from anita.orchestrator import (  # noqa: E402
    Conversation,
    ConversationJanitor,
    ConversationQueue,
    ConversationRegistry,
    Orchestrator,
    QueueFull,
)
from anita.surfaces import AdminSurface, ChatSurface  # noqa: E402

NOW = datetime(2026, 10, 4, 18, 0, 0, tzinfo=timezone.utc)


def _store():
    return ConversationStore(":memory:")


def _conversation(store, **kwargs):
    defaults = dict(channel_kind="web_chat", identity_type="session",
                    identity_value="s1")
    defaults.update(kwargs)
    return store.create_conversation(**defaults)


def _turn(conversation_id, role="user", text="hello", **kwargs):
    defaults = dict(state=CLASSIFIED, classification="catalogue", route="retrieval")
    defaults.update(kwargs)
    return NewTurn(conversation_id=conversation_id, role=role, text=text, **defaults)


# ---------------------------------------------------------------------------
# The store: sequence, dedupe, paging
# ---------------------------------------------------------------------------
class TestTurnAppend:
    def test_sequence_is_contiguous_per_conversation(self):
        store = _store()
        first = _conversation(store)
        second = _conversation(store, identity_value="s2")

        seqs = [store.append_turn(_turn(first.id, text=f"t{n}")).seq for n in range(5)]
        other = store.append_turn(_turn(second.id, text="other")).seq

        assert seqs == [1, 2, 3, 4, 5]
        assert other == 1, "sequence must be per conversation, not global"

    def test_twenty_concurrent_writers_leave_no_gaps_or_duplicates(self):
        """T-11. The audit reproduced interleaving; this is the fix's floor."""
        store = _store()
        conversation = _conversation(store)
        errors: list[Exception] = []

        def write(n: int) -> None:
            try:
                store.append_turn(_turn(conversation.id, text=f"turn {n}"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors
        turns = store.page_turns(conversation.id, limit=100)
        assert len(turns) == 20
        assert [turn.seq for turn in turns] == list(range(1, 21))

    def test_a_duplicate_provider_event_returns_the_stored_turn(self):
        """T-07: one row, and the caller gets the turn that exists."""
        store = _store()
        conversation = _conversation(store)
        first = store.append_turn(_turn(conversation.id, provider_event_id="SM-1"))

        with pytest.raises(DuplicateTurn) as excinfo:
            store.append_turn(_turn(conversation.id, text="again",
                                    provider_event_id="SM-1"))

        assert excinfo.value.turn.id == first.id
        assert store.turn_count(conversation.id) == 1

    def test_an_answered_turn_carries_its_route_and_reference(self):
        store = _store()
        conversation = _conversation(store)
        store.append_turn(_turn(
            conversation.id, role="assistant", text="₹60/Piece",
            state=ANSWERED, route="deterministic", answer_ref="abc",
            grounding_refs=("https://anita.example/gel",), latency_ms=42,
        ))
        record = store.page_turns(conversation.id)[0]
        assert record.state == ANSWERED
        assert record.route == "deterministic"
        assert record.grounding_refs == ("https://anita.example/gel",)
        assert record.latency_ms == 42

    def test_a_failed_turn_carries_its_reason(self):
        store = _store()
        conversation = _conversation(store)
        store.append_turn(_turn(conversation.id, role="assistant", text="",
                                state=FAILED, failure_reason="timeout"))
        assert store.page_turns(conversation.id)[0].failure_reason == "timeout"

    def test_paging_cursor_walks_the_transcript(self):
        store = _store()
        conversation = _conversation(store)
        for n in range(250):
            store.append_turn(_turn(conversation.id, text=f"turn {n}"))

        first = store.page_turns(conversation.id, limit=100)
        second = store.page_turns(conversation.id, after_seq=first[-1].seq, limit=100)
        third = store.page_turns(conversation.id, after_seq=second[-1].seq, limit=100)

        assert len(first) == len(second) == 100
        assert len(third) == 50
        assert [t.seq for t in first + second + third] == list(range(1, 251))


# ---------------------------------------------------------------------------
# Durability when the store is down (AC-4, TAC-3)
# ---------------------------------------------------------------------------
class TestReplay:
    def test_turns_are_buffered_when_the_store_fails(self):
        recorder = TurnRecorder(None)  # no store at all
        assert recorder.record(_turn("c1")) is None  # never raises
        assert recorder.enabled is False

    def test_a_failed_write_is_replayed_and_drained(self):
        store = _store()
        conversation = _conversation(store)
        recorder = TurnRecorder(store, ReplayQueue(capacity=10))

        broken = _store()

        def boom(_turn):
            raise RuntimeError("database is locked")

        broken.append_turn = boom  # type: ignore[method-assign]
        recorder.store = broken
        assert recorder.record(_turn(conversation.id)) is None
        assert recorder.replay.pending == 1

        recorder.store = store
        assert recorder.drain() == 1
        assert recorder.replay.pending == 0
        assert store.turn_count(conversation.id) == 1

    def test_the_buffer_drops_oldest_and_counts(self):
        replay = ReplayQueue(capacity=3)
        for n in range(5):
            replay.put(_turn("c1", text=f"turn {n}"))
        assert replay.pending == 3
        assert replay.dropped == 2

    def test_drain_keeps_the_queue_when_the_store_is_still_down(self):
        store = _store()
        recorder = TurnRecorder(store, ReplayQueue(capacity=10))
        store.append_turn = lambda turn: (_ for _ in ()).throw(RuntimeError("down"))  # type: ignore
        recorder.record(_turn("c1"))
        recorder.record(_turn("c1"))
        assert recorder.drain() == 0
        assert recorder.replay.pending == 2


# ---------------------------------------------------------------------------
# Serialisation (AC-3)
# ---------------------------------------------------------------------------
class TestConversationQueue:
    def test_two_turns_never_overlap(self):
        queue = ConversationQueue(depth=5)
        events: list[str] = []

        def work(name: str, delay: float):
            def run():
                events.append(f"{name}:start")
                time.sleep(delay)
                events.append(f"{name}:end")
            return run

        threads = [
            threading.Thread(target=lambda: queue.submit("c1", work("A", 0.05))),
            threading.Thread(target=lambda: queue.submit("c1", work("B", 0.01))),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Whichever went first, its end precedes the other's start: the whole
        # turn is serialised, not just the append.
        assert events in (["A:start", "A:end", "B:start", "B:end"],
                          ["B:start", "B:end", "A:start", "A:end"])

    def test_overflow_re_prompts_rather_than_dropping(self):
        queue = ConversationQueue(depth=1)
        release = threading.Event()
        started = threading.Event()

        def slow():
            started.set()
            release.wait(2)
            return "done"

        thread = threading.Thread(target=lambda: queue.submit("c1", slow))
        thread.start()
        assert started.wait(2)
        with pytest.raises(QueueFull):
            queue.submit("c1", lambda: "second")
        release.set()
        thread.join()
        assert queue.overflowed == 1

    def test_a_second_conversation_is_not_blocked(self):
        queue = ConversationQueue(depth=1)
        release = threading.Event()
        started = threading.Event()

        def slow():
            started.set()
            release.wait(2)

        thread = threading.Thread(target=lambda: queue.submit("c1", slow))
        thread.start()
        assert started.wait(2)
        assert queue.submit("c2", lambda: "ran") == "ran"
        release.set()
        thread.join()


# ---------------------------------------------------------------------------
# The orchestrator writes the transcript (AC-1, TAC-4)
# ---------------------------------------------------------------------------
def _orchestrator(**kwargs):
    store = kwargs.pop("store", None) or _store()
    recorder = TurnRecorder(store, ReplayQueue(capacity=100))
    registry = ConversationRegistry(store=store, recorder=recorder, **kwargs)
    return Orchestrator(registry), store


class TestTurnsArePersisted:
    def test_three_turns_persist_with_sequence_and_route(self):
        orchestrator, store = _orchestrator()
        envelope = _Env(channel=Channel.WEB_CHAT, text="hi", session_id="s1")

        conversation, routing = orchestrator.begin_turn(envelope)
        orchestrator.complete_turn(conversation, "Hello — how can I help?")
        orchestrator.begin_turn(_Env(channel=Channel.WEB_CHAT, text="what is the price?",
                                     session_id="s1"))
        orchestrator.complete_turn(conversation, "₹60 per piece.")

        turns = store.page_turns(conversation.conversation_id)
        assert [t.seq for t in turns] == [1, 2, 3, 4]
        assert [t.role for t in turns] == ["user", "assistant", "user", "assistant"]
        assert turns[0].state == CLASSIFIED
        assert turns[0].classification == "introduction"
        assert turns[0].route == "greet"
        assert turns[2].classification == "commercial"
        assert turns[2].route == "deterministic"
        assert turns[1].state == ANSWERED

    def test_a_failed_answer_persists_as_failed_with_a_reason(self):
        orchestrator, store = _orchestrator()
        conversation, _ = orchestrator.begin_turn(
            _Env(channel=Channel.WEB_CHAT, text="hi", session_id="s2"))
        orchestrator.complete_turn(conversation, "unavailable", failed=True,
                                   failure_reason="timeout")
        assistant = [t for t in store.page_turns(conversation.conversation_id)
                     if t.role == "assistant"][0]
        assert assistant.state == FAILED
        assert assistant.failure_reason == "timeout"

    def test_the_provider_event_id_dedupes_a_redelivery(self):
        orchestrator, store = _orchestrator()
        envelope = _Env(channel=Channel.WHATSAPP, text="MOQ?", identity="+919820011122",
                        raw={"MessageSid": "SM-9"})
        first, _ = orchestrator.begin_turn(envelope)
        second, _ = orchestrator.begin_turn(envelope)
        assert first.conversation_id == second.conversation_id
        assert store.turn_count(first.conversation_id) == 1

    def test_a_turn_survives_a_recorder_target_that_is_down(self):
        """AC-4: the buyer still gets the answer; the turn is buffered."""
        orchestrator, store = _orchestrator()
        recorder = orchestrator.recorder
        original = store.append_turn

        def boom(_turn):
            raise RuntimeError("database is locked")

        store.append_turn = boom  # type: ignore[method-assign]
        try:
            conversation, _ = orchestrator.begin_turn(
                _Env(channel=Channel.WEB_CHAT, text="hi", session_id="s3"))
            orchestrator.complete_turn(conversation, "hello")
        finally:
            store.append_turn = original  # type: ignore[method-assign]

        assert recorder.replay.pending == 2
        assert conversation.turn_count == 2  # the buyer's answer was delivered

        assert recorder.drain() == 2
        assert store.turn_count(conversation.conversation_id) == 2


class TestSummaries:
    def test_a_summary_is_written_once_however_many_writers(self):
        """TAC-5: two writes produce one summary."""
        store = _store()
        conversation = store.create_conversation(
            channel_kind="web_chat", identity_type="session", identity_value="s1")
        assert store.write_summary(conversation.id, "first") is True
        assert store.write_summary(conversation.id, "second") is False
        assert store.get_conversation(conversation.id).summary == "first"

    def test_closing_writes_the_summary(self):
        orchestrator, store = _orchestrator()
        conversation, _ = orchestrator.begin_turn(
            _Env(channel=Channel.WEB_CHAT, text="hi", session_id="s4"))
        orchestrator.complete_turn(conversation, "hello")
        orchestrator.close(conversation, reason="idle")

        record = store.get_conversation(conversation.conversation_id)
        assert record.state == CLOSED
        assert "2 turns" in (record.summary or "")
        assert "idle" in (record.summary or "")

    def test_teardown_summarises_without_closing(self):
        """A call ending must not fork the thread the buyer continues later."""
        orchestrator, store = _orchestrator()
        conversation, _ = orchestrator.begin_turn(
            _Env(channel=Channel.VOICE_INBOUND, text="hi", identity="+919820011122"))
        orchestrator.complete_turn(conversation, "hello")
        orchestrator.finalise(conversation, reason="voice_stop")

        record = store.get_conversation(conversation.conversation_id)
        assert record.summary and "voice_stop" in record.summary
        assert record.state != CLOSED

    def test_the_idle_sweep_closes_and_summarises(self):
        registry = ConversationRegistry(idle_window_seconds=0.001, store=_store(),
                                        recorder=None)
        orchestrator = Orchestrator(registry)
        conversation, _ = orchestrator.begin_turn(
            _Env(channel=Channel.WEB_CHAT, text="hi", session_id="s5"))
        time.sleep(0.01)
        assert orchestrator.sweep_idle() == 1
        record = registry.store.get_conversation(conversation.conversation_id)
        assert record.state == CLOSED
        assert record.summary

    def test_a_conversation_with_no_turns_says_so(self):
        conversation = Conversation(conversation_id="c1", key="k")
        assert "No transcript" in ConversationRegistry.summary_for(conversation)


# ---------------------------------------------------------------------------
# The review read model (AC-2, AC-6, AC-7)
# ---------------------------------------------------------------------------
class TestReviewReadModel:
    def _surface(self, store):
        return AdminSurface(registry=ConversationRegistry(store=store), store=store)

    def test_the_transcript_is_paged_with_a_cursor(self):
        store = _store()
        conversation = _conversation(store)
        for n in range(500):
            store.append_turn(_turn(conversation.id, text=f"turn {n}"))

        admin = self._surface(store)
        first = admin.conversation(conversation_id=conversation.id).body
        assert len(first["turns"]) == 100
        assert first["turn_count"] == 500

        second = admin.conversation(conversation_id=conversation.id,
                                    after_seq=first["next_after_seq"]).body
        assert second["turns"][0]["seq"] == 101

    def test_an_expired_transcript_is_labelled_with_its_date(self):
        """AC-7: 'deleted under retention' is not 'there was nothing'."""
        store = _store()
        conversation = _conversation(store)
        store.append_turn(_turn(conversation.id))
        store.expire_transcript(conversation.id, now=NOW)

        body = self._surface(store).conversation(conversation_id=conversation.id).body
        assert body["transcript_state"] == "expired"
        assert body["transcript_expired_at"].startswith("2026-10-04")
        assert body["turns"] == []
        # The conversation row survives — only the transcript is gone.
        assert store.get_conversation(conversation.id) is not None

    def test_a_conversation_with_no_turns_is_empty_not_expired(self):
        store = _store()
        conversation = _conversation(store)
        body = self._surface(store).conversation(conversation_id=conversation.id).body
        assert body["transcript_state"] == "empty"
        assert body["turns"] == []

    def test_an_unknown_conversation_is_404_not_a_stack_trace(self):
        body = self._surface(_store()).conversation(conversation_id="nope")
        assert body.status == 404

    def test_repeated_reads_are_idempotent(self):
        store = _store()
        conversation = _conversation(store)
        store.append_turn(_turn(conversation.id))
        admin = self._surface(store)
        assert (admin.conversation(conversation_id=conversation.id).body
                == admin.conversation(conversation_id=conversation.id).body)

    def test_the_conversation_list_is_paged(self):
        store = _store()
        for n in range(30):
            store.create_conversation(
                channel_kind="web_chat", identity_type="session",
                identity_value=f"s{n}", now=NOW - timedelta(minutes=n))
        body = self._surface(store).conversations(limit=10, offset=10).body
        assert body["count"] == 30
        assert body["offset"] == 10
        assert len(body["conversations"]) == 10


# ---------------------------------------------------------------------------
# The chat surface: the serialisation fix end to end (AC-3)
# ---------------------------------------------------------------------------
class TestChatSerialisation:
    class _Retriever:
        """Not relevant — every question takes the decline path, which is the
        cheapest answer that still runs the whole turn."""

        def retrieve(self, query, **kwargs):
            from anita.retrieval import GateDecision, RetrievalResult

            return RetrievalResult(
                chunks=[],
                decision=GateDecision(relevant=False, reason="no context"),
                query=query,
            )

    def test_concurrent_messages_do_not_interleave(self):
        from anita.surfaces import ChatSurface

        store = _store()
        recorder = TurnRecorder(store, ReplayQueue(capacity=100))
        orchestrator = Orchestrator(
            ConversationRegistry(store=store, recorder=recorder))
        chat = ChatSurface(orchestrator, retriever=self._Retriever())

        def send(text: str) -> None:
            chat.post_message(message=text, session_id="race-1")

        threads = [threading.Thread(target=send, args=(f"question {n}",))
                   for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        conversation = orchestrator.registry.get("race-1")
        turns = store.page_turns(conversation.conversation_id)
        roles = [turn.role for turn in turns]
        # Every question is immediately followed by its own answer — the
        # audit's `[user A, user B, assistant B, assistant A]` cannot occur.
        assert roles == ["user", "assistant"] * 6
        user_seqs = [t.seq for t in turns if t.role == "user"]
        answer_seqs = [t.seq for t in turns if t.role == "assistant"]
        assert all(b > a for a, b in zip(user_seqs, answer_seqs))


class TestVoiceSerialisation:
    """§8.25 — the voice leg takes the same per-conversation slot as chat.

    US-006's queue was wired into `ChatSurface.post_message` and the voice
    pipeline was missed: it called `begin_turn` directly, so a conversation a
    buyer had also touched over web chat could still interleave. The
    reproduction is the chat test's race, driven through the voice entry point.
    """

    class _Retriever:
        def retrieve(self, query, **kwargs):
            from anita.retrieval import GateDecision, RetrievalResult

            return RetrievalResult(
                chunks=[],
                decision=GateDecision(relevant=False, reason="no context"),
                query=query,
            )

    def test_concurrent_voice_turns_do_not_interleave(self):
        from anita.answer_engine import AnswerEngine
        from anita.voice_pipeline import install, run_rag_query_sync

        store = _store()
        recorder = TurnRecorder(store, ReplayQueue(capacity=100))
        orchestrator = Orchestrator(
            ConversationRegistry(store=store, recorder=recorder))
        install(orchestrator, AnswerEngine(retriever=self._Retriever()))

        def speak(n: int) -> None:
            run_rag_query_sync(
                "", "voice", f"question {n}",
                session_key="+919820055555", caller="+919820055555",
            )

        threads = [threading.Thread(target=speak, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        conversation = orchestrator.registry.get("+919820055555")
        turns = store.page_turns(conversation.conversation_id)
        assert [turn.role for turn in turns] == ["user", "assistant"] * 6, (
            "a voice turn answered outside the conversation's slot interleaves"
        )

    def test_the_voice_turn_goes_through_submit_turn(self):
        """The mechanism, not just the effect: the pipeline must ask the queue
        for the slot rather than calling `begin_turn` on its own."""
        from anita.answer_engine import AnswerEngine
        from anita.voice_pipeline import install, run_rag_query_sync

        store = _store()
        orchestrator = Orchestrator(ConversationRegistry(store=store))
        submitted: list[str] = []
        real_submit = orchestrator.submit_turn

        def spy(conversation_id, work):
            submitted.append(conversation_id)
            return real_submit(conversation_id, work)

        orchestrator.submit_turn = spy
        install(orchestrator, AnswerEngine(retriever=self._Retriever()))

        run_rag_query_sync("", "voice", "hello",
                           session_key="+919820066666", caller="+919820066666")

        conversation = orchestrator.registry.get("+919820066666")
        assert submitted == [conversation.conversation_id]


# ---------------------------------------------------------------------------
# The janitor
# ---------------------------------------------------------------------------
class TestJanitor:
    def test_the_janitor_sweeps_and_replays(self):
        store = _store()
        recorder = TurnRecorder(store, ReplayQueue(capacity=10))
        registry = ConversationRegistry(idle_window_seconds=0.001, store=store,
                                        recorder=recorder)
        orchestrator = Orchestrator(registry)
        conversation, _ = orchestrator.begin_turn(
            _Env(channel=Channel.WEB_CHAT, text="hi", session_id="s6"))

        # A buffered turn from an earlier outage drains on the next tick. The
        # store is made to fail the way a locked database does.
        original = store.append_turn

        def boom(_turn):
            raise RuntimeError("database is locked")

        store.append_turn = boom  # type: ignore[method-assign]
        recorder.record(_turn(conversation.conversation_id, text="buffered"))
        store.append_turn = original  # type: ignore[method-assign]
        assert recorder.replay.pending == 1

        time.sleep(0.01)
        result = ConversationJanitor(orchestrator=orchestrator).run_once()
        assert result["swept"] == 1
        assert result["replayed"] == 1
        assert store.turn_count(conversation.conversation_id) == 2

    def test_a_lost_summary_is_retried_on_the_next_tick(self):
        """§8.28: `unsummarised_closed` existed to find these and had no
        caller, so a summary lost to a transient store failure stayed lost."""
        store = _store()
        registry = ConversationRegistry(idle_window_seconds=0.001, store=store)
        orchestrator = Orchestrator(registry)
        conversation, _ = orchestrator.begin_turn(
            _Env(channel=Channel.WEB_CHAT, text="hi", session_id="s-retry"))

        original = store.write_summary

        def boom(*args, **kwargs):
            raise RuntimeError("database is locked")

        store.write_summary = boom  # type: ignore[method-assign]
        time.sleep(0.01)
        ConversationJanitor(orchestrator=orchestrator).run_once()  # summary lost
        assert store.unsummarised_closed() == [conversation.conversation_id]

        store.write_summary = original  # type: ignore[method-assign]
        result = ConversationJanitor(orchestrator=orchestrator).run_once()

        assert result["resummarised"] == 1
        assert store.unsummarised_closed() == []

    def test_a_restart_summarises_what_the_old_process_could_not(self):
        """§8.34: the retry looked only in `registry._by_key`, so in a process
        that had just started — when that map is empty — every id the pending
        list returned was filtered to nothing and the row stayed unsummarised
        for good. The retry now rebuilds from the store."""
        store = _store()
        first = Orchestrator(ConversationRegistry(idle_window_seconds=0.001, store=store))
        conversation, _ = first.begin_turn(
            _Env(channel=Channel.WEB_CHAT, text="hi", session_id="s-restart"))

        original = store.write_summary

        def boom(*args, **kwargs):
            raise RuntimeError("database is locked")

        store.write_summary = boom  # type: ignore[method-assign]
        time.sleep(0.01)
        ConversationJanitor(orchestrator=first).run_once()   # summary lost, row closed
        assert store.unsummarised_closed() == [conversation.conversation_id]
        store.write_summary = original  # type: ignore[method-assign]

        # A new process. It has never held this conversation, which is exactly
        # the case the old retry could not repair.
        second = Orchestrator(ConversationRegistry(store=store))
        assert second.registry.count() == 0

        result = ConversationJanitor(orchestrator=second).run_once()

        assert result["resummarised"] == 1
        assert store.unsummarised_closed() == []
        record = store.get_conversation(conversation.conversation_id)
        assert record is not None and record.summary

    def test_a_rebuilt_summary_counts_every_turn_not_the_context_tail(self):
        """`_seed_context` keeps only the last few turns, because feeding a
        prompt is what it is for. A summary is a record: rebuilt from the
        context tail it would under-report a long conversation, which is the
        one thing a record must not do."""
        store = _store()
        recorder = TurnRecorder(store, ReplayQueue(capacity=50))
        registry = ConversationRegistry(store=store, recorder=recorder)
        orchestrator = Orchestrator(registry)
        conversation, _ = orchestrator.begin_turn(
            _Env(channel=Channel.WEB_CHAT, text="turn 0", session_id="s-long"))
        assert registry.context_turns < 12, "this test needs more turns than the tail"

        for n in range(1, 12):
            recorder.record(_turn(conversation.conversation_id, text=f"turn {n}"))
        stored = store.turn_count(conversation.conversation_id)
        assert stored > registry.context_turns

        # Rebuilt in a process that never held it, and from the store alone.
        rebuilt = registry.from_store(conversation.conversation_id)

        assert rebuilt is not None
        assert len(rebuilt.turns) == stored, "a summary must count the whole thread"
        assert "12 turns" in registry.summary_for(rebuilt, reason="retry")
        assert rebuilt.channels_seen == [Channel.WEB_CHAT.value]

    def test_rebuilding_an_unknown_conversation_is_a_miss_not_an_error(self):
        store = _store()
        registry = ConversationRegistry(store=store)

        assert registry.from_store("no-such-conversation") is None
        assert registry.from_store("") is None

    def test_an_unreadable_classification_does_not_lose_the_turn(self):
        """A label written by an older build must not make a whole summary
        fail — the turn is still worth counting."""
        from anita.orchestrator import _intent_or_none

        assert _intent_or_none("catalogue") is not None
        assert _intent_or_none("a-route-this-build-removed") is None
        assert _intent_or_none(None) is None

    def test_retention_expires_a_transcript_and_keeps_the_row(self):
        """`DG-11`: `expire_transcript` had no caller, so the review's
        `expired` state was unreachable in production."""
        store = _store()
        recorder = TurnRecorder(store, ReplayQueue(capacity=10))
        registry = ConversationRegistry(store=store, recorder=recorder)
        orchestrator = Orchestrator(registry)
        conversation, _ = orchestrator.begin_turn(
            _Env(channel=Channel.WEB_CHAT, text="hi", session_id="s-exp"))
        recorder.record(_turn(conversation.conversation_id, text="hi"))

        store._connection.execute(  # noqa: SLF001 - age the conversation
            "UPDATE conversation SET last_activity_at = ? WHERE id = ?",
            ((NOW - timedelta(days=120)).isoformat(), conversation.conversation_id),
        )
        result = ConversationJanitor(
            orchestrator=orchestrator, retention_days=90
        ).run_once()

        assert result["expired"] == 1
        assert store.turn_count(conversation.conversation_id) == 0
        # The row survives, so the review can say the transcript expired *with
        # its date* rather than showing a conversation that looks empty.
        assert store.get_conversation(conversation.conversation_id) is not None
        assert store.transcript_state(conversation.conversation_id) == "expired"

    def test_retention_is_off_when_no_window_is_configured(self):
        """A deployment that has not decided a window keeps everything."""
        store = _store()
        recorder = TurnRecorder(store, ReplayQueue(capacity=10))
        registry = ConversationRegistry(store=store, recorder=recorder)
        orchestrator = Orchestrator(registry)
        conversation, _ = orchestrator.begin_turn(
            _Env(channel=Channel.WEB_CHAT, text="hi", session_id="s-off"))
        recorder.record(_turn(conversation.conversation_id, text="hi"))

        store._connection.execute(  # noqa: SLF001
            "UPDATE conversation SET last_activity_at = ? WHERE id = ?",
            ((NOW - timedelta(days=3650)).isoformat(), conversation.conversation_id),
        )
        result = ConversationJanitor(orchestrator=orchestrator).run_once()

        assert result["expired"] == 0
        assert store.turn_count(conversation.conversation_id) > 0
