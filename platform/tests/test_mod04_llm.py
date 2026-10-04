"""MOD-04 tests — prompt assembly, numeric grounding, streaming, degradation.

The numeric-grounding check is the highest-value thing here: it is the last
defence between a fabricated figure and a buyer. A prompt saying "only use the
context" is an instruction; this is a check on the output.

Run:  .venv\\Scripts\\python -m pytest tests -q
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "app"))

from anita.llm import (  # noqa: E402
    BreakState,
    CircuitBreaker,
    ClauseCutter,
    LLMError,
    OllamaBackend,
    select_backend,
)
from anita.prompts import (  # noqa: E402
    CONTEXT_BUDGET_CHARS,
    build_decline_prompt,
    build_prompt,
    check_numeric_grounding,
    extract_numbers,
    system_prompt,
    verify_answer,
)

CONTEXT = """[§ Ultrasound Gel]
Price: ₹60/Piece (MRP)
Minimum Order Quantity: 10 Pieces
Delivery Time: 7 Days
Weight: 150 ml"""


# ---------------------------------------------------------------------------
# Numeric grounding — the fabrication guard
# ---------------------------------------------------------------------------
class TestNumericGrounding:
    def test_grounded_answer_passes(self):
        check = check_numeric_grounding(
            "The Ultrasound Gel is listed at ₹60 per Piece, with a minimum order of 10 Pieces.",
            CONTEXT,
        )
        assert check.ok, check.violations

    def test_a_fabricated_price_is_caught(self):
        """The failure that costs money: a plausible figure that is not in the
        catalogue."""
        check = check_numeric_grounding(
            "The Ultrasound Gel is listed at ₹2,500 per Piece.", CONTEXT
        )
        assert not check.ok
        assert "2,500" in check.violations

    def test_a_figure_absent_from_context_is_caught(self):
        check = check_numeric_grounding("Delivery takes about 12 days.", CONTEXT)
        assert not check.ok
        assert "12" in check.violations

    def test_the_check_is_numeric_not_semantic(self):
        """A documented limitation, asserted so it cannot be mistaken for
        coverage it does not provide.

        "Delivery takes about 10 days" passes below, because 10 *is* in the
        context — as the minimum order quantity, not the delivery time. The
        check verifies that figures exist in the context, not that they are
        bound to the right attribute. Semantic binding is the prompt's job; this
        is the backstop for invention, and it is honest about being only that.
        """
        check = check_numeric_grounding("Delivery takes about 10 days.", CONTEXT)
        assert check.ok, "10 appears in the context (as MOQ) — this passes by design"

    def test_indian_and_plain_formatting_are_the_same_number(self):
        """`14,50,000` and `1450000` are the same figure written two ways; a
        check that flags a correct answer is a check people switch off."""
        context = "[§ FCR] Price: ₹14,50,000/Piece"
        assert check_numeric_grounding("It is listed at 1450000 rupees.", context).ok
        assert check_numeric_grounding("It is listed at ₹14,50,000.", context).ok

    def test_decimal_trailing_zeros_normalise(self):
        context = "[§ X] Weight: 2.50 Kilograms"
        assert check_numeric_grounding("It weighs 2.5 kg.", context).ok

    def test_numbers_from_the_question_are_allowed(self):
        """A buyer who asks about '500 units' introduced 500; echoing it is not
        fabrication."""
        check = check_numeric_grounding(
            "For 500 units, please request a quotation.",
            CONTEXT,
            question="What is the price for 500 units?",
        )
        assert check.ok, check.violations

    def test_trivial_counts_are_not_violations(self):
        check = check_numeric_grounding("We supply 3 product lines.", CONTEXT)
        assert check.ok, check.violations

    def test_empty_answer_is_grounded(self):
        assert check_numeric_grounding("", CONTEXT).ok

    def test_extract_numbers_handles_grouping(self):
        assert "14,50,000" in extract_numbers("₹14,50,000 per unit")
        assert "2" in extract_numbers("2 pieces")

    def test_violations_are_reported_with_their_original_form(self):
        """Reporting the form the model used makes the log actionable."""
        check = check_numeric_grounding("It costs ₹9,999.", CONTEXT)
        assert "9,999" in check.violations


class TestCaveatVerification:
    def test_missing_caveat_is_a_violation(self):
        """BRD-06: a price without the quotation note is a defect."""
        check = verify_answer(
            "The Ultrasound Gel is listed at ₹60 per Piece.",
            CONTEXT,
            require_caveat=True,
        )
        assert not check.ok
        assert "missing price caveat" in check.violations

    def test_present_caveat_passes(self):
        check = verify_answer(
            "The Ultrasound Gel is listed at ₹60 per Piece. A formal quotation is required.",
            CONTEXT,
            require_caveat=True,
        )
        assert check.ok, check.violations

    def test_caveat_not_required_when_no_price_is_stated(self):
        check = verify_answer("The MOQ is 10 Pieces.", CONTEXT, require_caveat=True)
        assert check.ok

    def test_both_failures_are_reported_together(self):
        check = verify_answer(
            "It costs ₹2,500 per Piece.", CONTEXT, require_caveat=True
        )
        assert "2,500" in check.violations
        assert "missing price caveat" in check.violations


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------
class TestPromptAssembly:
    def test_system_prompt_leads_with_the_rules(self):
        prompt = system_prompt("chat")
        assert "RULES" in prompt
        assert "Answer ONLY from the CONTEXT" in prompt
        assert prompt.index("RULES") < prompt.index("STYLE")

    def test_voice_and_chat_styles_differ(self):
        assert "speaking aloud" in system_prompt("voice")
        assert "chat window" in system_prompt("chat")

    def test_context_and_question_are_both_present(self):
        bundle = build_prompt("What is the MOQ?", CONTEXT)
        assert "10 Pieces" in bundle.user
        assert "What is the MOQ?" in bundle.user

    def test_context_is_truncated_when_oversized(self):
        huge = "[§ X]\n" + ("filler sentence. " * 1000)
        bundle = build_prompt("q", huge, context_budget=500)
        assert bundle.context_truncated
        assert bundle.context_chars <= 600

    def test_history_is_truncated_before_context(self):
        """Losing history makes the assistant forget; losing context makes it
        unable to answer. The lesser failure goes first."""
        huge_history = [{"role": "user", "content": "x" * 400} for _ in range(20)]
        bundle = build_prompt("q", CONTEXT, history=huge_history, history_budget=300)
        assert bundle.history_truncated
        assert not bundle.context_truncated
        assert "10 Pieces" in bundle.user   # context survived intact

    def test_history_keeps_the_most_recent_turns(self):
        history = [
            {"role": "user", "content": "oldest"},
            {"role": "assistant", "content": "middle"},
            {"role": "user", "content": "newest"},
        ]
        bundle = build_prompt("q", CONTEXT, history=history)
        assert "newest" in bundle.user
        assert bundle.user.index("newest") > bundle.user.index("middle")

    def test_decline_prompt_has_no_context_block(self):
        """Not an empty CONTEXT: section — an empty one invites the model to
        fill the gap, which is what the gate exists to prevent."""
        bundle = build_decline_prompt("What is the weather?")
        assert "CONTEXT" not in bundle.user
        assert "must not answer" in bundle.user

    def test_budget_report_is_accurate(self):
        bundle = build_prompt("q", CONTEXT)
        report = bundle.budget_report()
        assert report["context_chars"] > 0
        assert report["chunks_included"] == 1

    def test_messages_shape(self):
        messages = build_prompt("q", CONTEXT).messages()
        assert [m["role"] for m in messages] == ["system", "user"]


# ---------------------------------------------------------------------------
# Clause cutting (streaming → speech)
# ---------------------------------------------------------------------------
class TestClauseCutter:
    def test_emits_at_sentence_boundaries(self):
        cutter = ClauseCutter()
        clauses = cutter.feed("The gel costs 60 rupees. It ships in ")
        assert clauses == ["The gel costs 60 rupees."]

    def test_holds_an_incomplete_sentence(self):
        cutter = ClauseCutter()
        assert cutter.feed("The gel costs") == []

    def test_flush_drains_the_tail(self):
        cutter = ClauseCutter()
        cutter.feed("The gel costs 60 rupees")
        assert cutter.flush() == "The gel costs 60 rupees"

    def test_long_text_without_a_full_stop_still_emits(self):
        """A model that produces one long run would otherwise buffer the whole
        answer and defeat streaming entirely."""
        cutter = ClauseCutter(max_chars=50)
        clauses = cutter.feed("word " * 40)
        assert clauses, "must emit rather than hold forever"

    def test_multiple_sentences_in_one_feed(self):
        cutter = ClauseCutter()
        clauses = cutter.feed("One. Two. Three.")
        assert len(clauses) >= 2

    def test_empty_feed_is_safe(self):
        assert ClauseCutter().feed("") == []

    def test_due_reports_a_waiting_buffer(self):
        cutter = ClauseCutter(flush_ms=0)
        cutter.feed("held text")
        assert cutter.due()

    def test_not_due_immediately_after_emitting(self):
        cutter = ClauseCutter(flush_ms=10_000)
        cutter.feed("Something. ")
        assert not cutter.due()


# ---------------------------------------------------------------------------
# Circuit breaker and degradation (DG-03)
# ---------------------------------------------------------------------------
class TestCircuitBreaker:
    def test_starts_closed(self):
        assert CircuitBreaker().state is BreakState.CLOSED
        assert CircuitBreaker().allow()

    def test_opens_after_three_consecutive_failures(self):
        breaker = CircuitBreaker(threshold=3)
        for _ in range(3):
            breaker.record_failure()
        assert breaker.state is BreakState.OPEN
        assert not breaker.allow()

    def test_a_success_resets_the_count(self):
        """Flaky is not down. Opening on a fail/succeed/fail pattern would shed
        traffic that works."""
        breaker = CircuitBreaker(threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        assert breaker.state is BreakState.CLOSED

    def test_half_open_after_cooldown_admits_one_probe(self):
        breaker = CircuitBreaker(threshold=1, cooldown_s=0.01)
        breaker.record_failure()
        time.sleep(0.02)
        assert breaker.state is BreakState.HALF_OPEN
        assert breaker.allow() is True
        assert breaker.allow() is False   # only one probe at a time

    def test_snapshot_reports_state(self):
        snapshot = CircuitBreaker().snapshot()
        assert snapshot["state"] == "closed"

    def test_open_breaker_refuses_generation(self):
        backend = OllamaBackend("http://fake", "m", breaker=CircuitBreaker(threshold=1))
        backend.breaker.record_failure()
        with pytest.raises(LLMError, match="unavailable"):
            backend.generate([{"role": "user", "content": "hi"}])


class TestBackendSelection:
    def test_cuda_selects_ollama(self):
        assert select_backend("cuda") == "ollama"

    def test_metal_selects_mlx(self):
        assert select_backend("metal") == "mlx"

    def test_cpu_selects_ollama(self):
        assert select_backend("cpu") == "ollama"

    def test_explicit_provider_overrides_detection(self):
        assert select_backend("cuda", provider="mlx") == "mlx"

    def test_unknown_provider_is_refused(self):
        with pytest.raises(ValueError):
            select_backend("cuda", provider="gpt-5")


class TestDegradationContract:
    def test_the_deterministic_path_has_no_llm_dependency(self):
        """DG-03: when the model is down, commercial answers must still work.

        Asserted structurally — if `answers.py` ever imports the LLM module,
        this fails and the degradation guarantee is visibly broken.
        """
        import ast

        tree = ast.parse((PLATFORM / "app" / "anita" / "answers.py").read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        assert not [m for m in imported if "llm" in m.lower() or "prompt" in m.lower()]

    def test_backend_describe_exposes_the_breaker(self):
        described = OllamaBackend("http://fake", "qwen2.5:14b").describe()
        assert described["breaker"]["state"] == "closed"
        assert described["num_ctx"] == 8192
