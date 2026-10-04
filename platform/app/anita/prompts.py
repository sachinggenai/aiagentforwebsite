"""Prompt assembly and the numeric-grounding check (TRD-20).

Two jobs, and the second is the reason this module exists.

**Assembly** builds the prompt from a fixed budget so a long conversation
cannot push the grounding rules out of the window — the rules are the part that
must never be truncated, so they are placed first and the history is what gets
cut.

**The numeric-grounding check** is the last line of defence against a
fabricated figure. A prompt that says "only use the context" is an instruction a
model can drift from; this is a *check* on the output, applied after generation
and before anything is said to a buyer. It works on one narrow, high-value
signal: **every number in the answer must appear in the context.**

That catches the failure that actually costs money — a plausible-looking
₹2,50,000 where the catalogue says ₹25,000 — without trying to verify prose,
which is not reliably checkable and would produce false alarms until people
learned to ignore it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

# Character budgets. The context budget leaves room inside an 8,192-token window
# for the rules, the question and the answer after a 14B model's own overhead.
CONTEXT_BUDGET_CHARS = 4000
HISTORY_BUDGET_CHARS = 1200
ANSWER_BUDGET_CHARS = 800
MAX_CONTEXT_TOKENS = 8192

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
GROUNDING_RULES = """RULES — these override everything else:
1. Answer ONLY from the CONTEXT below. If the context does not contain the
   answer, say you don't have that information and offer to have the team
   confirm it. Never answer from your own knowledge.
2. Never invent, round, convert or estimate a price, quantity, weight,
   dimension or date. State figures exactly as they appear in the context.
3. Every price you state is a published list price and must be accompanied by
   the note that a formal quotation is required. Omitting that note is an error.
4. If the context is marked as not published for something, say so plainly.
   Do not infer a value from a similar product.
5. If two products share a name, ask which one is meant rather than choosing.
6. You are a product and catalogue assistant. You do not give medical or
   clinical advice. If asked for any, decline and offer to connect a person.
7. When the context cites a source, prefer the most specific one for the
   question asked."""

VOICE_STYLE = """STYLE — you are speaking aloud on a phone call:
- Two or three short sentences. No lists, no markdown, no headings.
- Never read out a URL or a product id.
- Say figures the way a person would: "about fourteen and a half lakh rupees".
- If you must decline, do it in one sentence and offer the alternative."""

CHAT_STYLE = """STYLE — you are answering in a chat window:
- Short paragraphs. Plain text, no markdown headings.
- You may use a short bullet list only for comparing two products.
- State figures exactly as published, with the quotation note."""


def system_prompt(mode: str = "chat", *, company: str = "ANITA MEDICAL SYSTEMS PVT. LTD.") -> str:
    """The fixed instruction block. Placed first so it is never truncated."""
    style = VOICE_STYLE if mode == "voice" else CHAT_STYLE
    return f"You are the product assistant for {company}.\n\n{GROUNDING_RULES}\n\n{style}"


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
@dataclass
class PromptBundle:
    """The assembled prompt plus what went into it.

    The budget report is not decoration: when an answer is poor, the first
    question is whether the context was truncated, and guessing wastes a lot of
    time.
    """

    system: str
    user: str
    context_chars: int = 0
    history_chars: int = 0
    question_chars: int = 0
    context_truncated: bool = False
    history_truncated: bool = False
    chunks_included: int = 0

    def messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.user},
        ]

    def budget_report(self) -> dict:
        return {
            "context_chars": self.context_chars,
            "history_chars": self.history_chars,
            "question_chars": self.question_chars,
            "context_truncated": self.context_truncated,
            "history_truncated": self.history_truncated,
            "chunks_included": self.chunks_included,
        }


def _budget(text: str, limit: int) -> tuple[str, bool]:
    """Trim to a limit on a sentence boundary where one exists."""
    if len(text) <= limit:
        return text, False
    clipped = text[:limit]
    cut = max(clipped.rfind(". "), clipped.rfind("\n"))
    if cut > limit * 0.6:
        clipped = clipped[: cut + 1]
    return clipped.rstrip() + "\n[context truncated]", True


def build_prompt(
    question: str,
    context: str,
    *,
    mode: str = "chat",
    history: Sequence[dict[str, str]] = (),
    company: str = "ANITA MEDICAL SYSTEMS PVT. LTD.",
    context_budget: int = CONTEXT_BUDGET_CHARS,
    history_budget: int = HISTORY_BUDGET_CHARS,
) -> PromptBundle:
    """Assemble the prompt, truncating history before context.

    Order matters. The grounding rules are fixed and never cut. Context is cut
    only after history, because context is what the answer must come from —
    losing history makes the assistant forget the conversation, losing context
    makes it unable to answer at all. The lesser failure goes first.
    """
    trimmed_context, context_cut = _budget(context or "", context_budget)

    history_text = ""
    history_cut = False
    if history:
        # Most recent turns matter most, so build backwards and keep what fits.
        kept: list[str] = []
        used = 0
        for turn in reversed(list(history)):
            line = f"{turn.get('role', 'user')}: {turn.get('content', '')}"
            if used + len(line) > history_budget:
                history_cut = True
                break
            kept.append(line)
            used += len(line) + 1
        history_text = "\n".join(reversed(kept))

    parts = [f"CONTEXT:\n{trimmed_context}" if trimmed_context else "CONTEXT:\n(none)"]
    if history_text:
        parts.append(f"CONVERSATION SO FAR:\n{history_text}")
    parts.append(f"QUESTION:\n{question}")

    user = "\n\n".join(parts)
    return PromptBundle(
        system=system_prompt(mode, company=company),
        user=user,
        context_chars=len(trimmed_context),
        history_chars=len(history_text),
        question_chars=len(question),
        context_truncated=context_cut,
        history_truncated=history_cut,
        chunks_included=trimmed_context.count("[§ "),
    )


def build_decline_prompt(question: str, *, mode: str = "chat") -> PromptBundle:
    """The prompt used when retrieval found nothing.

    There is no context block at all — not an empty one. An empty `CONTEXT:`
    section invites the model to fill the gap, which is the behaviour the gate
    exists to prevent.
    """
    user = (
        "The catalogue has no relevant information for this question, so you "
        "must not answer it from your own knowledge.\n\n"
        f"QUESTION:\n{question}\n\n"
        "Reply in one or two sentences: say you don't have that information, "
        "and offer to have the team follow up."
    )
    return PromptBundle(system=system_prompt(mode), user=user, question_chars=len(question))


# ---------------------------------------------------------------------------
# Numeric grounding check
# ---------------------------------------------------------------------------
# Numbers as they appear in an answer: 60, 2,500, 14,50,000, 1450000, 2-3.5, 7
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Numbers a model may legitimately produce that are not catalogue facts:
# small counts in prose, ordinals, and the "one or two sentences" register.
_TRIVIAL = frozenset({"1", "2", "3", "4", "5", "10", "100", "24"})


def _normalise(number: str) -> str:
    """Strip grouping separators and trailing zeros after a decimal point.

    `14,50,000` and `1450000` are the same figure written two ways, and the
    model may echo either back. Comparing raw strings would flag a correct
    answer as a violation, which is how a check gets switched off.
    """
    value = number.replace(",", "")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value or "0"


def extract_numbers(text: str) -> list[str]:
    return [match.group(0) for match in _NUMBER_RE.finditer(text or "")]


@dataclass
class GroundingCheck:
    """Result of verifying an answer against the context it was given."""

    grounded: bool
    violations: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.grounded

    def as_dict(self) -> dict:
        return {
            "grounded": self.grounded,
            "violations": self.violations,
            "checked": self.checked,
        }


def check_numeric_grounding(
    answer: str,
    context: str,
    *,
    question: str = "",
    ignore: Sequence[str] = (),
) -> GroundingCheck:
    """Verify every number in the answer appears in the context.

    This is a deliberately narrow check. It cannot tell whether prose is
    faithful — that is not reliably checkable, and a checker that cries wolf is
    one people learn to override. It *can* tell whether a figure was invented,
    and an invented figure is the failure that loses a sale or creates a
    liability.

    Numbers present in the question are permitted: a buyer who asks "what is the
    MOQ for 500 units" has introduced 500, and echoing it is not fabrication.
    Everything else must be traceable to the context.
    """
    context_numbers = {_normalise(n) for n in extract_numbers(context)}
    question_numbers = {_normalise(n) for n in extract_numbers(question)}
    allowed = context_numbers | question_numbers | {_normalise(n) for n in ignore}

    violations: list[str] = []
    checked: list[str] = []
    for raw in extract_numbers(answer):
        normalised = _normalise(raw)
        checked.append(raw)
        if normalised in allowed or raw in _TRIVIAL or normalised in _TRIVIAL:
            continue
        violations.append(raw)

    return GroundingCheck(grounded=not violations, violations=violations, checked=checked)


# What it looks like when an answer states a price. Matching only the word
# "price" was too narrow — a model asked about cost may answer "listed at
# ₹60/Piece" or "it costs 60 rupees" and never use the word at all, and the
# caveat would then go unchecked on exactly the answers that need it.
_PRICE_SIGNAL_RE = re.compile(
    r"(?:₹|\bINR\b|\bRs\.?\b|\brupees\b|\bprice\b|\bpriced\b|\bcost\b|\bcosts\b"
    r"|\blisted at\b|\bquoted at\b)",
    re.IGNORECASE,
)


def states_a_price(answer: str) -> bool:
    """True when the answer mentions a figure in a commercial way."""
    return bool(_PRICE_SIGNAL_RE.search(answer or ""))


def verify_answer(
    answer: str,
    context: str,
    *,
    question: str = "",
    require_caveat: bool = False,
    caveat_marker: str = "quotation",
) -> GroundingCheck:
    """Numeric grounding plus the price-caveat requirement.

    `BRD-06` makes the caveat mandatory whenever a price is stated. Checking it
    here, on the output, means the guarantee holds even if the prompt drifts —
    the prompt asks, this verifies.
    """
    check = check_numeric_grounding(answer, context, question=question)

    if require_caveat and states_a_price(answer):
        if caveat_marker.lower() not in (answer or "").lower():
            check.violations.append("missing price caveat")
            check.grounded = False

    return check
