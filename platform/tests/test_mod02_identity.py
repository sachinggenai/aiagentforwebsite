"""US-004 — cross-channel identity, the registry that survives a restart, and
the lead link.

The story's own hard cases: the same person on two channels, a conversation
that outlived the process, an anonymous session that must not merge with
anyone, a phone number in the format a human typed, and a stale thread that is
closed rather than silently reopened.
"""

from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.channels import Channel, InboundEnvelope  # noqa: E402
from anita.conversation_store import ConversationStore, ReplayQueue, TurnRecorder  # noqa: E402
from anita.orchestrator import (  # noqa: E402
    ConversationRegistry,
    ConversationState,
    IdentityType,
    Orchestrator,
    normalise_phone,
    resolve_identity,
)

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)


def _envelope(text="hi", *, channel=Channel.WEB_CHAT, identity=None, session=None, **raw):
    return InboundEnvelope(channel=channel, text=text, identity=identity,
                           session_id=session, raw=raw)


def _registry(store=None, *, idle=6 * 3600, recorder=True):
    store = store or ConversationStore(":memory:")
    return ConversationRegistry(
        idle_window_seconds=idle, store=store,
        recorder=TurnRecorder(store, ReplayQueue(10)) if recorder else None,
    ), store


# ---------------------------------------------------------------------------
# Identity precedence and normalisation (CO-1, edge case 9)
# ---------------------------------------------------------------------------
class TestIdentityResolution:
    @pytest.mark.parametrize("raw,expected", [
        ("9820011122", "+919820011122"),          # what a buyer types
        ("+91 98200 11122", "+919820011122"),     # E.164 with spaces
        ("+919820011122", "+919820011122"),       # what Twilio sends
        ("0091-9820011122", "+919820011122"),     # ITU access code form
        ("12345", None),                          # too short to be a phone
        ("acme@example.com", None),               # a handle, not a phone
    ])
    def test_phone_normalisation(self, raw, expected):
        assert normalise_phone(raw) == expected

    def test_the_same_person_in_two_formats_is_one_identity(self):
        """AC-1 depends on this: the web chat holds '98xxxxxxxx' and the
        carrier sends '+9198xxxxxxxx'."""
        typed = resolve_identity(_envelope(identity="9820011122"))
        carrier = resolve_identity(_envelope(identity="+919820011122"))
        assert typed.type is IdentityType.PHONE
        assert typed.key == carrier.key

    def test_precedence_is_phone_handle_session_anonymous(self):
        assert resolve_identity(
            _envelope(identity="+919820011122", session="s1")
        ).type is IdentityType.PHONE
        assert resolve_identity(
            _envelope(identity="acme@example.com", session="s1")
        ).type is IdentityType.HANDLE
        assert resolve_identity(_envelope(session="s1")).type is IdentityType.SESSION
        assert resolve_identity(_envelope()).type is IdentityType.ANONYMOUS

    def test_a_blank_key_mints_one_rather_than_sharing_a_bucket(self):
        first = resolve_identity(_envelope())
        second = resolve_identity(_envelope())
        assert first.value != second.value

    def test_handles_are_case_folded(self):
        assert resolve_identity(_envelope(identity="Acme@Example.COM")).key == \
            resolve_identity(_envelope(identity="acme@example.com")).key


# ---------------------------------------------------------------------------
# Continuity (AC-1, AC-2, AC-4, AC-5, AC-6)
# ---------------------------------------------------------------------------
class TestContinuity:
    def test_the_same_identity_across_channels_is_one_conversation(self):
        """BRD-01: one thread regardless of channel; the products discussed on
        web chat are in the voice conversation's context because it *is* the
        conversation."""
        registry, store = _registry()
        orchestrator = Orchestrator(registry)

        chat, _ = orchestrator.begin_turn(_envelope(
            "what does the FCR Prima T2 weigh?", identity="9820011122"))
        orchestrator.complete_turn(chat, "It weighs 4.2 kg.")
        voice, _ = orchestrator.begin_turn(_envelope(
            "and the price?", channel=Channel.VOICE_INBOUND, identity="+919820011122"))

        assert voice.conversation_id == chat.conversation_id
        assert set(voice.channels_seen) == {"web_chat", "voice_inbound"}
        # The prior question is in the transcript the voice turn can see.
        assert any("FCR Prima T2" in t.content for t in voice.turns)

    def test_duplicate_starts_produce_one_conversation(self):
        """AC-5 / TAC-3: ten duplicate media-stream starts, one conversation."""
        registry, store = _registry()
        envelope = _envelope("", channel=Channel.VOICE_INBOUND, session="stream-7")
        ids = {registry.resolve(envelope).conversation_id for _ in range(10)}
        assert len(ids) == 1
        assert store.count_conversations() == 1

    def test_a_registry_miss_recovers_the_conversation_from_the_store(self):
        """AC-2/AC-6: registered, process restarts, next message joins the
        existing conversation."""
        store = ConversationStore(":memory:")
        first_registry, _ = _registry(store)
        before = first_registry.resolve(_envelope(
            "MOQ on the gel?", channel=Channel.WHATSAPP, identity="+919820011122"))

        # The restart: a brand-new registry over the same database.
        second_registry, _ = _registry(store)
        after = second_registry.resolve(_envelope(
            "and delivery?", channel=Channel.WHATSAPP, identity="+919820011122"))

        assert after.conversation_id == before.conversation_id
        assert store.count_conversations() == 1

    def test_a_stale_conversation_yields_a_child_not_a_reopening(self):
        """AC-4: 7 hours idle is beyond the 6-hour window."""
        store = ConversationStore(":memory:")
        registry, _ = _registry(store, idle=6 * 3600)
        orchestrator = Orchestrator(registry)

        first, _ = orchestrator.begin_turn(_envelope(
            "hello", channel=Channel.WHATSAPP, identity="+919820011122"))
        # Age it: the row's last activity moves outside the window and the
        # sweeper closes it, exactly as it would overnight.
        store._connection.execute(
            "UPDATE conversation SET last_activity_at = ? WHERE id = ?",
            ((NOW - timedelta(hours=7)).isoformat(), first.conversation_id),
        )
        assert registry.sweep_idle(now=(NOW).timestamp()) >= 1

        fresh_registry, _ = _registry(store, idle=6 * 3600)
        second = fresh_registry.resolve(_envelope(
            "still there?", channel=Channel.WHATSAPP, identity="+919820011122"))

        assert second.conversation_id != first.conversation_id
        assert second.parent_conversation_id == first.conversation_id
        record = store.get_conversation(second.conversation_id)
        assert record.parent_conversation_id == first.conversation_id

    def test_an_anonymous_session_never_merges(self):
        """AC-3 / CV-02."""
        registry, store = _registry()
        a = registry.resolve(_envelope("hi", session="tab-a"))
        b = registry.resolve(_envelope("hi", session="tab-b"))
        assert a.conversation_id != b.conversation_id
        assert a.lead_id is None and b.lead_id is None
        assert store.count_conversations() == 2

    def test_recover_finds_a_session_conversation_after_a_restart(self):
        store = ConversationStore(":memory:")
        first, _ = _registry(store)
        conversation = first.resolve(_envelope("hello", session="tab-9"))

        second, _ = _registry(store)
        recovered = second.recover("tab-9")
        assert recovered is not None
        assert recovered.conversation_id == conversation.conversation_id

    def test_recover_returns_none_outside_the_window(self):
        store = ConversationStore(":memory:")
        first, _ = _registry(store)
        conversation = first.resolve(_envelope("hello", session="tab-9"))
        store._connection.execute(
            "UPDATE conversation SET last_activity_at = ? WHERE id = ?",
            ((NOW - timedelta(hours=9)).isoformat(), conversation.conversation_id),
        )
        second, _ = _registry(store)
        assert second.recover("tab-9") is None

# ---------------------------------------------------------------------------
# The lead link (AC-1, TAC-4)
# ---------------------------------------------------------------------------
class TestIdentityLink:
    def test_a_link_is_upserted_and_found(self):
        store = ConversationStore(":memory:")
        store.upsert_identity_link(identity_type="phone",
                                   identity_value="+919820011122", lead_id="lead-1")
        assert store.lead_for_identity("phone", "+919820011122") == "lead-1"

    def test_two_channels_linking_one_identity_converge_on_one_lead(self):
        """TAC-4: the constraint decides, not a read-then-write."""
        store = ConversationStore(":memory:")
        winner = store.upsert_identity_link(
            identity_type="phone", identity_value="+919820011122", lead_id="lead-1")
        loser = store.upsert_identity_link(
            identity_type="phone", identity_value="+919820011122", lead_id="lead-2")
        assert winner == loser == "lead-1"

    def test_a_conversation_resolves_to_the_linked_lead(self):
        store = ConversationStore(":memory:")
        store.upsert_identity_link(identity_type="phone",
                                   identity_value="+919820011122", lead_id="lead-7")
        registry, _ = _registry(store)
        conversation = registry.resolve(_envelope(
            "hello again", channel=Channel.VOICE_INBOUND, identity="+919820011122"))
        assert conversation.lead_id == "lead-7"

    def test_capturing_a_lead_links_the_identity_and_the_conversation(self):
        """The chat side of AC-1: the number the buyer supplies is what a later
        call from that number resolves through."""
        from anita.surfaces import ChatSurface

        class CaptureEngine:
            """Stands in for the answer engine's lead capture, mirroring the
            contract `AnswerEngine._capture` writes onto the conversation:
            the lead id and the details the buyer supplied."""

            def __init__(self, surface):
                self.surface = surface

            def answer(self, intent, message, conversation):
                from anita.answer_engine import ChatReply

                conversation.lead_id = "lead-42"
                conversation.lead_phone = "98200 11122"   # as typed by the buyer
                conversation.lead_email = ""
                return ChatReply(text="passed your details", intent="consent",
                                 conversation_id=conversation.conversation_id)

        store = ConversationStore(":memory:")
        registry, _ = _registry(store)
        orchestrator = Orchestrator(registry)
        chat = ChatSurface(orchestrator)
        chat.engine = CaptureEngine(chat)

        chat.post_message(message="call me on 98200 11122", session_id="s1")
        # The orchestrator writes the link when the turn completes.
        assert store.lead_for_identity("phone", "+919820011122") == "lead-42"

        later = registry.resolve(_envelope(
            "you called?", channel=Channel.VOICE_INBOUND, identity="+919820011122"))
        assert later.lead_id == "lead-42"


# ---------------------------------------------------------------------------
# The follow-up resolution the context enables (US-004 AC-1, US-002 AC-2)
# ---------------------------------------------------------------------------
CORPUS_DIR = PLATFORM.parent / "data" / "out"


@pytest.mark.skipif(not CORPUS_DIR.exists(), reason="catalogue corpus not collected")
class TestContextIsUsable:
    """Carrying the turns across is only half of `AC-1`; the answer must use
    them. These drive the real engine over the real corpus."""

    @staticmethod
    def _engine():
        from anita.answer_engine import AnswerEngine
        from anita.answers import AnitaAnswers
        from anita.corpus import Corpus

        answers = AnitaAnswers(Corpus(CORPUS_DIR))
        return AnswerEngine(answers=answers)

    def test_a_follow_up_question_inherits_the_product(self):
        """"and the MOQ?" names no product — the previous turn did."""
        from anita.orchestrator import Orchestrator

        from anita.orchestrator import Intent, Orchestrator

        engine = self._engine()
        orchestrator = Orchestrator()
        conversation, _ = orchestrator.begin_turn(_envelope(
            "what is the price of the ultrasound gel?", session="s1"))
        reply = engine.answer(Intent.COMMERCIAL, "what is the minimum order quantity?",
                              conversation)
        assert "10 Pieces" in reply.text
        assert "Ultrasound Gel" in reply.text

    def test_a_follow_up_across_channels_uses_the_carried_context(self):
        """The `AC-1` scenario end to end: asked on web chat, asked again by
        phone, answered without the caller naming the product."""
        from anita.answer_engine import AnswerEngine
        from anita.answers import AnitaAnswers
        from anita.crm import MockCrmStore
        from anita.corpus import Corpus
        from anita.leads import LeadService
        from anita.surfaces import ChatSurface
        from anita.voice_pipeline import install, run_rag_query_sync

        store = ConversationStore(":memory:")
        registry, _ = _registry(store)
        orchestrator = Orchestrator(registry)
        leads = LeadService(MockCrmStore(":memory:"))
        answers = AnitaAnswers(Corpus(CORPUS_DIR))
        chat = ChatSurface(orchestrator, leads=leads)
        chat.engine.answers = answers
        install(orchestrator, AnswerEngine(answers=answers, leads=leads))

        chat.post_message(message="what is the price of the ultrasound gel?",
                          session_id="web-1")
        chat.post_message(message="call me on 98200 44444", session_id="web-1")

        answer = run_rag_query_sync(
            "", "voice", "what is the minimum order quantity?",
            "+919820044444", caller="+919820044444",
        )
        assert "10 Pieces" in answer
        # ...and the voice conversation is the same lead.
        conversation = registry.get("+919820044444")
        assert conversation.lead_id == store.lead_for_identity("phone", "+919820044444")

    def test_an_unknown_product_still_declines(self):
        """The guard: a follow-up rule that answered about whatever was
        discussed earlier would quote a real price for the wrong product."""
        from anita.orchestrator import Orchestrator

        from anita.orchestrator import Intent, Orchestrator

        engine = self._engine()
        orchestrator = Orchestrator()
        conversation, _ = orchestrator.begin_turn(_envelope(
            "what is the price of the ultrasound gel?", session="s1"))
        reply = engine.answer(
            Intent.COMMERCIAL, "what is the price of the MRI machine you sell?", conversation)
        assert reply.declined
        assert "Ultrasound Gel" not in reply.text
class TestNoCrmVocabulary:
    """The relocated registry must carry zero CRM vocabulary (`REC-02`, TAC-7).

    A static check over the source, because the failure mode this guards is a
    *rename* drifting back: the registry came out of a CRM package and the
    names it must never carry again are the ones that made deleting the package
    look safe and impossible at the same time.
    """

    FORBIDDEN = ("crm_user_id", "salesforce", "sfdc", "opportunity", "account_id",
                 "sobject", "contact_id")

    def _source(self, name: str) -> str:
        return (PLATFORM / "app" / "anita" / name).read_text(encoding="utf-8").lower()

    @pytest.mark.parametrize("module", ["orchestrator.py", "conversation_store.py"])
    def test_no_forbidden_symbols(self, module):
        source = self._source(module)
        found = [word for word in self.FORBIDDEN if word in source]
        assert not found, f"{module} carries CRM vocabulary: {found}"

    @pytest.mark.parametrize("module", ["orchestrator.py", "conversation_store.py"])
    def test_no_network_imports(self, module):
        tree = ast.parse((PLATFORM / "app" / "anita" / module).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not (imported & {"requests", "httpx", "socket", "urllib", "aiohttp"})
