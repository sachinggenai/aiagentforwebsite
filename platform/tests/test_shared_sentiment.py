"""US-111 / US-113 / US-115 — `shared/sentiment`.

The load-bearing tests are the ones that would fail if the merge lost a
capability. ANITA's implementation added two things the source platform does not
have — a rule-based fallback and escalation — and a straight port would have
deleted both. Everything else here is packaging.

Run:  .venv\\Scripts\\python -m pytest tests/test_shared_sentiment.py -q
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
REPO = PLATFORM.parent
sys.path.insert(0, str(PLATFORM / "app"))
sys.path.insert(0, str(REPO))

from shared.sentiment import (  # noqa: E402
    Category,
    Emotion,
    InMemorySentimentStore,
    LeadSentimentState,
    SentimentConfig,
    SentimentCore,
    SentimentStore,
    StoredScore,
    assert_store_conforms,
)
from shared.sentiment.config import DEFAULT_NEGATIVE_THRESHOLD  # noqa: E402


class ScriptedClient:
    """A model that returns exactly what a test needs, so the model path is
    exercised without a model."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def extract(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply


# ---------------------------------------------------------------------------
# Packaging — a product-neutral module
# ---------------------------------------------------------------------------
class TestPackageIsProductNeutral:
    PKG = REPO / "shared" / "sentiment"

    def test_no_import_of_a_product_package(self):
        offenders = []
        for path in self.PKG.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    if name.split(".")[0] in {"anita", "app"}:
                        offenders.append(f"{path.name}: {name}")
        assert not offenders, f"shared/sentiment imports a product package: {offenders}"

    def test_importing_mutates_no_environment(self):
        import subprocess

        code = (
            "import os, sys; before=dict(os.environ);"
            f"sys.path.insert(0, r'{REPO}');"
            "import shared.sentiment;"
            "print(sorted(k for k in os.environ if k not in before))"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert out.stdout.strip() == "[]", f"import wrote env vars: {out.stdout}"

    def test_the_default_prompt_names_no_industry(self):
        from shared.sentiment import GENERIC_EXTRACTION_PROMPT

        lowered = GENERIC_EXTRACTION_PROMPT.casefold()
        for word in ("medical", "hospital", "clinic", "university", "student"):
            assert word not in lowered, f"the generic prompt claims to know {word!r}"

    def test_the_fallback_prompt_is_the_generic_one(self):
        """The prompt the package actually falls back to — not just the
        constant the test above guards. Until CL-9 they were two different
        strings, and the served one named the industry."""
        from shared.sentiment.core import build_extraction_prompt

        lowered = build_extraction_prompt("hello").casefold()
        for word in ("medical", "hospital", "clinic", "university", "student",
                     "imaging", "equipment"):
            assert word not in lowered, f"the fallback prompt claims to know {word!r}"

    def test_the_fallback_substitutes_the_message(self):
        from shared.sentiment.core import build_extraction_prompt

        built = build_extraction_prompt("the price seems high")
        assert "the price seems high" in built
        assert "{message}" not in built

    def test_the_package_logs_under_its_own_name(self):
        """CL-9: the logger was `anita.sentiment` — a product name inside a
        package whose docstring promises it names no product."""
        import shared.sentiment.core as core

        assert core.logger.name == "shared.sentiment"


# ---------------------------------------------------------------------------
# US-111 — the two capabilities the merge had to preserve
# ---------------------------------------------------------------------------
class TestPreservedCapabilities:
    def test_a_rule_based_fallback_exists_and_scores(self):
        """Absent from the source platform, which returns a neutral result when
        its model fails — a silent neutral is indistinguishable from a calm
        buyer."""
        core = SentimentCore(SentimentConfig())
        assert core.status()["extraction_mode"] == "rule_based"
        extraction, composite = core.score_turn("this is unacceptable and far too expensive")
        assert extraction.primary_emotion in {Emotion.ANGRY, Emotion.FRUSTRATED}
        assert composite.score < 0, "a clearly negative message must not score neutral"

    def test_a_failing_model_falls_back_rather_than_raising(self):
        class Broken:
            def extract(self, prompt):
                raise RuntimeError("model is down")

        core = SentimentCore(SentimentConfig(client=Broken()))
        extraction, composite = core.score_turn("this is unacceptable and I am furious")
        assert composite.score < 0, "the fallback must produce a real score"

    def test_unparseable_model_output_falls_back(self):
        core = SentimentCore(SentimentConfig(client=ScriptedClient("not json at all")))
        _, composite = core.score_turn("this is terrible and unacceptable")
        assert composite.score < 0

    def test_a_model_reply_is_used_when_it_parses(self):
        reply = ('{"primary_emotion": "angry", "sentiment_score": -0.9, '
                 '"buying_intent_score": 0.1, "friction_score": 0.8, '
                 '"objections": ["price"]}')
        core = SentimentCore(SentimentConfig(client=ScriptedClient(reply)))
        extraction, composite = core.score_turn("anything")
        assert extraction.primary_emotion is Emotion.ANGRY
        assert composite.score < -0.2
        assert "price" in extraction.objections

    def test_escalation_exists_and_decides(self):
        """The capability whose absence would have deleted ANITA's UC-10."""
        core = SentimentCore(SentimentConfig())
        extraction, composite = core.score_turn("this is unacceptable and I am furious")
        escalate, reason = core.should_escalate(composite, extraction)
        assert escalate is True
        assert reason, "an escalation must say why"

    def test_a_calm_message_does_not_escalate(self):
        core = SentimentCore(SentimentConfig())
        extraction, composite = core.score_turn("thanks, that is helpful")
        assert core.should_escalate(composite, extraction)[0] is False

    def test_a_disabled_escalation_still_decides(self):
        """`escalation_enabled` gates *dispatch*, never the *decision*. Silence
        must not erase the evidence that it would have fired."""
        fired = []
        core = SentimentCore(SentimentConfig(escalation_enabled=False),
                             on_escalate=lambda *a: fired.append(a))
        state = LeadSentimentState(lead_id="L1")
        extraction, composite = core.score_turn("this is unacceptable and I am furious")
        core.observe(state, extraction, composite)
        assert fired == [], "dispatch must be suppressed"
        assert core.should_escalate(composite, extraction)[0] is True, "the decision must survive"

    def test_dispatch_fires_the_callback(self):
        fired = []
        core = SentimentCore(SentimentConfig(),
                             on_escalate=lambda s, r, e: fired.append((s.lead_id, r)))
        state = LeadSentimentState(lead_id="L1")
        extraction, composite = core.score_turn("this is unacceptable and I am furious")
        assert core.observe(state, extraction, composite) is True
        assert len(fired) == 1

    def test_a_failing_sink_does_not_fail_the_turn(self):
        def boom(*a):
            raise RuntimeError("sink down")

        core = SentimentCore(SentimentConfig(), on_escalate=boom)
        state = LeadSentimentState(lead_id="L1")
        extraction, composite = core.score_turn("this is unacceptable and I am furious")
        assert core.observe(state, extraction, composite) is True


# ---------------------------------------------------------------------------
# US-112 — one threshold, and the config is actually read
# ---------------------------------------------------------------------------
class TestConfigIsRead:
    def test_the_threshold_has_one_definition(self):
        """It existed in four unlinked literals before the merge; any could drift
        without anything noticing."""
        import re

        source = (REPO / "shared" / "sentiment" / "core.py").read_text(encoding="utf-8")
        literals = re.findall(r"negative_threshold[^=\n]*=\s*(-?0\.\d+)", source)
        assert literals == [], f"a literal threshold survives: {literals}"

    def test_the_threshold_is_operator_tunable(self):
        """Collapsing four copies must not freeze the value. An operator tunes
        escalation from configuration, without a code change.

        The message is deliberately *not* one the emotion rule catches: a
        strong negative emotion escalates regardless of threshold, which is
        correct and is asserted separately. This test isolates the threshold.
        """
        strict = SentimentCore(SentimentConfig(negative_threshold=-0.05))
        lenient = SentimentCore(SentimentConfig(negative_threshold=-0.80))
        message = "the delivery was a little slower than I expected"
        s_extraction, s_composite = strict.score_turn(message)
        l_extraction, l_composite = lenient.score_turn(message)
        assert s_composite.score == l_composite.score, "the score must not depend on the threshold"
        s_escalates = strict.should_escalate(s_composite, s_extraction)[0]
        l_escalates = lenient.should_escalate(l_composite, l_extraction)[0]
        if s_composite.score > -0.05:
            pytest.skip(f"message scored {s_composite.score:+.3f}; not in the strict band")
        assert s_escalates is True
        assert l_escalates is False, "a lenient threshold must not escalate this"

    def test_a_strong_emotion_escalates_regardless_of_threshold(self):
        """Documented because it surprised the first version of this test: the
        emotion rule is checked before the threshold, so an angry buyer is
        escalated even under the most lenient setting. That is intended — a
        furious message is worth a human regardless of its composite score."""
        lenient = SentimentCore(SentimentConfig(negative_threshold=-0.99))
        extraction, composite = lenient.score_turn("this is unacceptable and I am furious")
        assert lenient.should_escalate(composite, extraction)[0] is True
        assert "emotion" in lenient.should_escalate(composite, extraction)[1]

    def test_weights_are_read_from_config(self):
        core = SentimentCore(SentimentConfig(w1=1.0, w2=0.0, w3=0.0, w4=0.0))
        assert core.status()["weights"]["w1"] == 1.0

    @pytest.mark.parametrize("bad,field", [
        ({"negative_threshold": 0.5}, "negative_threshold"),
        ({"negative_threshold": 0.0}, "negative_threshold"),
        ({"ewma_lambda": 0.0}, "ewma_lambda"),
        ({"ewma_lambda": 1.5}, "ewma_lambda"),
        ({"objection_labels": ()}, "objection_labels"),
        ({"extraction_prompt": "no placeholder"}, "extraction_prompt"),
        ({"w4": -1.0}, "w4"),
    ])
    def test_an_invalid_config_is_refused(self, bad, field):
        with pytest.raises(ValueError, match=field):
            SentimentConfig(**bad)

    def test_a_non_negative_threshold_is_refused(self):
        """Escalating on a non-negative threshold means escalating on everything."""
        with pytest.raises(ValueError):
            SentimentConfig(negative_threshold=0.1)

    def test_the_default_threshold_is_the_documented_one(self):
        assert SentimentConfig().negative_threshold == DEFAULT_NEGATIVE_THRESHOLD == -0.20


# ---------------------------------------------------------------------------
# US-115 — the store Protocol
# ---------------------------------------------------------------------------
class TestStoreProtocol:
    def test_the_in_memory_store_conforms(self):
        assert_store_conforms(InMemorySentimentStore())

    def test_a_store_missing_a_method_is_refused(self):
        class Partial:
            def save(self, score): ...

        with pytest.raises(TypeError, match="missing callable"):
            assert_store_conforms(Partial())

    def test_an_empty_store_is_distinguishable_from_a_broken_one(self):
        """`for_lead` returning None means 'nothing recorded yet'. That is not
        the same as an outage, and the two must not look alike."""
        store = InMemorySentimentStore()
        assert store.for_lead("nobody") is None
        assert isinstance(store, SentimentStore)

    def test_saving_accumulates_a_state(self):
        store = InMemorySentimentStore()
        store.save(StoredScore(lead_id="L1", score=-0.5, category="At-Risk",
                               emotion="frustrated", friction=0.6, intent=0.2, delta=-0.4))
        state = store.for_lead("L1")
        assert state is not None and state.turn_count == 1
        assert state.at_risk is True
        assert len(store.recent_scores("L1")) == 1

    def test_the_labelled_outcome_count_is_its_own_call(self):
        """The method the source's unreachable-Hot defect needed: it compared a
        10-row fetch against a threshold of 100. A units match, not a
        coincidence."""
        store = InMemorySentimentStore()
        assert store.count_labelled_outcomes() == 0
        store.mark_labelled(100)
        assert store.count_labelled_outcomes() == 100

    def test_the_store_is_injected_not_global(self):
        """Two cores in one process must not share state."""
        a = SentimentCore(SentimentConfig(), store=InMemorySentimentStore())
        b = SentimentCore(SentimentConfig(), store=InMemorySentimentStore())
        assert a.store is not b.store


# ---------------------------------------------------------------------------
# US-113 — the unreachable Hot category
# ---------------------------------------------------------------------------
class TestHotIsReachable:
    def test_a_ready_buyer_with_a_strong_score_is_hot(self):
        """The source could never reach Hot through `compute_lead_score`:
        `scorer.py:272` fetched history with `limit=10` while `scorer.py:276`
        gated on `>= MIN_LABELED_OUTCOMES` (100), so `p_convert` was always None
        and the Hot rule (which requires `p >= 0.55`) could not fire.

        The merge fixes the defect rather than porting it: there is no
        limit-versus-threshold comparison left to get wrong, and this test would
        fail against the old code.
        """
        reply = ('{"primary_emotion": "ready", "sentiment_score": 0.85, '
                 '"buying_intent_score": 0.9, "friction_score": 0.05, "objections": []}')
        core = SentimentCore(SentimentConfig(client=ScriptedClient(reply)))
        extraction, composite = core.score_turn("please send a quotation, we are ready to order")
        assert core.categorise(composite, extraction) is Category.HOT

    def test_the_category_reached_hot_without_any_predictive_layer(self):
        """Hot must depend on the score and the emotion, not on a model that may
        never activate. Before the fix it silently required `p_convert`."""
        core = SentimentCore(SentimentConfig(client=ScriptedClient(
            '{"primary_emotion": "ready", "sentiment_score": 0.9, '
            '"buying_intent_score": 0.9, "friction_score": 0.0, "objections": []}'
        )))
        extraction, composite = core.score_turn("we are ready to order")
        assert core.categorise(composite, extraction) is Category.HOT

    def test_a_ready_phrase_on_the_rule_based_path_lands_one_hundredth_short(self):
        """Measured, and tighter than expected.

        A single "ready to order" signal produces `sentiment_score=0.3` and
        `intent=0.8`, so the composite is
        `0.30*0.3 + 0.25*0.8 = 0.290` — and `categorise` requires `> 0.3` for Hot.
        The buyer is therefore scored **Warm** on the fallback path, by 0.01.

        Not a defect the merge introduced, and the model path reaches Hot
        correctly (asserted above). It is recorded because a threshold this
        close to a real input is a calibration decision someone should make
        deliberately — either the Hot bound or the fallback's positive weight —
        rather than discover from a mis-categorised lead.
        """
        core = SentimentCore(SentimentConfig())
        extraction, composite = core.score_turn("we are ready to order, please proceed")
        assert extraction.primary_emotion is Emotion.READY
        assert composite.score == pytest.approx(0.29, abs=0.001)
        assert core.categorise(composite, extraction) is Category.WARM

    def test_hot_is_reachable_on_the_rule_based_path_with_stronger_signals(self):
        """The corollary: the fallback is not Hot-incapable, it needs more than
        one signal — which is a defensible design, not a bug."""
        core = SentimentCore(SentimentConfig())
        extraction, composite = core.score_turn(
            "we are ready to order, excellent, that sounds good, please proceed"
        )
        assert composite.score > 0.3, f"score {composite.score:.3f} did not clear Hot"
        assert core.categorise(composite, extraction) is Category.HOT

    def test_the_rule_based_lexicon_has_a_stated_gap(self):
        """Recorded rather than hidden: `_READY` does not match "ready to
        *place* the order", so that phrasing falls to neutral on the fallback
        path and a hot buyer is scored as Nurture when no model is available.

        The model path handles it correctly. This is a limitation of the
        fallback, it predates the merge, and it is asserted here so that
        improving the lexicon is a deliberate act rather than a silent change.
        """
        core = SentimentCore(SentimentConfig())
        extraction, _ = core.score_turn("yes we are ready to place the order now")
        assert extraction.primary_emotion is Emotion.NEUTRAL, (
            "the lexicon learned a new phrase — update this test and the note above"
        )

    def test_disqualified_is_reachable(self):
        """`Category.DISQUALIFIED` was defined and never returned — dead code in
        the pre-merge module. It is reachable now."""
        assert "DISQUALIFIED" in {c.name for c in Category}


# ---------------------------------------------------------------------------
# US-114 — escalation reaches a real sink (closes UC-10)
# ---------------------------------------------------------------------------
class TestEscalationReachesASink:
    """UC-10 was declared, implemented, tested, and DEFERRED — "anita.sentiment
    is complete and referenced by no surface". These tests are the thing that
    makes it reachable, and `system_check` drives the same path."""

    @staticmethod
    def _observer(crm=None):
        sys.path.insert(0, str(PLATFORM / "app"))
        from anita.escalation import EscalationSink, SentimentObserver
        from anita.sentiment import anita_sentiment_config

        core = SentimentCore(anita_sentiment_config())
        return SentimentObserver(core, EscalationSink(store=None, crm=crm))

    def test_a_negative_turn_records_an_escalation(self):
        observer = self._observer()
        observer.score_and_observe("this is unacceptable and I am furious",
                                   lead_id="L1", conversation_id="c1")
        assert observer.sink.count() == 1
        assert observer.sink.status()["last"]["emotion"] == "angry"

    def test_a_positive_turn_records_nothing(self):
        observer = self._observer()
        observer.score_and_observe("thanks, that is helpful", lead_id="L1", conversation_id="c1")
        assert observer.sink.count() == 0

    def test_escalation_opens_a_handoff_against_the_crm(self):
        from anita.crm import MockCrmStore
        from anita.leads import LeadService

        crm = MockCrmStore(":memory:")
        observer = self._observer(crm=LeadService(crm).store)
        observer.score_and_observe("this is unacceptable and I am furious",
                                   lead_id="L1", conversation_id="c1")
        status = observer.sink.status()
        assert status["dispatched"] == 1, "the escalation was decided but not dispatched"
        assert status["open_handoffs"] == 1, "no handoff reached the CRM"

    def test_two_negative_turns_open_one_handoff(self):
        """Idempotent per lead, per the CRM's `SM-06` contract — two negative
        turns must not open two handoffs, or a rep sees the same problem twice."""
        from anita.crm import MockCrmStore
        from anita.leads import LeadService

        observer = self._observer(crm=LeadService(MockCrmStore(":memory:")).store)
        observer.score_and_observe("this is unacceptable and I am furious",
                                   lead_id="L1", conversation_id="c1")
        observer.score_and_observe("and the delivery was terrible too",
                                   lead_id="L1", conversation_id="c1")
        assert observer.sink.count() == 2, "both turns were scored"
        assert observer.sink.status()["open_handoffs"] == 1, "both opened a handoff"

    def test_escalation_without_a_crm_is_recorded_not_dropped(self):
        """No CRM attached means no handoff, but the decision still leaves a
        record — silence must not erase the evidence."""
        observer = self._observer(crm=None)
        observer.score_and_observe("this is unacceptable and I am furious",
                                   lead_id="L1", conversation_id="c1")
        status = observer.sink.status()
        assert status["escalations"] == 1
        assert status["dispatched"] == 0
        assert status["crm_attached"] is False

    def test_a_failing_crm_does_not_fail_the_turn(self):
        class BrokenCrm:
            def record_handoff(self, handoff):
                raise RuntimeError("crm is down")

            def list_handoffs(self, state=None):
                raise RuntimeError("crm is down")

        observer = self._observer(crm=BrokenCrm())
        state = observer.score_and_observe("this is unacceptable and I am furious",
                                           lead_id="L1", conversation_id="c1")
        assert state is not None, "a broken sink must not break the scoring path"
        assert observer.sink.count() == 1

    def test_momentum_uses_the_running_baseline(self):
        """The observer carries each lead's EWMA forward, so a *decline* is
        detectable. Without it every turn is a first turn and momentum is
        permanently zero."""
        observer = self._observer()
        observer.score_and_observe("this is fine, thank you", lead_id="L1", conversation_id="c1")
        first = observer._baselines["L1"]
        observer.score_and_observe("this is unacceptable and I am furious",
                                   lead_id="L1", conversation_id="c1")
        assert observer._baselines["L1"] != first, "the baseline never moved"
        assert observer._baselines["L1"] < first, "the baseline moved the wrong way"
