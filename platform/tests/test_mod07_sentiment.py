"""MOD-07 tests — sentiment composition, categorisation, escalation.

Run:  .venv\\Scripts\\python -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.sentiment import (  # noqa: E402
    CATEGORY_ACTIONS,
    Category,
    Emotion,
    Extraction,
    ScoreState,
    SentimentService,
    Weights,
    build_extraction_prompt,
    categorise,
    composite_score,
    parse_extraction,
    rule_based_extraction,
    should_escalate,
)


# ---------------------------------------------------------------------------
# Rule-based fallback
# ---------------------------------------------------------------------------
class TestRuleBasedFallback:
    @pytest.mark.parametrize(("message", "emotion"), [
        ("This is unacceptable, ridiculous, terrible and the worst service",
         Emotion.ANGRY),
        ("Still waiting for a reply.", Emotion.FRUSTRATED),
        ("Please send me a quotation for 50 units", Emotion.READY),
        ("Thanks, that's helpful", Emotion.INTERESTED),
        ("What is the weight of the cassette?", Emotion.UNCERTAIN),
        ("Hello", Emotion.NEUTRAL),
    ])
    def test_emotions_are_recognised(self, message, emotion):
        assert rule_based_extraction(message).primary_emotion is emotion

    def test_negative_text_scores_negative(self):
        assert rule_based_extraction("This is ridiculous and unacceptable").sentiment_score < 0

    def test_positive_text_scores_positive(self):
        assert rule_based_extraction("Thanks, excellent service").sentiment_score > 0

    def test_scores_stay_in_range(self):
        for message in ["", "great great great great", "angry furious ridiculous terrible worst"]:
            score = rule_based_extraction(message).sentiment_score
            assert -1.0 <= score <= 1.0

    def test_fallback_marks_its_source(self):
        result = rule_based_extraction("hello")
        assert result.source == "rules"
        assert result.state is ScoreState.FALLBACK

    @pytest.mark.parametrize(("message", "label"), [
        ("that's too expensive for us", "price"),
        ("the minimum order is too high", "minimum_order"),
        ("is it compatible with our machine?", "compatibility"),
        ("do you provide service and spares?", "support"),
        ("can we get credit terms?", "payment_terms"),
    ])
    def test_objections_are_captured_as_labels(self, message, label):
        """BRD-17: 'too expensive' as a label is actionable; 'seemed unhappy
        about pricing' is not."""
        assert label in rule_based_extraction(message).objections

    def test_no_objection_is_invented(self):
        assert rule_based_extraction("Hello, good morning").objections == []


# ---------------------------------------------------------------------------
# LLM extraction parsing
# ---------------------------------------------------------------------------
class TestParseExtraction:
    def test_parses_clean_json(self):
        result = parse_extraction(
            '{"primary_emotion":"ready","sentiment_score":0.8,'
            '"buying_intent_score":0.9,"friction_score":0.1,"objections":["price"]}'
        )
        assert result.primary_emotion is Emotion.READY
        assert result.state is ScoreState.SCORED

    def test_parses_json_in_a_fence(self):
        """Models wrap JSON in prose and fences; that must not lose the turn."""
        result = parse_extraction('```json\n{"primary_emotion":"angry"}\n```')
        assert result.primary_emotion is Emotion.ANGRY

    def test_parses_json_embedded_in_prose(self):
        result = parse_extraction('Here is my analysis: {"primary_emotion":"neutral"} done.')
        assert result.primary_emotion is Emotion.NEUTRAL

    def test_unknown_emotion_degrades_to_neutral(self):
        assert parse_extraction('{"primary_emotion":"ecstatic"}').primary_emotion is Emotion.NEUTRAL

    def test_scores_are_clamped(self):
        result = parse_extraction('{"sentiment_score": 99, "buying_intent_score": -5}')
        assert result.sentiment_score == 1.0
        assert result.buying_intent_score == 0.0

    def test_unknown_objections_are_dropped(self):
        result = parse_extraction('{"objections":["price","nonsense_label"]}')
        assert result.objections == ["price"]

    def test_malformed_output_raises(self):
        with pytest.raises(ValueError):
            parse_extraction("I cannot help with that")

    def test_prompt_carries_the_product_taxonomy(self):
        """REC-05: the taxonomy is the re-target from admissions to product
        enquiries.

        Checked against the prompt ANITA actually serves (`ANITA_EXTRACTION_PROMPT`
        through `anita_sentiment_config`), not the shared package's fallback:
        that one deliberately names no industry and no objection vocabulary
        (`work-status.md` §10, CL-9). This test previously passed against the
        fallback, which is the wrong string to pin a product taxonomy to.
        """
        from anita.sentiment import anita_sentiment_config

        prompt = build_extraction_prompt(
            "is it expensive?",
            prompt=anita_sentiment_config().extraction_prompt,
        )
        assert "ready" in prompt
        assert "frustrated" in prompt
        assert "minimum_order" in prompt
        assert "admissions" not in prompt.lower()


# ---------------------------------------------------------------------------
# Composite scoring
# ---------------------------------------------------------------------------
class TestComposite:
    def test_positive_intent_raises_the_score(self):
        low = composite_score(0.2, intent=0.1)
        high = composite_score(0.2, intent=0.9)
        assert high.score > low.score

    def test_friction_lowers_the_score(self):
        calm = composite_score(0.0, friction=0.0)
        blocked = composite_score(0.0, friction=0.9)
        assert blocked.score < calm.score

    def test_momentum_distinguishes_repeated_negativity(self):
        """Two identical negative turns look the same on latest sentiment
        alone. The second is measurably worse — and that is the one worth
        escalating."""
        first = composite_score(-0.5, previous_latest=None)
        second = composite_score(-0.5, previous_latest=-0.5)
        third = composite_score(-0.5, previous_latest=0.4)
        assert third.score < second.score
        assert second.delta == pytest.approx(0.0)
        assert third.delta < 0

    def test_scores_are_clamped(self):
        assert -1.0 <= composite_score(-1.0, intent=0.0, friction=1.0).score <= 1.0
        assert composite_score(1.0, intent=1.0).score <= 1.0

    def test_baseline_is_an_ewma(self):
        """One bad turn must not permanently mark a lead, and one good turn must
        not erase a pattern."""
        result = composite_score(-1.0, baseline=0.0, weights=Weights(ewma_lambda=0.35))
        assert -1.0 < result.baseline < 0.0

    def test_turn_count_is_carried(self):
        assert composite_score(0.0, turn_count=7).turn_count == 7


# ---------------------------------------------------------------------------
# Categorisation
# ---------------------------------------------------------------------------
class TestCategorisation:
    def _composite(self, score=0.0, intent=0.0):
        return composite_score(score, intent=intent)

    def test_angry_is_at_risk(self):
        extraction = Extraction(primary_emotion=Emotion.ANGRY)
        assert categorise(self._composite(0.5), extraction) is Category.AT_RISK

    def test_negative_composite_is_at_risk(self):
        extraction = Extraction(primary_emotion=Emotion.NEUTRAL)
        assert categorise(self._composite(-0.9), extraction) is Category.AT_RISK

    def test_a_single_mildly_negative_turn_does_not_escalate_on_score_alone(self):
        """A documented property of the weighted formula.

        With delta and intent at zero, S = w1 x latest = 0.30 x latest, so a
        mildly negative turn (-0.5) scores -0.15 and does not cross -0.20. The
        composite is a *trend* signal; a mildly unhappy buyer is caught by the
        emotion check, not by arithmetic. Raising w1 to make mild negatives trip
        the threshold would also make mild positives look like hot leads.
        """
        composite = self._composite(-0.5)
        assert composite.score > -0.20
        assert categorise(composite, Extraction()) is not Category.AT_RISK

    def test_ready_with_intent_is_hot(self):
        extraction = Extraction(primary_emotion=Emotion.READY)
        assert categorise(self._composite(0.6, intent=0.9), extraction) is Category.HOT

    def test_engaged_is_warm(self):
        extraction = Extraction(primary_emotion=Emotion.INTERESTED)
        assert categorise(self._composite(0.2, intent=0.6), extraction) is Category.WARM

    def test_an_unhappy_but_interested_buyer_is_at_risk_not_hot(self):
        """Precedence matters. Someone unhappy *and* interested is the most
        valuable call a rep can make; burying them under 'Hot' loses the reason
        they need attention."""
        extraction = Extraction(primary_emotion=Emotion.FRUSTRATED, buying_intent_score=0.9)
        assert categorise(self._composite(0.8, intent=0.9), extraction) is Category.AT_RISK

    def test_every_category_has_an_action(self):
        for category in Category:
            assert category in CATEGORY_ACTIONS


# ---------------------------------------------------------------------------
# Escalation — BRD-16
# ---------------------------------------------------------------------------
class TestEscalation:
    def test_angry_escalates(self):
        escalate, reason = should_escalate(
            composite_score(0.9), Extraction(primary_emotion=Emotion.ANGRY)
        )
        assert escalate and "angry" in reason

    def test_negative_composite_escalates(self):
        escalate, reason = should_escalate(
            composite_score(-0.9), Extraction(primary_emotion=Emotion.NEUTRAL)
        )
        assert escalate and "<=" in reason

    def test_sharp_decline_escalates(self):
        """A buyer who was happy and is suddenly not is worth a call even
        before the composite crosses the threshold."""
        composite = composite_score(-0.1, previous_latest=0.4)
        assert composite.score > -0.20, "must not trip the threshold; this tests momentum only"
        escalate, reason = should_escalate(composite, Extraction())
        assert escalate and "decline" in reason

    def test_a_neutral_turn_does_not_escalate(self):
        escalate, _ = should_escalate(composite_score(0.1), Extraction())
        assert not escalate


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class TestSentimentService:
    def test_scores_a_turn(self):
        service = SentimentService()
        state, escalate, _ = service.observe("lead-1", "Please send a quotation")
        assert state.turn_count == 1
        assert state.category in {Category.HOT, Category.WARM}

    def test_escalates_a_frustrated_buyer(self):
        service = SentimentService()
        _, escalate, reason = service.observe(
            "lead-1", "This is unacceptable, I have waited three weeks"
        )
        assert escalate
        assert reason

    def test_escalation_handler_is_called_once(self):
        calls: list[str] = []
        service = SentimentService(
            on_escalate=lambda state, reason, extraction: calls.append(reason)
        )
        service.observe("lead-1", "This is ridiculous and unacceptable")
        assert len(calls) == 1

    def test_a_failing_escalation_handler_does_not_lose_the_score(self):
        """Best-effort: an escalation-sink failure must not discard the score."""
        def broken(state, reason, extraction):
            raise RuntimeError("notification service down")

        service = SentimentService(on_escalate=broken)
        state, escalate, _ = service.observe("lead-1", "unacceptable and ridiculous")
        assert escalate
        assert state.turn_count == 1

    def test_a_failing_scorer_degrades_to_rules(self):
        """The model may be down; escalation must not be."""
        def broken_scorer(message):
            raise RuntimeError("LLM unavailable")

        service = SentimentService(scorer=broken_scorer)
        state, escalate, _ = service.observe("lead-1", "This is unacceptable")
        assert escalate, "escalation must survive an LLM outage"
        assert state.score_state is ScoreState.FALLBACK

    def test_the_llm_scorer_is_preferred_when_it_works(self):
        service = SentimentService(
            scorer=lambda message: Extraction(
                primary_emotion=Emotion.READY, sentiment_score=0.9,
                buying_intent_score=0.95, state=ScoreState.SCORED,
            )
        )
        state, _, _ = service.observe("lead-1", "hello")
        assert state.score_state is ScoreState.SCORED

    def test_history_accumulates(self):
        service = SentimentService()
        for message in ["hello", "what is the price?", "thanks"]:
            service.observe("lead-1", message)
        assert service.for_lead("lead-1").turn_count == 3

    def test_leads_are_tracked_independently(self):
        service = SentimentService()
        service.observe("lead-1", "thanks, great")
        service.observe("lead-2", "this is ridiculous and unacceptable")
        assert service.for_lead("lead-1").category is not Category.AT_RISK
        assert service.for_lead("lead-2").category is Category.AT_RISK

    def test_at_risk_leads_are_listed(self):
        service = SentimentService()
        service.observe("lead-1", "thanks")
        service.observe("lead-2", "unacceptable, ridiculous, terrible")
        ids = [state.lead_id for state in service.at_risk()]
        assert "lead-2" in ids and "lead-1" not in ids

    def test_summary_counts_categories(self):
        service = SentimentService()
        service.observe("lead-1", "thanks")
        service.observe("lead-2", "unacceptable")
        summary = service.summary()
        assert summary["leads_scored"] == 2
        assert sum(summary["by_category"].values()) == 2
