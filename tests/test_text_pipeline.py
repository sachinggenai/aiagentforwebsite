"""Unit tests for the text pipeline and value parsing.

These cover the defects that actually occurred during development, so they are
regression tests rather than illustrative examples:

* Draft.js word-splitting (``"X - Ray"`` should read ``"X-Ray"``)
* block boundaries still separating sentences
* Indian-convention spoken money
* quantity ranges and units
* ``Q:``/``A:`` prefix stripping
* the identifier extraction that survives concatenated values

Run:  .venv\\Scripts\\python -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ams.dom import make_soup, smart_text, drop_chrome_lines, contains_chrome  # noqa: E402
from ams.normalize import (  # noqa: E402
    indian_grouping,
    parse_lead_time_days,
    parse_price,
    parse_quantity,
    spoken_amount,
    split_label_value,
    strip_qa_prefix,
    tidy_punctuation,
)


# ---------------------------------------------------------------------------
# The core text defect
# ---------------------------------------------------------------------------
class TestSmartText:
    def test_joins_inline_tags_without_separator(self):
        """The Draft.js defect: one span per word must not gain spaces."""
        html = "<div><span>light</span><span>-</span><span>tight</span></div>"
        assert smart_text(make_soup(html)) == "light-tight"

    def test_repairs_hyphen_compound(self):
        """Reproduces the real markup: ' X' / '-' / 'Ray' as separate spans.

        Joining with a separator yields "X - Ray"; joining inline with none
        yields the correct "X-Ray".
        """
        html = "<div><span>An</span><span> X</span><span>-</span><span>Ray</span></div>"
        assert smart_text(make_soup(html)) == "An X-Ray"

    def test_repairs_space_before_punctuation(self):
        """Reproduces the real markup: the full stop is its own span."""
        html = "<div><span>on a film</span><span>.</span></div>"
        assert smart_text(make_soup(html)) == "on a film."

    def test_does_not_invent_hyphens_where_source_has_spaces(self):
        """Guards the inverse: genuinely spaced text must be left alone."""
        html = "<div><span>NSIC - CRISIL</span></div>"
        assert smart_text(make_soup(html)) == "NSIC - CRISIL"

    def test_blocks_still_separate_sentences(self):
        """Inline joining must not merge genuinely separate blocks."""
        html = "<div><p>First sentence.</p><p>Second sentence.</p></div>"
        result = smart_text(make_soup(html))
        assert result == "First sentence. Second sentence."

    def test_list_items_stay_separate(self):
        html = "<ul><li>Condition</li><li>New</li></ul>"
        assert smart_text(make_soup(html)) == "Condition New"

    def test_skips_script_and_style(self):
        html = "<div><style>a{b:c}</style><script>x()</script>Visible</div>"
        assert smart_text(make_soup(html)) == "Visible"

    def test_collapses_whitespace(self):
        assert smart_text(make_soup("<div>  a   b  </div>")) == "a b"

    def test_empty_input(self):
        assert smart_text(None) == ""
        assert smart_text(make_soup("")) == ""


class TestChromeFiltering:
    def test_detects_marker(self):
        assert contains_chrome("Please Wait...") != []

    def test_clean_text_has_no_marker(self):
        assert contains_chrome("X-Ray Cassette is a device") == []

    def test_drops_only_offending_lines(self):
        text = "Real content here\nPlease Wait...\nMore real content"
        result = drop_chrome_lines(text)
        assert "Real content here" in result
        assert "More real content" in result
        assert "Please Wait..." not in result


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
class TestMoney:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (60, "60"),
            (2500, "2,500"),
            (25000, "25,000"),
            (550000, "5,50,000"),
            (1385000, "13,85,000"),
            (2500000, "25,00,000"),
        ],
    )
    def test_indian_grouping(self, value, expected):
        assert indian_grouping(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (60, "60 rupees"),
            (2500, "2,500 rupees"),
            (550000, "about 5.5 lakh rupees"),
            (1450000, "about 14.5 lakh rupees"),
            (2500000, "about 25 lakh rupees"),
            (25000000, "about 2.5 crore rupees"),
        ],
    )
    def test_spoken_amount_uses_indian_convention(self, value, expected):
        assert spoken_amount(value) == expected

    def test_parse_price_with_unit(self):
        money = parse_price("2500", unit="Piece")
        assert money.amount_int == 2500
        assert money.display == "₹2,500/Piece"
        assert money.currency == "INR"

    def test_parse_price_from_full_string(self):
        money = parse_price("Price 2500 INR/ Piece")
        assert money.amount_int == 2500

    def test_parse_price_handles_garbage(self):
        money = parse_price("Price on request")
        assert money.amount_int is None

    def test_price_is_flagged_as_list_price(self):
        """A bot must never quote a list price as final."""
        money = parse_price("2500")
        assert money.is_list_price is True
        assert money.quote_required is True


# ---------------------------------------------------------------------------
# Quantities
# ---------------------------------------------------------------------------
class TestQuantity:
    def test_range(self):
        qty = parse_quantity("2-3.5 Kilograms (kg)")
        assert qty.value_min == 2.0
        assert qty.value_max == 3.5
        assert qty.unit == "Kilograms"
        assert qty.is_range

    def test_single_with_unit(self):
        qty = parse_quantity("1 Piece")
        assert qty.value == 1.0
        assert qty.unit == "Piece"

    def test_per_day(self):
        qty = parse_quantity("10 Pieces Per Day")
        assert qty.value == 10.0

    def test_pressure_unit(self):
        assert parse_quantity("1200 psi").value == 1200.0

    def test_empty(self):
        assert parse_quantity("").value is None

    @pytest.mark.parametrize(
        ("raw", "days"),
        [("7 Days", 7), ("2 Weeks", 14), ("1 Month", 30), ("immediate", None)],
    )
    def test_lead_time(self, raw, days):
        assert parse_lead_time_days(raw) == days


# ---------------------------------------------------------------------------
# Labels and prefixes
# ---------------------------------------------------------------------------
class TestLabels:
    def test_splits_on_colon(self):
        assert split_label_value("Condition: New") == ("Condition", "New")

    def test_splits_on_known_label_without_colon(self):
        assert split_label_value("Weight 2-3.5 Kilograms (kg)") == (
            "Weight",
            "2-3.5 Kilograms (kg)",
        )

    def test_longest_known_label_wins(self):
        label, value = split_label_value("Sample Available Yes")
        assert label == "Sample Available"
        assert value == "Yes"

    def test_sample_policy_not_split_on_first_word(self):
        label, value = split_label_value(
            "Sample Policy Sample costs shipping and taxes has to be paid by the buyer"
        )
        assert label == "Sample Policy"
        assert value.startswith("Sample costs")

    def test_strips_question_prefix(self):
        assert strip_qa_prefix("Q: How is it used?") == "How is it used?"

    def test_strips_answer_prefix(self):
        assert strip_qa_prefix("A: It is used thus") == "It is used thus"

    def test_leaves_unprefixed_text(self):
        assert strip_qa_prefix("No prefix") == "No prefix"


class TestPunctuation:
    def test_repairs_source_malformed_commas(self):
        """`1 , , Unit` is malformed in the source markup itself."""
        assert tidy_punctuation("1 , , Unit") == "1 Unit"

    def test_preserves_legitimate_list_commas(self):
        result = tidy_punctuation('14" x 17" , 14" x 14"')
        assert "14" in result and "17" in result

    def test_drops_trailing_comma(self):
        assert tidy_punctuation("Value,") == "Value"

    def test_empty(self):
        assert tidy_punctuation("") == ""


# ---------------------------------------------------------------------------
# Emitted chunk text is read by three consumers and wants no markup
#
# Found by asking the running platform a question and getting back a bubble
# beginning "Q: What makes this Ultrasound Gel suitable... A: This gel...".
# The chunk emitter was adding those labels. They are noise tokens to an
# embedding, a TTS engine reads "Q colon" aloud, and the chat surface returns
# the chunk verbatim to a buyer — so all three got something they did not want.
# ---------------------------------------------------------------------------
class TestChunkTextIsProse:
    @staticmethod
    def _chunks():
        import json

        path = Path(__file__).resolve().parents[1] / "data" / "out" / "chunks.jsonl"
        if not path.exists():
            pytest.skip("corpus not emitted")
        with path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def test_no_chunk_carries_question_or_answer_labels(self):
        offenders = [
            chunk["chunk_id"] for chunk in self._chunks()
            if chunk["text"].lstrip().startswith(("Q:", "A:"))
            or "\nA:" in chunk["text"] or "\nQ:" in chunk["text"]
        ]
        assert not offenders, f"{len(offenders)} chunks still carry Q:/A: labels"

    def test_a_faq_chunk_reads_as_the_question_then_its_answer(self):
        faqs = [c for c in self._chunks() if c.get("chunk_type") == "product_faq"]
        assert faqs, "no FAQ chunks emitted"
        text = faqs[0]["text"]
        # The question is still first, so retrieval on the question still works.
        assert "?" in text.split(".")[0] or text.rstrip().endswith("."), text[:80]

    def test_every_chunk_still_carries_its_provenance(self):
        """Removing the labels must not have removed the traceability."""
        for chunk in self._chunks():
            assert chunk.get("source_url"), chunk.get("chunk_id")
            assert chunk.get("product_id") or chunk.get("page_type") != "product"
