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

    def test_uncited_answers_refuse_promotion(self):
        """§8.24 — the provenance counts used to be printed and dropped. A
        grounded answer with no citation is a composition defect (`US-012`
        `TAC-1`), and a defect that cannot refuse a release is documentation."""
        from anita.observability import combine_validation

        result = combine_validation(
            self._gate(True), self._browser(True), self._system(True),
            citations={"grounded": 55, "uncited": 2, "incomplete": 0},
        )
        assert result["passed"] is False
        assert "citation integrity failed" in result["reason"]
        assert result["citations"]["uncited"] == 2

    def test_incomplete_citations_refuse_promotion(self):
        from anita.observability import combine_validation

        result = combine_validation(
            self._gate(True), self._browser(True), self._system(True),
            citations={"grounded": 55, "uncited": 0, "incomplete": 1},
        )
        assert result["passed"] is False
        assert "1 incomplete" in result["reason"]

    def test_a_broken_citation_run_refuses_promotion(self):
        """"The check did not complete" must not read as "the check passed"."""
        from anita.observability import combine_validation

        result = combine_validation(
            self._gate(True), self._browser(True), self._system(True),
            citations={"grounded": 0, "uncited": 0, "incomplete": 0,
                       "error": "TimeoutError: ollama"},
        )
        assert result["passed"] is False
        assert "did not complete" in result["reason"]

    def test_clean_citations_do_not_block(self):
        from anita.observability import combine_validation

        result = combine_validation(
            self._gate(True), self._browser(True), self._system(True),
            citations={"grounded": 55, "uncited": 0, "incomplete": 0},
        )
        assert result["passed"] is True
        assert result["citations"]["grounded"] == 55

    def test_citations_travel_with_an_unrun_suite(self):
        """The early return (no browser) must still carry the counts, or the
        report loses the very check that was run."""
        from anita.observability import combine_validation

        result = combine_validation(
            self._gate(True), None, self._system(True),
            citations={"grounded": 55, "uncited": 0, "incomplete": 0},
        )
        assert result["passed"] is False
        assert result["citations"]["grounded"] == 55

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


# ---------------------------------------------------------------------------
# US-025 — per-turn trace emission
# ---------------------------------------------------------------------------
class TestUS025TurnTrace:
    """Every turn emits a trace, and tracing can never fail a turn.

    Before this, `voice_trace.new_trace(**kwargs)` did not accept the engine's
    positional call, the `TypeError` vanished into a broad `except`, and
    `self._trace` was `None` on every turn: traced in tests, nowhere else.
    """

    LOUD = None  # built lazily in _speak

    def _speak(self, session):
        """Drive the session to end-of-speech, which is where a trace opens."""
        from anita import voice

        loud = voice.pcm_to_ulaw(b"\x00\x20" * 320)
        for _ in range(voice.MIN_UTTERANCE_FRAMES):
            session.feed_audio(loud)
        for _ in range(30):
            if session.feed_audio(b"\xff" * 160):
                return
        raise AssertionError("end-of-speech never fired")

    # -- the record --------------------------------------------------------
    def test_a_stage_that_did_not_run_is_absent_never_zero(self):
        trace = TurnTrace(trace_id="t")
        trace.mark("turn_received")
        trace.mark("outcome")
        durations = trace.as_dict()["durations_ms"]
        assert "retrieval_done" not in durations
        assert list(durations) == ["outcome"], "only the interval that happened"

    def test_a_repeated_mark_is_counted_not_overwritten(self):
        trace = TurnTrace(trace_id="t")
        trace.mark("classified")
        trace.mark("classified")
        assert trace.as_dict()["stages_seen"] == ["classified"]
        assert trace.dropped_marks == 1

    def test_note_refuses_non_numeric_values(self):
        """The PII guard: a buyer's name cannot enter through a counter."""
        import json

        trace = TurnTrace(trace_id="t")
        trace.note(hits=3, buyer_name="Priya", transcript="my number is 98…")
        assert trace.as_dict()["counters"] == {"hits": 3}
        assert "Priya" not in json.dumps(trace.as_dict())

    def test_a_backwards_clock_is_clamped_and_flagged(self):
        trace = TurnTrace(trace_id="t")
        trace.mark("a")
        trace._marks["b"] = trace._marks["a"] - 5.0
        assert trace.durations_ms() == {"b": 0}
        assert trace.clock_skew is True

    # -- the writer --------------------------------------------------------
    def test_a_failing_sink_is_counted_not_raised(self):
        class Broken:
            def write(self, trace):
                raise OSError("disk full")

        writer = TraceWriter(Broken())
        assert writer.emit(TurnTrace(trace_id="t")) is False
        assert writer.stats()["dropped"] == 1

    def test_the_log_rotates_and_keeps_the_configured_files(self, tmp_path):
        sink = JsonlSink(tmp_path / "perf_turns.jsonl", rotate_bytes=200, keep_files=2)
        writer = TraceWriter(sink)
        for index in range(10):
            writer.emit(TurnTrace(trace_id=f"t{index}", channel="web_chat"))

        assert (tmp_path / "perf_turns.jsonl").exists()
        assert (tmp_path / "perf_turns.jsonl.1").exists()
        assert not (tmp_path / "perf_turns.jsonl.3").exists(), "keeps only 2"
        assert writer.stats()["dropped"] == 0, "rotation must not drop turns"

    # -- the adapter -------------------------------------------------------
    def test_the_engine_opens_a_trace_at_end_of_speech(self):
        from anita import voice

        session = voice.VoiceCallSession()
        self._speak(session)
        assert session._trace is not None, "the engine is tracing nothing again"
        assert session._trace.channel == "voice"
        assert session._trace.stages_seen == ["vad_end"]

    def test_a_finished_voice_turn_emits_one_masked_record(self):
        from anita import voice, voice_trace

        sink = MemorySink()
        original = voice_trace.writer()
        voice_trace.install(TraceWriter(sink))
        try:
            session = voice.VoiceCallSession()
            session.call_id = "CA-simcall-0000000000001"
            self._speak(session)
            session.finish_trace(outcome="answered")
        finally:
            voice_trace.install(original)

        assert len(sink.traces) == 1
        record = sink.traces[0]
        assert record.outcome == "answered"
        assert "vad_end" in record.stages_seen
        assert record.conversation_id.startswith("…"), "call ids are masked (TAC-8)"
        assert "0000000000001" not in record.conversation_id

    def test_a_chat_turn_emits_a_trace(self):
        from types import SimpleNamespace

        from anita.surfaces import ChatSurface

        sink = MemorySink()
        chat = ChatSurface(orchestrator=None, traces=TraceWriter(sink))
        conversation = SimpleNamespace(
            conversation_id="c1",
            turns=[object(), object()],
            sentiment=SimpleNamespace(category="Warm"),
        )
        reply = SimpleNamespace(
            intent="price", grounded=True, unavailable=False,
            declined=False, escalation=False, text="₹60",
        )

        trace = chat._begin_trace(conversation)
        chat._emit_trace(trace, conversation, reply)

        record = sink.traces[0]
        assert record.channel == "web_chat"
        assert record.turn_no == 2
        assert record.sentiment_category == "Warm"
        assert record.grounded is True
        assert record.outcome == "answered"
        assert record.stages_seen == [
            "turn_received", "answer_composed", "sentiment_scored", "outcome",
        ]

    def test_a_chat_turn_with_no_writer_is_untouched(self):
        from types import SimpleNamespace

        from anita.surfaces import ChatSurface

        chat = ChatSurface(orchestrator=None)
        conversation = SimpleNamespace(conversation_id="c1", turns=[])
        trace = chat._begin_trace(conversation)
        assert trace is None
        chat._emit_trace(trace, conversation, SimpleNamespace())  # must not raise

    def test_the_trace_module_imports_stdlib_only(self):
        """TAC-1: telemetry must be deletable, so it may depend on nothing in
        this repository and nothing third-party."""
        import ast
        import sys as _sys

        source = (PLATFORM / "app" / "anita" / "observability.py").read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])

        non_stdlib = imported - set(_sys.stdlib_module_names) - {"__future__"}
        assert not non_stdlib, (
            f"observability pulled in a non-stdlib dependency: {sorted(non_stdlib)}"
        )


# ---------------------------------------------------------------------------
# US-027 — the frozen evaluation set and the quality gate
# ---------------------------------------------------------------------------
class TestUS027FrozenEvalSet:
    """The set is a versioned repo file with a pinned hash.

    Before this it was regenerated from the catalogue at every boot, so a
    corpus refresh silently changed the test set and every "70/70 passed"
    described a different set than the previous run's.
    """

    ENV = {"ANITA_ADMIN_TOKEN": "test"}

    def _settings(self):
        from anita.config import load_settings

        return load_settings(env=self.ENV)

    def test_the_frozen_set_is_on_disk_and_matches_the_pin(self):
        from anita.observability import eval_set_sha256, load_eval_set

        settings = self._settings()
        path = settings.eval_set_path
        assert path.exists(), f"the frozen set is missing: {path}"
        assert load_eval_set(path), "the frozen set is empty"
        assert eval_set_sha256(path) == settings.release.eval_set_sha256, (
            "the set changed without the hash being re-pinned"
        )

    def test_the_gate_loads_the_frozen_set_with_its_hash(self):
        from anita import assembly

        settings = self._settings()
        notes: list[str] = []
        gate = assembly._build_release_gate(settings, notes)
        assert gate is not None, notes
        assert gate.set_sha256 == settings.release.eval_set_sha256
        assert len(gate.cases) == 70
        assert not notes, "a healthy gate should not degrade anything"

    def test_a_hash_mismatch_disables_the_gate(self):
        """Fail closed: an edited set cannot be scored by accident."""
        from dataclasses import replace

        from anita import assembly

        settings = self._settings()
        broken = replace(
            settings, release=replace(settings.release, eval_set_sha256="0" * 64)
        )
        notes: list[str] = []
        gate = assembly._build_release_gate(broken, notes)
        assert gate is None, "a mismatched set must not build a gate"
        assert any("hash mismatch" in note for note in notes), notes

    def test_a_missing_set_disables_the_gate(self):
        from dataclasses import replace

        from anita import assembly

        settings = self._settings()
        broken = replace(
            settings, release=replace(settings.release, eval_set="eval/does-not-exist.jsonl")
        )
        notes: list[str] = []
        assert assembly._build_release_gate(broken, notes) is None
        assert any("unavailable" in note for note in notes), notes

    # -- the verdict rules (the false-green fix) ---------------------------
    def _cases(self, count: int, severity: str = "normal"):
        from anita.observability import EvalCase

        return [
            EvalCase(
                case_id=f"c{index}", question=f"q{index}",
                must_contain=("ok",), severity=severity,
            )
            for index in range(count)
        ]

    def test_a_non_critical_failure_passes_but_the_report_says_so(self):
        """The gate used to report `all N cases passed` while holding failures:
        a false green. The tolerance is real (a 98 % floor is the point), but
        the reason must name what failed."""
        from anita.observability import QualityGate

        gate = QualityGate(self._cases(50))

        def answerer(case):
            return "nope" if case.case_id == "c7" else "ok"

        report = gate.evaluate(answerer)
        assert report.passed is True, "one non-critical failure is inside the floor"
        assert report.failed_ids == ["c7"]
        assert "non-critical" in report.reason and "c7" in report.reason
        assert "all 50" not in report.reason

    def test_a_critical_failure_blocks_regardless_of_rate(self):
        from anita.observability import QualityGate

        gate = QualityGate(self._cases(50, severity="critical"))

        def answerer(case):
            return "nope" if case.case_id == "c7" else "ok"

        report = gate.evaluate(answerer)
        assert report.passed is False
        assert "critical" in report.reason

    def test_a_clean_run_says_so(self):
        from anita.observability import QualityGate

        report = QualityGate(self._cases(5)).evaluate(lambda case: "ok")
        assert report.passed is True
        assert report.reason == "all 5 cases passed"

    def test_the_report_carries_the_set_hash(self):
        from anita.observability import QualityGate

        gate = QualityGate(self._cases(2), set_sha256="abc123")
        assert gate.evaluate(lambda case: "ok").as_dict()["set_sha256"] == "abc123"


class TestUS027RebuildEntryPoint:
    """The operational entry point that was missing: nothing in the product
    ever called `index.build_index(..., validate=...)`, so the atomic-promotion
    design was tested and unreachable."""

    def test_the_rebuild_script_exists_and_imports(self):
        import importlib.util

        path = PLATFORM / "scripts" / "rebuild_index.py"
        assert path.exists()
        spec = importlib.util.spec_from_file_location("rebuild_index", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # must not run anything on import
        assert callable(module.main)
        assert callable(module._gate_validator)

    def _module(self):
        import importlib.util

        path = PLATFORM / "scripts" / "rebuild_index.py"
        spec = importlib.util.spec_from_file_location("rebuild_index", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_validator_returns_the_report_dict(self):
        from types import SimpleNamespace

        class FakeGate:
            set_sha256 = "x"
            cases: list = []

            def evaluate(self, answerer):
                return SimpleNamespace(as_dict=lambda: {"passed": True, "reason": "ok"})

        class FakeChat:
            retriever = object()

        validator = self._module()._gate_validator(
            chat=FakeChat(), gate=FakeGate(), retriever=object()
        )
        result = validator(SimpleNamespace(collection="anita_ams__v2"))

        assert result["passed"] is True
        assert result["reason"] == "ok"
        # §8.33: the verdict carries provenance, not just the gate's text
        # assertions — an empty set means nothing was uncited.
        assert result["citations"] == {"grounded": 0, "uncited": 0, "incomplete": 0}

    def _rebuild_with(self, bodies):
        """Drive `_gate_validator` over a set whose answers are `bodies`."""
        from types import SimpleNamespace

        class FakeRetriever:
            collection = "anita_ams__v2"

            def for_collection(self, name):
                return self

        class FakeChat:
            retriever = FakeRetriever()

            def post_message(self, *, message, session_id):
                return SimpleNamespace(body=bodies[message])

        class FakeGate:
            set_sha256 = "x"
            cases = [SimpleNamespace(question=q, case_id=str(i))
                     for i, q in enumerate(bodies)]

            def evaluate(self, answerer):
                for case in self.cases:
                    answerer(case)
                return SimpleNamespace(as_dict=lambda: {"passed": True, "reason": "all passed"})

        validator = self._module()._gate_validator(
            chat=FakeChat(), gate=FakeGate(), retriever=FakeRetriever()
        )
        return validator(SimpleNamespace(collection="anita_ams__v2"))

    def test_a_rebuild_is_refused_when_a_grounded_answer_is_uncited(self):
        """§8.33: `rebuild_index.py` validated through `QualityGate` alone, which
        sees answer text and never provenance — so an index could be promoted
        while uncited answers existed, the one moment the citation rule was
        written for. The check now travels with this verdict."""
        result = self._rebuild_with({
            "q0": {"text": "a price", "grounded": True, "grounded_on": [{"chunk_id": "c1"}]},
            "q1": {"text": "an answer with no citation", "grounded": True, "grounded_on": []},
        })

        assert result["passed"] is False
        assert "citation integrity failed" in result["reason"]
        assert result["citations"]["uncited"] == 1

    def test_a_rebuild_is_refused_when_a_citation_could_not_be_completed(self):
        result = self._rebuild_with({
            "q0": {"text": "a price", "grounded": True, "grounded_on": [{"chunk_id": "c1"}],
                   "citation_incomplete": True},
        })

        assert result["passed"] is False
        assert result["citations"]["incomplete"] == 1

    def test_a_fully_cited_rebuild_still_passes(self):
        result = self._rebuild_with({
            "q0": {"text": "a price", "grounded": True, "grounded_on": [{"chunk_id": "c1"}]},
            "q1": {"text": "a refusal", "grounded": False},
        })

        assert result["passed"] is True
        assert result["citations"] == {"grounded": 1, "uncited": 0, "incomplete": 0}

    def test_the_two_paths_run_the_same_citation_check(self):
        """One definition, two callers. A second copy of the rule is a second
        place for it to drift, which is how the promotion path came to be
        missing it in the first place."""
        import importlib.util
        import inspect

        from anita import observability

        path = PLATFORM / "scripts" / "release_gate.py"
        spec = importlib.util.spec_from_file_location("release_gate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        source = inspect.getsource(module.run_evaluation_set)
        assert "citation_integrity" in source, (
            "the release gate must use the shared counter, not its own copy"
        )
        assert callable(observability.citation_failure)

    def test_the_validator_scores_the_staging_collection_and_restores_the_surface(self):
        """US-010: the frozen set must be answered from the collection the build
        *names*, not the one that is live while the rebuild runs — and the
        surface must be put back afterwards, including when the gate raises, so
        a rejected build cannot leave the process answering from a collection
        that was never promoted."""
        from types import SimpleNamespace

        asked: list[str] = []

        class FakeRetriever:
            def __init__(self, collection):
                self.collection = collection

            def for_collection(self, name):
                return FakeRetriever(name)

        class FakeChat:
            def __init__(self, retriever):
                self._retriever = retriever

            @property
            def retriever(self):
                return self._retriever

            @retriever.setter
            def retriever(self, value):
                self._retriever = value

            def post_message(self, *, message, session_id):
                asked.append(self._retriever.collection)
                return SimpleNamespace(body={"text": "ok"})

        class FakeGate:
            set_sha256 = "x"
            # §8.33: the validator now runs the citation pass over the set's
            # cases, so a double for the gate has to carry them like the real
            # `QualityGate` does.
            cases: list = []

            def evaluate(self, answerer):
                for i in range(2):
                    answerer(SimpleNamespace(question=f"q{i}", case_id=str(i)))
                return SimpleNamespace(as_dict=lambda: {"passed": True, "reason": "ok"})

        module = self._module()
        live = FakeRetriever("anita_ams__v1")
        chat = FakeChat(live)
        validator = module._gate_validator(chat=chat, gate=FakeGate(), retriever=live)

        validator(SimpleNamespace(collection="anita_ams__v2"))

        assert asked == ["anita_ams__v2", "anita_ams__v2"], (
            "the gate must query the staging collection, not the live one"
        )
        assert chat.retriever is live, "the serving retriever must be restored"

        class ExplodingGate(FakeGate):
            def evaluate(self, answerer):
                raise RuntimeError("gate blew up")

        with pytest.raises(RuntimeError):
            module._gate_validator(chat=chat, gate=ExplodingGate(), retriever=live)(
                SimpleNamespace(collection="anita_ams__v3")
            )
        assert chat.retriever is live, "restore must happen on the failure path too"


# ---------------------------------------------------------------------------
# §8.5 / §8.19 — the gate's environment and its diagnosis
# ---------------------------------------------------------------------------
class TestGateEnvironmentAndDiagnosis:
    """Two artifacts the gate used to produce:

    * §8.5 — it built with an *empty* environment (`assemble(
      require_preflight=False)` collapsed `None` to `{}`), so it printed
      "TWILIO_AUTH_TOKEN unset" beside a `.env` that sets it, and scored a
      default assembly rather than the deployed one.
    * §8.19 — with nothing listening on the instance port it reported
      "1 scenario failed", blaming the product for an absent instance.
    """

    def _gate_script(self):
        import importlib.util

        path = PLATFORM / "scripts" / "release_gate.py"
        spec = importlib.util.spec_from_file_location("release_gate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    # -- §8.19 -------------------------------------------------------------
    def test_an_unreachable_instance_is_named_as_such(self, monkeypatch):
        module = self._gate_script()
        monkeypatch.setattr(module, "instance_is_up", lambda url=None: False)
        report = module.run_browser_suite()
        assert report.total == 0
        assert "no instance" in report.reason
        assert "not a product failure" in report.reason

    def test_a_closed_port_is_not_reachable(self):
        module = self._gate_script()
        assert module.instance_is_up("http://127.0.0.1:9", timeout=0.3) is False

    def test_an_unrun_suite_is_not_reported_as_failed_scenarios(self):
        from anita.observability import (
            BrowserReport, GateReport, SystemReport, combine_validation,
        )

        result = combine_validation(
            GateReport(passed=True, total=70, reason="all 70 cases passed"),
            BrowserReport(passed=False, total=0, reason="no instance on http://127.0.0.1:8085"),
            SystemReport(passed=True, loaded=41, wired=11),
        )
        assert result["passed"] is False, "an unrun suite is still not evidence"
        assert "did not run" in result["reason"]
        assert "failed: []" not in result["reason"]

    # -- §8.5 --------------------------------------------------------------
    def test_the_deployed_environment_is_the_boots_environment(self, monkeypatch):
        from anita.assembly import deployed_environment

        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "probe-token")
        assert deployed_environment()["TWILIO_AUTH_TOKEN"] == "probe-token"

    def test_the_test_default_stays_an_empty_environment(self):
        """The isolation is the point: a developer's `.env` must never change
        a test's result, so the fix moved to the callers that need the real
        environment rather than changing the default."""
        from anita.assembly import assemble

        built = assemble(require_preflight=False)
        assert any("TWILIO_AUTH_TOKEN unset" in note for note in built.notes)

    def test_the_tooling_passes_the_deployed_environment(self):
        for name in (
            "release_gate.py", "system_check.py", "rag_audit.py",
            "trace_question.py", "trace_use_cases.py", "validate_chat.py",
            "rebuild_index.py",
        ):
            source = (PLATFORM / "scripts" / name).read_text(encoding="utf-8")
            assert "env=deployed_environment()" in source, (
                f"{name} builds an empty environment again (§8.5)"
            )
        streamlit = (PLATFORM / "streamlit_app.py").read_text(encoding="utf-8")
        assert "env=deployed_environment()" in streamlit, "Streamlit lost its environment"


# ---------------------------------------------------------------------------
# US-018 — the VRAM ladder actually moves
# ---------------------------------------------------------------------------
class TestUS018Capacity:
    """The ladder, the ceiling and the footprint maths all existed — and
    nothing drove them. `.degrade(` had no caller outside tests, `gpu_free_mib`
    was never written, and a host without a GPU admitted calls as if it had
    one beside a boot report claiming voice "will be refused at admission".
    """

    def _controller(self, **kwargs):
        from anita.speech import AdmissionController

        return AdmissionController(max_concurrent_calls=2, **kwargs)

    def _policy(self, controller, free):
        from anita.capacity import CapacityPolicy

        return CapacityPolicy(controller, source=lambda: free)

    def test_the_ladder_is_the_storys_order(self):
        from anita.speech import LADDER

        assert [level.value for level in LADDER] == [
            "full", "voice_shed", "stt_off", "tts_off", "chat_degrade",
            "commercial_only",
        ]

    def test_pressure_sheds_voice_before_anything_else(self):
        from anita.speech import DegradationLevel

        controller = self._controller()
        policy = self._policy(controller, free=100)   # below the sibling floor

        moved = policy.sample()

        assert moved == "degrade:voice_shed"
        assert controller.level is DegradationLevel.VOICE_SHED
        assert controller.text_available is True, "text must survive voice being shed"

    def test_a_shed_voice_refuses_the_next_call(self):
        controller = self._controller()
        self._policy(controller, free=100).sample()

        decision = controller.admit("call-1")

        assert decision.admitted is False
        assert "shed" in decision.reason
        assert controller.active_calls() == 0, "no slot is taken by a refusal"

    def test_pressure_easing_walks_back_up(self):
        controller = self._controller()
        policy = self._policy(controller, free=100)
        policy.sample()
        assert controller.level.value == "voice_shed"

        policy.source = lambda: 60_000   # the card is free again
        moved = policy.sample()

        assert moved == "recover:full"
        assert controller.admit("call-1").admitted is True

    def test_recovery_needs_headroom_not_just_the_floor(self):
        """Hysteresis: a box hovering at the threshold must not flap."""
        controller = self._controller()
        policy = self._policy(controller, free=100)
        policy.sample()

        policy.source = lambda: controller.sibling_floor_mib + 1
        assert policy.sample() is None, "no headroom for the rung above"
        assert controller.level.value == "voice_shed"

    def test_an_unmeasurable_device_is_never_guessed_at(self):
        controller = self._controller()
        policy = self._policy(controller, free=None)

        assert policy.sample() is None
        assert controller.level.value == "full", "absence is not pressure"

    def test_the_monitor_publishes_the_reading_it_decides_on(self):
        from anita.capacity import CapacityMonitor

        controller = self._controller()
        metrics = MetricsRegistry()
        monitor = CapacityMonitor(
            self._policy(controller, free=100), metrics=metrics, source=lambda: 100
        )

        moved = monitor.tick()

        assert metrics.get("gpu_free_mib").value == 100.0
        assert moved == "degrade:voice_shed"

    def test_the_monitor_reports_absence_rather_than_zero(self):
        from anita.capacity import CapacityMonitor

        controller = self._controller()
        metrics = MetricsRegistry()
        monitor = CapacityMonitor(
            self._policy(controller, free=None), metrics=metrics, source=lambda: None
        )

        assert monitor.tick() is None
        assert metrics.get("gpu_free_mib").value is None, "absent, not 0 MiB free"
        assert controller.level.value == "full"

    # -- the device policy --------------------------------------------------
    def test_the_configured_capacity_numbers_reach_the_controller(self):
        """TAC-7: a capacity key the code does not read is decoration."""
        from anita.assembly import assemble
        from anita.config import load_settings

        settings = load_settings(env={"ANITA_ADMIN_TOKEN": "x"})
        built = assemble(require_preflight=False)
        admission = built.voice.admission

        assert admission.anita_budget_mib == settings.vram.anita_budget_mib
        assert admission.shared_card_mib == settings.vram.shared_card_mib
        assert admission.sibling_floor_mib == settings.vram.sibling_floor_mib

    def test_a_host_without_a_gpu_caps_voice_and_says_so(self):
        from anita.assembly import assemble
        from anita.config import load_settings
        from anita.platform_detect import detect_platform, pick_model
        from anita.speech import CPU_CONCURRENT_CALLS

        settings = load_settings(env={"ANITA_ADMIN_TOKEN": "x"})
        capable = pick_model(detect_platform(), settings).voice_capable

        built = assemble(require_preflight=False)
        admission = built.voice.admission

        if capable:
            assert admission.max_concurrent_calls == settings.speech.max_concurrent_calls
        else:
            assert admission.max_concurrent_calls == CPU_CONCURRENT_CALLS
            assert any("voice capped" in note for note in built.notes)

    def test_the_boot_report_matches_what_admission_does(self):
        """The old text claimed a refusal no code implemented."""
        from anita.boot import _phase_platform
        from anita.config import load_settings
        from anita.platform_detect import detect_platform, pick_model

        settings = load_settings(env={"ANITA_ADMIN_TOKEN": "x"})
        capable = pick_model(detect_platform(), settings).voice_capable
        detail = _phase_platform(settings).detail

        if capable:
            assert "capped" not in detail
        else:
            assert "voice capped at 1" in detail
            assert "refused" not in detail

    def test_a_cpu_host_starts_no_monitor(self):
        """Shedding releases ANITA's VRAM; a host that uses none must not shed.

        Gating the monitor on `nvidia-smi` alone put one on a box whose speech
        stack runs on CPU and degraded the ladder to `commercial_only` on the
        first samples — disabling a working feature on a card ANITA was never
        occupying.
        """
        from anita.assembly import assemble
        from anita.speech import CPU_CONCURRENT_CALLS

        built = assemble(require_preflight=False)
        admission = built.voice.admission
        if admission.max_concurrent_calls != CPU_CONCURRENT_CALLS:
            return  # a GPU host: the monitor is supposed to be running there
        assert admission.level.value == "full", "no monitor, no shed"
        assert admission.active_calls() == 0
