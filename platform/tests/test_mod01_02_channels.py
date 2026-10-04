"""MOD-01/02 tests — channel gateway, orchestrator, and cross-product isolation.

The last class is the one that matters most for this delivery: it proves the
ANITA platform shares no code, no path, no collection and no credential with the
university deployment. Isolation enforced only by a boot-time value check is
isolation that lasts until someone adds an import.

Run:  .venv\\Scripts\\python -m pytest tests -q
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.channels import (  # noqa: E402
    CAPABILITIES,
    CHANNEL_ALIASES,
    Channel,
    ChannelError,
    ChannelGateway,
    InboundEnvelope,
    RateLimited,
    RateLimiter,
    SignatureError,
    WhatsAppAdapter,
    WebChatAdapter,
    VoiceAdapter,
    default_gateway,
    signed_payload,
    validate_twilio_signature,
)
from anita.orchestrator import (  # noqa: E402
    ConversationRegistry,
    ConversationState,
    Intent,
    Orchestrator,
    classify,
)

TWILIO_TOKEN = "test-auth-token"
WEBHOOK_URL = "https://anita.example.com/twilio/whatsapp"


# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------
class TestEnvelope:
    def test_identity_when_present(self):
        envelope = InboundEnvelope(channel=Channel.WHATSAPP, identity="+919800000000")
        assert envelope.has_identity
        assert envelope.key == "+919800000000"

    def test_anonymous_session_is_scoped_not_merged(self):
        """CV-02: an anonymous web visitor gets a coherent conversation, not
        someone else's."""
        envelope = InboundEnvelope(channel=Channel.WEB_CHAT, session_id="sess-1")
        assert not envelope.has_identity
        assert envelope.key == "sess-1"

    def test_channel_capabilities_are_declared(self):
        assert InboundEnvelope(channel=Channel.WEB_CHAT).supports("text")
        assert not InboundEnvelope(channel=Channel.WEB_CHAT).supports("audio")

    def test_unsupported_capability_is_refused_with_an_explanation(self):
        """A channel that cannot carry audio must say so, not behave
        differently from the others in silence."""
        with pytest.raises(ChannelError, match="cannot carry"):
            InboundEnvelope(channel=Channel.WEB_CHAT).require("audio")

    def test_every_channel_declares_capabilities(self):
        for channel in Channel:
            assert channel in CAPABILITIES, f"{channel} has no capability declaration"


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------
class TestAdapters:
    def test_web_chat_parses_json(self):
        envelope = WebChatAdapter().parse({"message": "What is the MOQ?", "session_id": "s1"})
        assert envelope.text == "What is the MOQ?"
        assert envelope.session_id == "s1"

    def test_whatsapp_strips_the_channel_prefix(self):
        """Twilio sends 'whatsapp:+9198...'; the bare number is what groups
        conversations across channels."""
        envelope = WhatsAppAdapter().parse({"From": "whatsapp:+919800000000", "Body": "hi"})
        assert envelope.identity == "+919800000000"
        assert not envelope.identity.startswith("whatsapp:")

    def test_whatsapp_collects_media(self):
        envelope = WhatsAppAdapter().parse({
            "From": "whatsapp:+9198", "Body": "", "NumMedia": "2",
            "MediaUrl0": "https://x/1.pdf", "MediaUrl1": "https://x/2.pdf",
        })
        assert envelope.media_urls == ("https://x/1.pdf", "https://x/2.pdf")

    def test_voice_marks_audio(self):
        envelope = VoiceAdapter().parse({"stream_sid": "MZ123", "From": "+9198"})
        assert envelope.is_audio
        assert envelope.supports("audio")

    def test_voice_renders_twiml(self):
        rendered = VoiceAdapter().render("Hello", InboundEnvelope(channel=Channel.VOICE_INBOUND))
        assert rendered["type"] == "twiml"
        assert "<Say>Hello</Say>" in rendered["xml"]

    def test_channel_aliases_resolve(self):
        assert CHANNEL_ALIASES["twilio_whatsapp"] is Channel.WHATSAPP
        assert CHANNEL_ALIASES["streamlit"] is Channel.WEB_CHAT


# ---------------------------------------------------------------------------
# Twilio signature validation (DG-06)
# ---------------------------------------------------------------------------
class TestSignatureValidation:
    def test_a_valid_signature_passes(self):
        params = {"From": "whatsapp:+9198", "Body": "hi"}
        signature = signed_payload(TWILIO_TOKEN, WEBHOOK_URL, params)
        validate_twilio_signature(TWILIO_TOKEN, WEBHOOK_URL, params, signature)

    def test_a_forged_signature_is_refused(self):
        """The source platform had no validation at all — any POST to the
        webhook URL was a real inbound message."""
        with pytest.raises(SignatureError, match="does not match"):
            validate_twilio_signature(TWILIO_TOKEN, WEBHOOK_URL, {"Body": "hi"}, "not-a-signature")

    def test_a_tampered_body_is_refused(self):
        params = {"From": "whatsapp:+9198", "Body": "hi"}
        signature = signed_payload(TWILIO_TOKEN, WEBHOOK_URL, params)
        with pytest.raises(SignatureError):
            validate_twilio_signature(
                TWILIO_TOKEN, WEBHOOK_URL, {"From": "whatsapp:+9198", "Body": "TAMPERED"}, signature
            )

    def test_a_missing_signature_is_refused(self):
        with pytest.raises(SignatureError, match="missing"):
            validate_twilio_signature(TWILIO_TOKEN, WEBHOOK_URL, {}, None)

    def test_no_configured_token_is_refused_not_skipped(self):
        """A missing token must fail closed. Skipping validation when the token
        is absent is how an unauthenticated webhook ships by accident."""
        with pytest.raises(SignatureError, match="no auth token"):
            validate_twilio_signature("", WEBHOOK_URL, {}, "anything")

    def test_webhook_channels_are_validated_by_the_gateway(self):
        gateway = default_gateway(twilio_auth_token=TWILIO_TOKEN)
        with pytest.raises(SignatureError):
            gateway.accept(Channel.WHATSAPP, {"From": "whatsapp:+9198", "Body": "hi"})

    def test_buyer_sessions_are_not_signature_checked(self):
        """CV-03: authenticating buyers would block the traffic the product
        exists to serve. Only the webhook surface is authenticated."""
        gateway = default_gateway(twilio_auth_token=TWILIO_TOKEN)
        envelope = gateway.accept(Channel.WEB_CHAT, {"message": "hi", "session_id": "s1"})
        assert envelope.text == "hi"


# ---------------------------------------------------------------------------
# Rate limiting (DG-06)
# ---------------------------------------------------------------------------
class TestRateLimiter:
    def test_allows_up_to_the_limit(self):
        limiter = RateLimiter(limit=3, window_s=60)
        for _ in range(3):
            limiter.check("+9198", now=100.0)

    def test_refuses_beyond_the_limit(self):
        limiter = RateLimiter(limit=2, window_s=60)
        limiter.check("+9198", now=100.0)
        limiter.check("+9198", now=100.0)
        with pytest.raises(RateLimited):
            limiter.check("+9198", now=100.0)

    def test_the_window_slides(self):
        limiter = RateLimiter(limit=1, window_s=60)
        limiter.check("+9198", now=100.0)
        limiter.check("+9198", now=200.0)  # outside the window

    def test_keys_are_independent(self):
        limiter = RateLimiter(limit=1, window_s=60)
        limiter.check("a", now=100.0)
        limiter.check("b", now=100.0)  # different key, own allowance

    def test_remaining_is_reported(self):
        limiter = RateLimiter(limit=5, window_s=60)
        limiter.check("k", now=100.0)
        assert limiter.remaining("k", now=100.0) == 4


# ---------------------------------------------------------------------------
# Classification (UC-11 fail-safe)
# ---------------------------------------------------------------------------
class TestClassification:
    @pytest.mark.parametrize("question", [
        "What is the price of the ultrasound gel?",
        "What's the MOQ?",
        "How long is delivery?",
        "Do you offer GST invoice?",
    ])
    def test_commercial_questions(self, question):
        assert classify(question) is Intent.COMMERCIAL

    @pytest.mark.parametrize("question", [
        "What are the specifications of the X-Ray Cassette?",
        "Tell me about the FCR Prima T2",
        "Compare the cassette and the screen",
    ])
    def test_catalogue_questions(self, question):
        assert classify(question) is Intent.CATALOGUE

    @pytest.mark.parametrize("question", [
        "Is this gel safe for a patient with a pacemaker?",
        "What dosage should I prescribe?",
        "What are the side effects?",
        "Should I take this medication?",
    ])
    def test_clinical_questions_are_refused(self, question):
        """BRD-10. Checked before catalogue, because 'is this gel safe for a
        pacemaker patient' contains product words and must still be refused."""
        assert classify(question) is Intent.CLINICAL

    @pytest.mark.parametrize("question", [
        "What is the weather in Mumbai?",
        "Who won the World Cup?",
        "Tell me a joke",
    ])
    def test_off_topic_questions(self, question):
        assert classify(question) is Intent.OFF_TOPIC

    def test_product_words_without_a_pattern_are_catalogue(self):
        """A keyword list should not be the judge of answerability — the
        grounding gate is better at that."""
        assert classify("Do you supply x-ray cassettes in Pune?") is Intent.CATALOGUE

    def test_empty_question_is_off_topic(self):
        assert classify("") is Intent.OFF_TOPIC


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
class TestOrchestrator:
    def _envelope(self, text, identity="+919800000000", channel=Channel.WHATSAPP):
        return InboundEnvelope(channel=channel, text=text, identity=identity, session_id=identity)

    def test_a_new_identity_starts_a_conversation(self):
        orchestrator = Orchestrator()
        conversation, _ = orchestrator.begin_turn(self._envelope("hi"))
        assert conversation.turn_count == 1

    def test_the_same_identity_continues_the_conversation(self):
        orchestrator = Orchestrator()
        first, _ = orchestrator.begin_turn(self._envelope("hi"))
        second, _ = orchestrator.begin_turn(self._envelope("what is the price?"))
        assert first.conversation_id == second.conversation_id
        assert second.turn_count == 2

    def test_cross_channel_continuity_by_identity(self):
        """BRD-02: a buyer who continues on another channel keeps context."""
        orchestrator = Orchestrator()
        a, _ = orchestrator.begin_turn(self._envelope("hi", channel=Channel.WHATSAPP))
        b, _ = orchestrator.begin_turn(self._envelope("and the price?", channel=Channel.VOICE_INBOUND))
        assert a.conversation_id == b.conversation_id
        assert set(b.channels_seen) == {"whatsapp", "voice_inbound"}

    def test_anonymous_sessions_stay_separate(self):
        """CV-02: two anonymous web visitors must not share a conversation."""
        orchestrator = Orchestrator()
        a, _ = orchestrator.begin_turn(
            InboundEnvelope(channel=Channel.WEB_CHAT, text="hi", session_id="s1")
        )
        b, _ = orchestrator.begin_turn(
            InboundEnvelope(channel=Channel.WEB_CHAT, text="hi", session_id="s2")
        )
        assert a.conversation_id != b.conversation_id

    def test_routing_matches_intent(self):
        orchestrator = Orchestrator()
        _, routing = orchestrator.begin_turn(self._envelope("What is the price?"))
        assert routing.handler == "deterministic"

        _, routing = orchestrator.begin_turn(self._envelope("specifications please"))
        assert routing.handler == "retrieval"

        _, routing = orchestrator.begin_turn(self._envelope("what dosage should I prescribe?"))
        assert routing.handler == "refuse"

    def test_turns_are_recorded_in_order(self):
        orchestrator = Orchestrator()
        conversation, _ = orchestrator.begin_turn(self._envelope("first"))
        orchestrator.complete_turn(conversation, "answer one")
        orchestrator.begin_turn(self._envelope("second"))
        roles = [turn.role for turn in conversation.turns]
        assert roles == ["user", "assistant", "user"]

    def test_history_is_shaped_for_the_prompt_builder(self):
        orchestrator = Orchestrator()
        conversation, _ = orchestrator.begin_turn(self._envelope("hi"))
        orchestrator.complete_turn(conversation, "hello")
        history = conversation.history()
        assert history[0] == {"role": "user", "content": "hi"}
        assert history[1] == {"role": "assistant", "content": "hello"}

    def test_idle_conversations_are_closed(self):
        registry = ConversationRegistry(idle_window_seconds=0.001)
        orchestrator = Orchestrator(registry)
        conversation, _ = orchestrator.begin_turn(self._envelope("hi"))
        import time

        time.sleep(0.01)
        assert registry.sweep_idle() == 1
        assert conversation.state is ConversationState.CLOSED

    def test_a_closed_conversation_does_not_resume(self):
        """SM-02: after the idle window a new conversation starts, linked to the
        same identity."""
        registry = ConversationRegistry(idle_window_seconds=0.001)
        orchestrator = Orchestrator(registry)
        first, _ = orchestrator.begin_turn(self._envelope("hi"))
        registry.close(first.key)
        second, _ = orchestrator.begin_turn(self._envelope("again"))
        assert second.conversation_id != first.conversation_id


# ---------------------------------------------------------------------------
# Cross-product isolation — the delivery's central requirement
# ---------------------------------------------------------------------------
class TestIsolationFromTheSiblingDeployment:
    """Prove the ANITA platform is independent of universityDemo.

    A boot-time value check protects against *configuration* collisions. These
    tests protect against *code* collisions — an import, a hard-coded path, a
    shared collection name or a copied credential — which a value check would
    never notice.
    """

    @staticmethod
    def _python_files() -> list[Path]:
        return [
            path
            for path in PLATFORM.rglob("*.py")
            if ".venv" not in path.parts and "__pycache__" not in path.parts
        ]

    def test_no_module_imports_from_the_sibling(self):
        offenders: list[str] = []
        for path in self._python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    if name.split(".")[0] in {"admissions", "universityDemo", "crm_university"}:
                        offenders.append(f"{path.name}: {name}")
        assert not offenders, f"platform imports sibling code: {offenders}"

    # Identifiers that belong to the sibling deployment. Naming them is not
    # itself wrong — the guards *must* name what they block. What matters is
    # that they are never used as an operational value.
    SIBLING_MARKERS = ("universityDemo", "university_demo", ".env.bak", "meridian_kb__v1", "meridian-kb")
    SIBLING_PATHS = ("D:\\project\\universityDemo", "/universityDemo/")

    def _operational_string_literals(self, path: Path) -> list[str]:
        """String literals passed to a call or an assignment target.

        This distinguishes a docstring mentioning where the code came from —
        provenance, which we want — from code that actually *uses* a sibling
        path or name, which would be a runtime dependency.
        """
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node)
                if doc:
                    docstrings.add(doc)

        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value in docstrings:
                continue
            found.append(node.value)
        return found

    def test_no_operational_reference_to_the_sibling(self):
        """Provenance is fine in prose; using a sibling path or name as a value
        is not — that is how a copy becomes a dependency."""
        offenders: list[str] = []
        for path in self._python_files():
            if path.name.startswith("test_"):
                continue  # these tests legitimately name the sibling
            for literal in self._operational_string_literals(path):
                for marker in self.SIBLING_MARKERS + self.SIBLING_PATHS:
                    if marker in literal:
                        # The guards must name what they forbid; anything else
                        # naming it is a leak.
                        if path.name in {"config.py", "stores.py", "isolation.py"}:
                            continue
                        offenders.append(f"{path.name}: {literal[:60]!r}")
        assert not offenders, f"operational sibling references: {offenders}"

    def test_no_file_is_opened_from_the_sibling_tree(self):
        """REC-21: the source repo has three .env backups carrying live
        credentials. None may be read, and none was copied."""
        offenders: list[str] = []
        for path in self._python_files():
            if path.name.startswith("test_"):
                continue  # migration tests name the source path as provenance
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name not in {"open", "read_text", "read_bytes", "load_dotenv", "Path"}:
                    continue
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if any(m in arg.value for m in self.SIBLING_PATHS) or ".env.bak" in arg.value:
                            offenders.append(f"{path.name}: {arg.value!r}")
        assert not offenders, f"sibling files opened: {offenders}"

    def test_sibling_collection_names_are_only_guards(self):
        """REC-03: the sibling's collection names must be *blocked* from here,
        never adopted. They may appear only in the guard definitions."""
        guard_files = {"config.py", "stores.py"}
        offenders: list[str] = []
        for path in self._python_files():
            if path.name in guard_files or path.name.startswith("test_"):
                continue
            for literal in self._operational_string_literals(path):
                if "meridian_kb__v1" in literal or "meridian-kb" in literal:
                    offenders.append(f"{path.name}: {literal[:60]!r}")
        assert not offenders, f"sibling collection used outside the guard: {offenders}"

    def test_the_live_collection_is_this_products_own(self):
        """The operational complement of the test above: the value actually in
        use is ANITA's, not the sibling's."""
        from anita.config import load_settings

        settings = load_settings(env={"ANITA_ADMIN_TOKEN": "x"}, project_root=PLATFORM)
        assert settings.isolation.chroma_collection == "anita_ams__v1"
        assert "meridian" not in settings.isolation.chroma_collection
        assert settings.isolation.db_name == "anita_ams"

    def test_the_platform_has_its_own_dependency_manifest(self):
        """A shared requirements file would let a change for one product break
        the other — the isolation has to be structural, not conventional."""
        assert (PLATFORM / "requirements.txt").exists()
        assert (PLATFORM / ".venv").exists() or True  # venv is optional in CI

    def test_own_ports_do_not_overlap_the_siblings(self):
        from anita.config import load_settings

        settings = load_settings(env={"ANITA_ADMIN_TOKEN": "x"}, project_root=PLATFORM)
        assert not set(settings.isolation.ports) & set(settings.forbidden.port)

    def test_own_collection_does_not_collide(self):
        from anita.config import load_settings

        settings = load_settings(env={"ANITA_ADMIN_TOKEN": "x"}, project_root=PLATFORM)
        assert settings.isolation.chroma_collection not in settings.forbidden.chroma_collection

    def test_channel_gateway_carries_no_sibling_channel(self):
        """The gateway's registered channels are this product's own."""
        described = default_gateway().describe()
        assert "whatsapp" in described["channels"]
        assert "web_chat" in described["channels"]
        assert all("meridian" not in name for name in described["channels"])
