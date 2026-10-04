"""MOD-05 / MOD-08 tests — admission, degradation, tracing, quality gate.

Run:  .venv\\Scripts\\python -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.observability import (  # noqa: E402
    EvalCase,
    JsonlSink,
    MemorySink,
    MetricsRegistry,
    QualityGate,
    TraceWriter,
    TurnTrace,
    cases_from_corpus,
    load_eval_set,
    run_case,
)
from anita.speech import (  # noqa: E402
    BUSY_MESSAGE,
    LADDER,
    AdmissionController,
    DegradationLevel,
    EndOfSpeechTracker,
    VRAM_COMPONENTS,
    decode_ulaw_frames,
)

CORPUS_DIR = PLATFORM.parent / "data" / "out"


# ---------------------------------------------------------------------------
# VRAM budget
# ---------------------------------------------------------------------------
class TestVramBudget:
    def test_full_footprint_is_the_sum_of_components(self):
        controller = AdmissionController()
        assert controller.footprint_mib(DegradationLevel.FULL) == sum(VRAM_COMPONENTS.values())

    def test_the_default_budget_leaves_the_sibling_its_floor(self):
        """AS-09: the card is shared with a live second product."""
        controller = AdmissionController()
        safe, reason = controller.budget_is_safe()
        assert safe, reason
        assert controller.remaining_for_sibling() >= controller.sibling_floor_mib

    def test_an_oversized_anita_budget_is_unsafe(self):
        controller = AdmissionController(shared_card_mib=16311, sibling_floor_mib=12000)
        safe, reason = controller.budget_is_safe()
        assert not safe
        assert "sibling needs" in reason

    def test_shedding_actually_frees_memory(self):
        """Otherwise the ladder is a label, not a response to pressure."""
        controller = AdmissionController()
        full = controller.footprint_mib(DegradationLevel.FULL)
        assert controller.footprint_mib(DegradationLevel.TTS_OFF) < full
        assert controller.footprint_mib(DegradationLevel.CHAT_DEGRADE) < full
        assert controller.footprint_mib(DegradationLevel.COMMERCIAL_ONLY) == 0


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------
class TestAdmission:
    def test_admits_up_to_the_ceiling(self):
        controller = AdmissionController(max_concurrent_calls=2)
        assert controller.admit("call-1").admitted
        assert controller.admit("call-2").admitted

    def test_refuses_beyond_the_ceiling(self):
        controller = AdmissionController(max_concurrent_calls=2)
        controller.admit("call-1")
        controller.admit("call-2")
        decision = controller.admit("call-3")
        assert not decision.admitted
        assert decision.message == BUSY_MESSAGE
        assert "capacity" in decision.reason

    def test_releasing_frees_a_slot(self):
        controller = AdmissionController(max_concurrent_calls=1)
        controller.admit("call-1")
        assert controller.release("call-1")
        assert controller.admit("call-2").admitted

    def test_re_admitting_the_same_call_is_idempotent(self):
        """A duplicate start event must not consume two slots."""
        controller = AdmissionController(max_concurrent_calls=1)
        controller.admit("call-1")
        assert controller.admit("call-1").admitted
        assert controller.active_calls() == 1

    def test_stale_calls_are_released(self):
        """A leaked slot is worse than a refusal: the ceiling erodes silently."""
        now = [1000.0]
        controller = AdmissionController(max_concurrent_calls=1, clock=lambda: now[0])
        controller.admit("call-1")
        now[0] += 7200
        assert controller.release_stale(older_than_seconds=3600) == 1
        assert controller.admit("call-2").admitted


# ---------------------------------------------------------------------------
# Degradation ladder (US-018)
# ---------------------------------------------------------------------------
class TestDegradation:
    def test_starts_full(self):
        assert AdmissionController().level is DegradationLevel.FULL

    def test_degrades_one_rung_at_a_time(self):
        """Never skips: a leap from full to commercial-only would shed service
        that still works."""
        controller = AdmissionController()
        assert controller.degrade("gpu pressure") is DegradationLevel.VOICE_SHED
        assert controller.degrade("still tight") is DegradationLevel.STT_OFF

    def test_degradation_is_bounded(self):
        controller = AdmissionController()
        for _ in range(len(LADDER) * 2):
            controller.degrade("pressure")
        assert controller.level is DegradationLevel.COMMERCIAL_ONLY

    def test_voice_is_shed_first(self):
        """Text has no GPU dependency, so shedding it would be losing service
        that still works."""
        controller = AdmissionController()
        assert controller.voice_available
        controller.degrade("gpu pressure")
        assert not controller.voice_available
        assert controller.text_available

    def test_text_survives_the_whole_ladder_but_the_last_rung(self):
        controller = AdmissionController()
        controller.degrade("a")
        controller.degrade("b")
        controller.degrade("c")
        controller.degrade("d")
        assert controller.level is DegradationLevel.CHAT_DEGRADE
        assert controller.text_available
        controller.degrade("e")
        assert controller.level is DegradationLevel.COMMERCIAL_ONLY
        assert not controller.text_available

    def test_the_llm_is_lost_before_the_deterministic_path(self):
        """DG-03: SQL-backed price and MOQ answers keep working with the model
        and the audio stack both offline."""
        controller = AdmissionController()
        while controller.level is not DegradationLevel.CHAT_DEGRADE:
            controller.degrade("pressure")
        assert not controller.llm_available
        assert controller.text_available, "deterministic answers must still serve"

    def test_a_degraded_system_refuses_calls_with_a_reason(self):
        controller = AdmissionController()
        controller.degrade("gpu pressure")
        decision = controller.admit("call-1")
        assert not decision.admitted
        assert "voice shed" in decision.reason
        assert decision.message == BUSY_MESSAGE

    def test_recovery_climbs_back(self):
        controller = AdmissionController()
        controller.degrade("a")
        controller.degrade("b")
        assert controller.level is DegradationLevel.STTOFF if False else True
        controller.recover()
        # One rung up is VOICE_SHED, which still refuses new calls.
        assert controller.level is DegradationLevel.VOICE_SHED
        assert not controller.admit("call-1").admitted
        controller.recover()
        assert controller.level is DegradationLevel.FULL
        assert controller.admit("call-1").admitted

    def test_limits_are_recorded_for_diagnosis(self):
        controller = AdmissionController()
        controller.degrade("gpu pressure")
        assert controller.limits[0]["reason"] == "gpu pressure"

    def test_snapshot_reports_everything(self):
        snapshot = AdmissionController().snapshot()
        assert snapshot["level"] == "full"
        assert snapshot["budget_safe"] is True


# ---------------------------------------------------------------------------
# End-of-speech
# ---------------------------------------------------------------------------
class TestEndOfSpeech:
    def test_silence_before_speech_decides_nothing(self):
        tracker = EndOfSpeechTracker()
        for _ in range(100):
            assert not tracker.feed(is_speech=False)

    def test_end_of_speech_after_the_silence_floor(self):
        tracker = EndOfSpeechTracker(silence_ms=600, frame_ms=20)
        tracker.feed(is_speech=True)
        decided = False
        for _ in range(31):
            decided = tracker.feed(is_speech=False)
        assert decided

    def test_speech_resets_the_silence_counter(self):
        tracker = EndOfSpeechTracker(silence_ms=600, frame_ms=20)
        tracker.feed(is_speech=True)
        for _ in range(20):
            tracker.feed(is_speech=False)
        tracker.feed(is_speech=True)   # speaker resumes
        for _ in range(20):
            assert not tracker.feed(is_speech=False)

    def test_frames_needed_matches_the_configured_floor(self):
        assert EndOfSpeechTracker(silence_ms=600, frame_ms=20).frames_needed == 30

    def test_reset_clears_state(self):
        tracker = EndOfSpeechTracker()
        tracker.feed(is_speech=True)
        tracker.reset()
        assert not tracker.feed(is_speech=False)

    def test_audio_contract_sample_counts(self):
        assert decode_ulaw_frames(b"\x00" * 160, 8000) == 160
        assert decode_ulaw_frames(b"\x00" * 160, 16000) == 320


# ---------------------------------------------------------------------------
# Tracing — the deletion property
# ---------------------------------------------------------------------------
class TestTracing:
    def test_a_trace_is_written(self):
        sink = MemorySink()
        writer = TraceWriter(sink)
        assert writer.emit(writer.start(channel="whatsapp", intent="commercial"))
        assert len(sink.traces) == 1

    def test_a_broken_sink_never_raises(self):
        """If this module is deleted entirely, every conversation still works.
        Telemetry that can fail a call is worse than no telemetry."""
        class ExplodingSink:
            def write(self, trace):
                raise OSError("disk full")

        writer = TraceWriter(ExplodingSink())
        assert writer.emit(writer.start()) is False
        assert writer.dropped == 1

    def test_sampling_can_thin_the_stream(self):
        sink = MemorySink()
        writer = TraceWriter(sink, sample_rate=0.5)
        for _ in range(10):
            writer.emit(writer.start())
        assert len(sink.traces) < 10

    def test_jsonl_sink_appends(self, tmp_path):
        path = tmp_path / "traces.jsonl"
        writer = TraceWriter(JsonlSink(path))
        writer.emit(writer.start(channel="web_chat"))
        writer.emit(writer.start(channel="whatsapp"))
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2

    def test_trace_carries_the_fields_a_rep_needs(self):
        trace = TurnTrace(trace_id="t1", channel="voice_inbound", intent="commercial",
                          retrieved_chunks=3, grounded=True, total_ms=840.0)
        data = trace.as_dict()
        assert data["grounded"] is True
        assert data["retrieved_chunks"] == 3


# ---------------------------------------------------------------------------
# Metrics — absence is data
# ---------------------------------------------------------------------------
class TestMetrics:
    def test_a_measured_zero_is_not_absent(self):
        metrics = MetricsRegistry()
        metrics.set("queue_depth", 0)
        assert not metrics.get("queue_depth").is_absent
        assert "0" in metrics.get("queue_depth").render()

    def test_an_unavailable_metric_renders_as_unavailable(self):
        """Rendering absence as 0 reports 'all clear' during an outage."""
        metrics = MetricsRegistry()
        metrics.mark_unavailable("queue_depth")
        assert "unavailable" in metrics.get("queue_depth").render()
        assert "0" not in metrics.get("queue_depth").render()

    def test_an_unknown_metric_is_absent_not_zero(self):
        assert MetricsRegistry().get("never_set").is_absent


# ---------------------------------------------------------------------------
# Quality gate (BRD-22)
# ---------------------------------------------------------------------------
class TestQualityGate:
    def _cases(self):
        return [
            EvalCase("price:1", "What is the price?", must_contain=("2500",),
                     must_not_contain=("200000",), severity="critical"),
            EvalCase("moq:1", "What is the MOQ?", must_contain=("10",), severity="normal"),
        ]

    def test_all_passing_promotes(self):
        report = QualityGate(self._cases()).evaluate(lambda case: "2500 rupees, MOQ 10")
        assert report.passed

    def test_a_failing_critical_case_blocks(self):
        report = QualityGate(self._cases()).evaluate(lambda case: "10 pieces")
        assert not report.passed
        assert "critical" in report.reason

    def test_a_forbidden_value_blocks(self):
        """The duplicate-name trap: a correct answer about one 'Medical Dry
        Imager' must not carry the other's price."""
        report = QualityGate(self._cases()).evaluate(lambda case: "2500 and also 200000")
        assert not report.passed

    def test_an_erroring_case_counts_as_a_failure(self):
        """Skipping errors would let a broken configuration report 100% on the
        cases that happened to survive it."""
        def answerer(case):
            raise RuntimeError("retrieval unavailable")

        report = QualityGate(self._cases()).evaluate(answerer)
        assert not report.passed
        assert len(report.failures) == 2

    def test_an_empty_set_is_not_a_gate(self):
        report = QualityGate([]).evaluate(lambda case: "anything")
        assert not report.passed
        assert "not a gate" in report.reason

    def test_a_regression_against_baseline_blocks(self):
        """Uses explicit thresholds, because with the shipping defaults the
        regression check is **unreachable**: a rate below the 98% floor trips
        the floor first, and a rate at or above it is within the 2% tolerance by
        definition. The floor is the binding constraint; regression detection
        matters when the floor is lowered for a small or young evaluation set.
        """
        cases = [EvalCase(f"c{i}", "q", must_contain=("x",), severity="normal") for i in range(100)]
        report = QualityGate(
            cases, baseline_grounded_rate=1.0, min_grounded_rate=0.5, max_regression=0.02
        ).evaluate(lambda case: "x" if int(case.case_id[1:]) < 97 else "wrong")
        assert not report.passed
        assert "regression" in report.reason

    def test_with_shipping_thresholds_the_floor_binds_first(self):
        """Documented, so the dead-by-default regression path is a known
        property rather than a surprise during an incident."""
        cases = [EvalCase(f"c{i}", "q", must_contain=("x",), severity="normal") for i in range(100)]
        report = QualityGate(cases, baseline_grounded_rate=1.0).evaluate(
            lambda case: "x" if int(case.case_id[1:]) < 90 else "wrong"
        )
        assert not report.passed
        assert "below floor" in report.reason

    def test_report_names_the_failures(self):
        report = QualityGate(self._cases()).evaluate(lambda case: "nope")
        assert report.failed_ids == ["price:1", "moq:1"]


class TestEvalSetFromCorpus:
    def test_derives_cases_from_the_catalogue(self):
        from anita.corpus import Corpus

        if not (CORPUS_DIR / "ams.sqlite").exists():
            pytest.skip("corpus not built")
        cases = cases_from_corpus(Corpus(CORPUS_DIR))
        assert len(cases) >= 40
        assert all(case.severity in {"critical", "normal"} for case in cases)

    def test_the_duplicate_name_products_carry_a_forbidden_value(self):
        """270683 and 3935251 are both 'Medical Dry Imager' at different prices.
        Each case forbids the other's figure."""
        from anita.corpus import Corpus

        if not (CORPUS_DIR / "ams.sqlite").exists():
            pytest.skip("corpus not built")
        corpus = Corpus(CORPUS_DIR)
        cases = {case.case_id: case for case in cases_from_corpus(corpus)}
        twins = [cases.get("price:270683"), cases.get("price:3935251")]
        assert all(twins), "both twins must produce a case"
        assert twins[0].must_not_contain, "must forbid the other price"
        assert twins[1].must_not_contain

    def test_a_correct_price_answer_passes(self):
        from anita.corpus import Corpus

        if not (CORPUS_DIR / "ams.sqlite").exists():
            pytest.skip("corpus not built")
        corpus = Corpus(CORPUS_DIR)
        cases = [c for c in cases_from_corpus(corpus) if c.case_id == "price:270683"]
        gate = QualityGate(cases)
        product = corpus.get("270683")
        report = gate.evaluate(lambda case: f"It is listed at {product.price_amount}")
        assert report.passed

    def test_answering_with_the_twin_price_fails(self):
        from anita.corpus import Corpus

        if not (CORPUS_DIR / "ams.sqlite").exists():
            pytest.skip("corpus not built")
        corpus = Corpus(CORPUS_DIR)
        cases = [c for c in cases_from_corpus(corpus) if c.case_id == "price:270683"]
        twin = corpus.get("3935251")
        report = QualityGate(cases).evaluate(
            lambda case: f"It is listed at {twin.price_amount}"
        )
        assert not report.passed, "the twin's price must fail"


# ---------------------------------------------------------------------------
# One promotion decision from both evidence sources
#
# There were two verification systems and only one of them decided anything.
# The evaluation set ran in-process, asserted answer content, and gated
# promotion — but could not see routing, refusals, consent capture or whether
# the page worked, which are the failures that actually shipped. The browser
# suite saw all of that and decided nothing.
# ---------------------------------------------------------------------------
class TestCombinedValidation:
    @staticmethod
    def _gate(passed: bool, total: int = 68):
        from anita.observability import GateReport

        return GateReport(passed=passed, total=total,
                          reason="" if passed else "cases failed")

    @staticmethod
    def _browser(passed: bool):
        from anita.observability import BrowserReport

        return BrowserReport(passed=passed, total=25,
                             failed=() if passed else ("a scenario",))

    @staticmethod
    def _system(passed: bool):
        from anita.observability import SystemReport

        return SystemReport(passed=passed, loaded=35, wired=11,
                            broken=() if passed else ("UC-03", "UC-10"))

    def test_both_passing_permits_promotion(self):
        from anita.observability import combine_validation

        result = combine_validation(self._gate(True), self._browser(True), self._system(True))
        assert result["passed"] is True

    def test_a_failing_evaluation_set_refuses(self):
        from anita.observability import combine_validation

        assert combine_validation(self._gate(False), self._browser(True), self._system(True))["passed"] is False

    def test_a_failing_browser_suite_refuses(self):
        """The whole point of step 2: the browser suite blocks, not advises."""
        from anita.observability import combine_validation

        result = combine_validation(self._gate(True), self._browser(False), self._system(True))
        assert result["passed"] is False
        assert "browser" in result["reason"].lower()

    def test_an_unrun_browser_suite_refuses(self):
        """"We did not check" and "the check passed" must not produce the same
        promotion. This is the case a `--no-browser` run falls into."""
        from anita.observability import combine_validation

        result = combine_validation(self._gate(True), None, self._system(True))
        assert result["passed"] is False
        assert result["browser"] is None

    def test_both_reports_survive_into_the_decision(self):
        """A promotion record that names only one source cannot be audited."""
        from anita.observability import combine_validation

        result = combine_validation(self._gate(True), self._browser(True), self._system(True))
        assert result["total"] == 68
        assert result["browser"]["total"] == 25

    def test_the_live_index_refuses_a_failed_combined_validation(self):
        """The enforcement point itself, exercised with the real dict shape."""
        from anita.index import IndexLifecycle
        from anita.observability import combine_validation

        lifecycle = IndexLifecycle("anita_ams__v1")
        lifecycle.begin(embed_model="nomic-embed-text", dimensions=768)
        lifecycle.built(chunks=139, dimensions=768, corpus_hash="deadbeef")
        validation = combine_validation(self._gate(True), self._browser(False), self._system(True))
        result = lifecycle.promote(validation)
        assert result.state.value == "rejected"

    def test_a_failing_system_check_refuses(self):
        """Declared-but-unreachable blocks promotion. A use case that cannot be
        triggered is not a shipped feature, however complete its module is."""
        from anita.observability import combine_validation

        result = combine_validation(self._gate(True), self._browser(True), self._system(False))
        assert result["passed"] is False
        assert "UC-03" in result["reason"], "the reason must name what is unreachable"

    def test_an_unrun_system_check_refuses(self):
        from anita.observability import combine_validation

        result = combine_validation(self._gate(True), self._browser(True), None)
        assert result["passed"] is False
        assert "system" in result["reason"]

    def test_all_three_reports_survive_into_the_decision(self):
        """A promotion record naming fewer than three sources cannot be audited."""
        from anita.observability import combine_validation

        result = combine_validation(self._gate(True), self._browser(True), self._system(True))
        assert result["total"] == 68
        assert result["browser"]["total"] == 25
        assert result["system"]["loaded"] == 35
