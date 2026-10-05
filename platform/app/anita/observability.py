"""Observability and the release gate (MOD-08, TRD-36 … TRD-40).

Two halves:

**Tracing** — per-turn records of what happened, written best-effort. The
governing property is that **if this module is deleted entirely, every
conversation still works.** Telemetry that can fail a call is worse than no
telemetry: it converts an observability outage into a product outage. So the
writer swallows every exception and is dependency-free by construction.

**The quality gate** — a frozen set of questions with known-correct answers,
run against a candidate index or configuration. `SM-05` will not promote a build
whose gate fails, which is what makes "a bad catalogue refresh cannot reach a
buyer" a mechanism rather than an intention.

The gate reports **absent, never zero.** A metric nobody measured and a metric
that measured zero are different facts, and conflating them is how a dashboard
says "all clear" during an outage.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

logger = logging.getLogger("anita.observability")

# Thresholds the gate enforces (BRD-22). Named rather than inlined so a change
# is a visible decision.
MIN_GROUNDED_RATE = 0.98
MAX_REGRESSION = 0.02


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------
#: Trace log rotation (TAC-5). 100 MB per file, 7 retained.
ROTATE_BYTES = 100 * 1024 * 1024
KEEP_FILES = 7


@dataclass
class TurnTrace:
    """One conversation turn, as measured.

    Stages are recorded by `mark()` as the turn runs and turned into durations
    at write time. **A stage that did not run is absent, never zero** (TAC-3):
    `durations_ms` only ever contains intervals between marks that actually
    happened, so a reader can tell a short turn from a broken one.

    Every method swallows its own exceptions. Telemetry that can fail a turn is
    worse than no telemetry (TAC-1/TAC-4), so nothing here raises into a caller.
    """

    trace_id: str
    conversation_id: str = ""
    channel: str = ""
    turn_no: int = 0
    intent: str = ""
    stage: str = "complete"
    retrieved_chunks: int = 0
    grounded: bool | None = None
    total_ms: float | None = None
    first_token_ms: float | None = None
    degraded: str = ""
    failed: bool = False
    error: str = ""
    outcome: str = ""
    admission_refused: bool = False
    #: The sentiment category, set from a closed enum by the caller — never
    #: from buyer text, which is why it is a typed field rather than a counter
    #: (`note()` refuses strings, and that refusal is the PII guard).
    sentiment_category: str = ""
    #: Stage → monotonic timestamp. Private: never serialised directly; the
    #: written form is `stages_seen` plus `durations_ms`.
    _marks: dict[str, float] = field(default_factory=dict, repr=False)
    #: Numeric counters only — `note()` drops anything else, so a buyer's name
    #: or transcript text cannot enter a trace through the counter path (TAC-4).
    _counters: dict[str, float | int | bool] = field(default_factory=dict, repr=False)
    dropped_marks: int = 0
    clock_skew: bool = False
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # -- recording ---------------------------------------------------------
    def mark(self, stage: str, **counters) -> None:
        """Record a stage boundary. First mark for a stage wins; a repeat is
        counted in `dropped_marks` rather than overwriting the timestamp."""
        try:
            if stage in self._marks:
                self.dropped_marks += 1
                return
            self._marks[stage] = time.monotonic()
            if counters:
                self.note(**counters)
        except Exception:  # noqa: BLE001 - a trace must never fail a turn
            pass

    def note(self, **counters) -> None:
        """Attach counters to the turn. Non-numeric values are dropped, not
        stored — the refusal is the mechanism that keeps PII out (TAC-4)."""
        try:
            for key, value in counters.items():
                if isinstance(value, (int, float, bool)):
                    self._counters[key] = value
                else:
                    self.dropped_marks += 1
        except Exception:  # noqa: BLE001
            pass

    # -- serialisation -----------------------------------------------------
    @property
    def stages_seen(self) -> list[str]:
        """Stages in the order they were marked. Always present, so a reader
        can tell a short turn from a broken one (TAC-3)."""
        return list(self._marks)

    def durations_ms(self) -> dict[str, int]:
        """Milliseconds between consecutive *present* marks, keyed by the stage
        that closed the interval. A missing stage has no key at all."""
        out: dict[str, int] = {}
        previous: tuple[str, float] | None = None
        for stage, stamp in self._marks.items():
            if previous is not None:
                delta = stamp - previous[1]
                if delta < 0:
                    # A clock jump backwards. Clamp and flag rather than write
                    # a negative latency a reader would have to disbelieve.
                    self.clock_skew = True
                    delta = 0.0
                out[stage] = int(round(delta * 1000))
            previous = (stage, stamp)
        return out

    def as_dict(self) -> dict:
        stamps = list(self._marks.values())
        total = self.total_ms
        if total is None and len(stamps) >= 2:
            total = round((stamps[-1] - stamps[0]) * 1000, 1)
        return {
            "trace_id": self.trace_id,
            "conversation_id": self.conversation_id,
            "channel": self.channel,
            "turn_no": self.turn_no,
            "intent": self.intent,
            "stage": self.stage,
            "stages_seen": self.stages_seen,
            "durations_ms": self.durations_ms(),
            "counters": dict(self._counters),
            "retrieved_chunks": self.retrieved_chunks,
            "grounded": self.grounded,
            "total_ms": total,
            "first_token_ms": self.first_token_ms,
            "degraded": self.degraded,
            "failed": self.failed,
            "error": self.error,
            "outcome": self.outcome,
            "admission_refused": self.admission_refused,
            "sentiment": (
                {"category": self.sentiment_category} if self.sentiment_category else {}
            ),
            "dropped_marks": self.dropped_marks,
            "clock_skew": self.clock_skew,
            "at": self.at,
        }


class TraceSink(Protocol):
    def write(self, trace: TurnTrace) -> None: ...


class JsonlSink:
    """Append-only JSONL under the instance-scoped log directory.

    The path is passed in; this module never derives it, because the isolation
    requirement (`BRD-18`) says telemetry must not land in a shared directory
    and the safe source for that path is the config, not a default here.

    Rotates at 100 MB keeping 7 files (TAC-5). The rotating file is renamed
    rather than truncated, so a reader that already opened it keeps a
    consistent view, and `TraceWriter` counts a rotation failure as a dropped
    write rather than losing the turn silently.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        rotate_bytes: int = ROTATE_BYTES,
        keep_files: int = KEEP_FILES,
    ) -> None:
        self.path = Path(path)
        self.rotate_bytes = rotate_bytes
        self.keep_files = keep_files

    def write(self, trace: TurnTrace) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_if_needed()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(trace.as_dict(), ensure_ascii=False) + "\n")

    def _rotate_if_needed(self) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size < self.rotate_bytes:
            return

        def numbered(index: int) -> Path:
            return self.path.with_name(f"{self.path.name}.{index}")

        oldest = numbered(self.keep_files)
        if oldest.exists():
            oldest.unlink()
        for index in range(self.keep_files - 1, 0, -1):
            source = numbered(index)
            if source.exists():
                os.replace(source, numbered(index + 1))
        os.replace(self.path, numbered(1))


class MemorySink:
    def __init__(self) -> None:
        self.traces: list[TurnTrace] = []

    def write(self, trace: TurnTrace) -> None:
        self.traces.append(trace)


class TraceWriter:
    """Emits turn traces. Never raises, never blocks a turn.

    Every failure — a full disk, a missing directory, a broken sink — is
    swallowed and logged. The alternative is a product that stops answering
    because its metrics pipeline is unhappy.
    """

    def __init__(self, sink: TraceSink | None = None, *, sample_rate: float = 1.0) -> None:
        self.sink = sink or MemorySink()
        self.sample_rate = sample_rate
        self._counter = 0
        self._lock = threading.Lock()
        self.dropped = 0

    def start(self, **kwargs) -> TurnTrace:
        return TurnTrace(trace_id=str(uuid.uuid4()), **kwargs)

    def emit(self, trace: TurnTrace) -> bool:
        """Write a trace. Returns whether it was written — never raises."""
        try:
            with self._lock:
                self._counter += 1
                index = self._counter
            if self.sample_rate < 1.0 and (index % max(1, int(1 / self.sample_rate))) != 0:
                return False
            self.sink.write(trace)
            return True
        except Exception:  # noqa: BLE001 - telemetry must never fail a turn
            self.dropped += 1
            logger.warning("trace dropped", exc_info=True)
            return False

    def stats(self) -> dict:
        return {"emitted": self._counter, "dropped": self.dropped}


# ---------------------------------------------------------------------------
# Metrics — absence is data
# ---------------------------------------------------------------------------
@dataclass
class Metric:
    """A measurement that may genuinely not exist.

    ``value is None`` means *not measured*. A dashboard rendering that as ``0``
    reports "all clear" during an outage, which is the failure this type exists
    to prevent.
    """

    name: str
    value: float | None = None
    unit: str = ""
    measured_at: str | None = None

    @property
    def is_absent(self) -> bool:
        return self.value is None

    def render(self) -> str:
        if self.is_absent:
            return f"{self.name}: unavailable"
        return f"{self.name}: {self.value}{self.unit}"


class MetricsRegistry:
    """Holds current metric values, distinguishing absent from zero."""

    def __init__(self) -> None:
        self._metrics: dict[str, Metric] = {}

    def set(self, name: str, value: float, unit: str = "") -> None:
        self._metrics[name] = Metric(
            name=name, value=float(value), unit=unit,
            measured_at=datetime.now(timezone.utc).isoformat(),
        )

    def mark_unavailable(self, name: str, unit: str = "") -> None:
        """Explicitly record that a metric could not be read."""
        self._metrics[name] = Metric(name=name, value=None, unit=unit)

    def get(self, name: str) -> Metric:
        return self._metrics.get(name, Metric(name=name))

    def render_all(self) -> list[str]:
        return [metric.render() for metric in self._metrics.values()]


# ---------------------------------------------------------------------------
# Frozen evaluation set (BRD-22)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EvalCase:
    """One question with its known-correct answer."""

    case_id: str
    question: str
    # Substrings that must appear in a correct answer. Deliberately substring
    # rather than exact-match: the wording may vary, the figure may not.
    must_contain: tuple[str, ...] = ()
    # Substrings that must NOT appear — typically a wrong figure from a
    # neighbouring product, which is the error the corpus makes possible.
    must_not_contain: tuple[str, ...] = ()
    product_id: str | None = None
    severity: str = "critical"     # critical | normal

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    reason: str = ""
    answer: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class GateReport:
    """The outcome of running the frozen set."""

    passed: bool
    total: int
    failures: list[CaseResult] = field(default_factory=list)
    grounded_rate: float | None = None
    reason: str = ""
    duration_ms: float = 0.0
    #: TAC-1: the digest of the exact set this run scored against.
    set_sha256: str = ""

    @property
    def failed_ids(self) -> list[str]:
        return [failure.case_id for failure in self.failures]

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "total": self.total,
            "failed": self.failed_ids,
            "grounded_rate": self.grounded_rate,
            "reason": self.reason,
            "duration_ms": round(self.duration_ms, 1),
            "set_sha256": self.set_sha256,
        }


# Grouping separators that carry no numeric meaning: ASCII comma and space,
# and the non-breaking space some locale formatters emit.
_SEPARATORS = str.maketrans("", "", ",   ")


def _ungrouped(text: str) -> str:
    return (text or "").translate(_SEPARATORS).lower()


def run_case(case: EvalCase, answer: str) -> CaseResult:
    """Check one answer against its expectations.

    A numeric expectation is compared with digit grouping removed. The catalogue
    is Indian, so a price renders with lakh/crore grouping — ₹13,85,000 — while
    the case is generated from the corpus with the plain digits 1385000. Testing
    the literal string made the gate measure *formatting*, not accuracy: all 24
    price cases failed while every answer stated the correct figure. A gate that
    fails when the product is right is a gate someone switches off.
    """
    text = (answer or "").lower()
    ungrouped = _ungrouped(answer)
    for required in case.must_contain:
        if required.lower() in text:
            continue
        # Only a purely numeric expectation is allowed to match this way; a
        # phrase must still appear as written.
        digits = required.translate(_SEPARATORS)
        if digits.isdigit() and digits in ungrouped:
            continue
        return CaseResult(case.case_id, False, f"missing {required!r}", answer)
    for forbidden in case.must_not_contain:
        if forbidden.lower() in text:
            return CaseResult(case.case_id, False, f"contains forbidden {forbidden!r}", answer)
    return CaseResult(case.case_id, True, "", answer)


class QualityGate:
    """Runs the frozen set against a candidate and decides pass or fail."""

    def __init__(
        self,
        cases: Sequence[EvalCase],
        *,
        baseline_grounded_rate: float | None = None,
        min_grounded_rate: float = MIN_GROUNDED_RATE,
        max_regression: float = MAX_REGRESSION,
        set_sha256: str = "",
    ) -> None:
        self.cases = list(cases)
        self.baseline_grounded_rate = baseline_grounded_rate
        self.min_grounded_rate = min_grounded_rate
        self.max_regression = max_regression
        self.set_sha256 = set_sha256

    def evaluate(self, answerer: Callable[[EvalCase], str]) -> GateReport:
        """Run every case. A case that errors counts as a failure, not a skip.

        Skipping an erroring case would let a broken configuration report 100%
        on the cases that happened to survive it.
        """
        started = time.monotonic()
        failures: list[CaseResult] = []

        for case in self.cases:
            try:
                answer = answerer(case)
            except Exception as exc:  # noqa: BLE001 - an error is a failure
                failures.append(
                    CaseResult(case.case_id, False, f"raised {type(exc).__name__}: {exc}")
                )
                continue

            result = run_case(case, answer)
            if not result.passed:
                failures.append(result)

        total = len(self.cases)
        grounded_rate = (total - len(failures)) / total if total else None
        duration = (time.monotonic() - started) * 1000

        report = GateReport(
            passed=False,   # derived below; never assume a verdict before the rules run
            total=total,
            failures=failures,
            grounded_rate=grounded_rate,
            duration_ms=duration,
            set_sha256=self.set_sha256,
        )

        if not total:
            report.passed = False
            report.reason = "evaluation set is empty — a gate with no cases is not a gate"
            return report

        critical_failures = [
            failure for failure in failures
            if any(case.case_id == failure.case_id and case.severity == "critical"
                   for case in self.cases)
        ]
        if critical_failures:
            report.passed = False
            report.reason = (
                f"{len(critical_failures)} critical case(s) failed: "
                f"{[f.case_id for f in critical_failures]}"
            )
            return report

        if grounded_rate is not None and grounded_rate < self.min_grounded_rate:
            report.passed = False
            report.reason = (
                f"grounded rate {grounded_rate:.1%} below floor {self.min_grounded_rate:.0%}"
            )
            return report

        if self.baseline_grounded_rate is not None and grounded_rate is not None:
            regression = self.baseline_grounded_rate - grounded_rate
            if regression > self.max_regression:
                report.passed = False
                report.reason = (
                    f"regression of {regression:.1%} against baseline "
                    f"{self.baseline_grounded_rate:.1%}"
                )
                return report

        # Reached only when no critical case failed and both floors hold. A
        # non-critical failure inside the tolerance passes — that is what a
        # 98 % floor means — but the report must *say so* rather than claiming
        # every case passed. The unconditional `passed = True` that used to sit
        # here reported a run with failures as a clean sweep (US-027).
        report.passed = True
        if failures:
            report.reason = (
                f"{total - len(failures)} of {total} cases passed; "
                f"{len(failures)} non-critical failure(s) within the "
                f"{1 - self.min_grounded_rate:.0%} tolerance: {report.failed_ids}"
            )
        else:
            report.reason = f"all {total} cases passed"
        return report


def eval_set_sha256(path: Path | str) -> str:
    """The frozen set's digest, so a promoted release can be tied to the exact
    question set it passed (TAC-1)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_eval_set(path: Path | str) -> list[EvalCase]:
    """Load a frozen set from JSONL."""
    cases: list[EvalCase] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            cases.append(
                EvalCase(
                    case_id=data["case_id"],
                    question=data["question"],
                    must_contain=tuple(data.get("must_contain", ())),
                    must_not_contain=tuple(data.get("must_not_contain", ())),
                    product_id=data.get("product_id"),
                    severity=data.get("severity", "critical"),
                )
            )
    return cases


def cases_from_corpus(corpus, *, limit: int | None = None) -> list[EvalCase]:
    """Derive a starting evaluation set from the catalogue's own FAQs.

    The published FAQs are the ground truth the site itself asserts, which makes
    them the least arguable starting point — and `EvalCase` is frozen so a later
    edit is a deliberate act rather than drift. `must_not_contain` guards the
    duplicate-name trap: a correct answer about one "Medical Dry Imager" must
    not carry the other's price.
    """
    cases: list[EvalCase] = []

    for product in corpus.all_products():
        if product.price_display:
            # Two products share a name at different prices; each must not
            # answer with the other's figure.
            others = [
                other.price_display for other in corpus.all_products()
                if other.name == product.name and other.product_id != product.product_id
                and other.price_display
            ]
            cases.append(
                EvalCase(
                    case_id=f"price:{product.product_id}",
                    question=f"What is the price of the {product.name} ({product.product_id})?",
                    must_contain=(str(product.price_amount),),
                    must_not_contain=tuple(str(o) for o in others if o != product.price_display),
                    product_id=product.product_id,
                    severity="critical",
                )
            )

        if product.moq_value is not None:
            cases.append(
                EvalCase(
                    case_id=f"moq:{product.product_id}",
                    question=f"What is the minimum order quantity for the {product.name}?",
                    must_contain=(f"{product.moq_value:g}",),
                    product_id=product.product_id,
                )
            )

    cases.extend(_routing_cases(corpus))
    return cases[:limit] if limit else cases


@dataclass(frozen=True)
class BrowserReport:
    """The outcome of the Playwright scenario suite."""

    passed: bool
    total: int = 0
    failed: tuple[str, ...] = ()
    reason: str = ""
    duration_s: float = 0.0

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "total": self.total,
            "failed": list(self.failed),
            "reason": self.reason,
            "duration_s": round(self.duration_s, 1),
        }


@dataclass(frozen=True)
class SystemReport:
    """The outcome of the system integrity check.

    Three layers that fail independently: modules load, dependencies are wired,
    use cases are reachable. The third is the one nothing else catches — a
    feature can be complete, tested, and reachable from no surface, which
    happened nine times in this build.
    """

    passed: bool
    loaded: int = 0
    wired: int = 0
    broken: tuple[str, ...] = ()
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "loaded": self.loaded,
            "wired": self.wired,
            "broken": list(self.broken),
            "reason": self.reason,
        }


def combine_validation(
    gate: "GateReport",
    browser: BrowserReport | None,
    system: SystemReport | None = None,
    *,
    citations: dict | None = None,
) -> dict:
    """One promotion decision, from both evidence sources.

    `IndexLifecycle.promote` already refuses anything without ``passed: True``.
    The gap was never the enforcement point — it was that the dict reaching it
    came from the in-process evaluation set alone, which asserts answer *content*
    and cannot assert routing, refusals, consent capture or whether the page
    works in a browser. Those are the failures that actually shipped.

    So the two sources are combined here, and an unrun browser suite counts as
    a **failure**, not a pass. "We did not check" and "the check passed" must
    not produce the same promotion.

    ``browser=None`` is that case and it is deliberate: a caller who has not run
    the scenarios has no evidence to contribute, and silence is not evidence.
    """
    # Three sources, and a missing one is always a failure. "We did not check"
    # and "the check passed" must never produce the same promotion — that is
    # the whole reason this function exists rather than `and`-ing in the caller.
    missing = [
        name for name, report in (("browser", browser), ("system", system))
        if report is None
    ]
    citation_failure = _citation_failure(citations)
    if missing:
        return {
            **gate.as_dict(),
            "passed": False,
            "reason": (
                f"{' and '.join(missing)} check(s) did not run — promotion "
                f"requires the evaluation set, the browser suite and the system check"
            ),
            "browser": browser.as_dict() if browser else None,
            "system": system.as_dict() if system else None,
            "citations": citations,
        }

    passed = (
        bool(gate.passed) and bool(browser.passed) and bool(system.passed)
        and citation_failure is None
    )
    if not gate.passed:
        reason = gate.reason or "evaluation set failed"
    elif not browser.passed:
        if browser.total == 0:
            # It did not run at all — an environment problem, and the reason
            # names it. "failed: []" reads as a product failure with no
            # failures, which is the confusion §8.19 exists to remove.
            reason = f"browser suite did not run — {browser.reason or 'no reason given'}"
        else:
            reason = f"browser scenarios failed: {list(browser.failed)[:5]}"
    elif not system.passed:
        # Named, not counted: "6 use cases unreachable" tells an operator to go
        # and look. "system check failed" does not.
        reason = f"system check failed: {', '.join(system.broken[:8])}"
    elif citation_failure:
        reason = citation_failure
    else:
        reason = (
            f"all {gate.total} cases, {browser.total} scenarios and "
            f"{system.wired} dependencies verified"
        )

    return {
        **gate.as_dict(),
        "passed": passed,
        "reason": reason,
        "browser": browser.as_dict(),
        "system": system.as_dict(),
        "citations": citations,
    }


def _citation_failure(citations: dict | None) -> str | None:
    """Why the citation counts block promotion, or `None` when they do not.

    US-012 `TAC-1`/`TAC-2`: a grounded answer with no citation, or a citation
    that could not be completed, is a composition defect. The gate used to
    compute and print these counts and then drop them before the verdict
    (`§8.24`) — visible in a log, unable to refuse anything. A count that
    cannot fail a release is documentation, not a check.
    """
    if not citations:
        return None
    if citations.get("error"):
        return f"citation check did not complete — {citations['error']}"
    uncited = int(citations.get("uncited") or 0)
    incomplete = int(citations.get("incomplete") or 0)
    if uncited or incomplete:
        return (
            f"citation integrity failed: {uncited} uncited, {incomplete} incomplete "
            f"of {int(citations.get('grounded') or 0)} grounded answers"
        )
    return None


def _routing_cases(corpus) -> list[EvalCase]:
    """Cases for everything the catalogue cannot assert about itself.

    The corpus-derived cases above cover `commercial` and nothing else — 1 of
    the 9 intents the platform routes. That is why 48/48 green coexisted with
    "where is your office?" being unanswerable, with a buyer's "yes" being
    declined, and with greetings being answered as though they were product
    questions. A case set drawn only from the catalogue can only ever certify
    the catalogue.

    These cases are drawn from **what a buyer types** instead, and their
    answers are deterministic, so they assert exactly rather than by rubric —
    no judge model, therefore no judge bias to calibrate.

    Each group names the defect it would have caught.
    """
    cases: list[EvalCase] = []

    # Every one of these was answered with the catalogue decline.
    for case_id, question, must in (
        ("help:capability", "what can you do", ("specification", "price")),
        ("help:identity", "who are you", ("catalogue",)),
        ("listing:what-do-you-sell", "what do you sell", ("catalogue lists",)),
        ("listing:list-products", "list out all products", ("categories",)),
        ("company:address", "where is your office?", ("Mumbai",)),
        ("company:phone", "what is your phone number?", ("8045477795",)),
        ("company:certifications", "are you ISO certified?", ("ISO 9001",)),
        ("company:gst", "what is your GST number?", ("AAACA3337R",)),
        ("company:directors", "who is your managing director?", ("Chikramane",)),
    ):
        cases.append(EvalCase(
            case_id, question, must_contain=must,
            must_not_contain=("don't have that in our catalogue",),
        ))

    # Conversational scaffolding — the largest single group of failures.
    for case_id, question, must in (
        ("social:greeting", "hello", ("hello",)),
        ("social:introduction", "I am Dr Pradeep", ("Dr Pradeep",)),
        ("social:thanks", "thanks", ("welcome",)),
        ("social:signoff", "bye", ("goodbye",)),
    ):
        cases.append(EvalCase(
            case_id, question, must_contain=must,
            must_not_contain=("don't have that in our catalogue",),
        ))

    # Refusals. Each must refuse for its own reason — a single refusal string
    # for every failure is what made the others hard to see.
    cases.append(EvalCase(
        "refusal:clinical", "is this safe for a pacemaker patient?",
        must_contain=("medical advice", "clinician"),
    ))
    cases.append(EvalCase(
        "refusal:off-topic", "who won the world cup?",
        must_contain=("don't have that in our catalogue",),
    ))
    # A product we do not sell must not be answered with a different product.
    cases.append(EvalCase(
        "refusal:unknown-product", "what is the price of the MRI machine you sell?",
        must_contain=("don't have that in our catalogue",),
        must_not_contain=("cassette", "gel"),
    ))

    # The duplicate-name trap, asserted as behaviour rather than as a figure.
    cases.append(EvalCase(
        "ambiguity:shared-name", "what is the price of the medical dry imager?",
        must_contain=("which one did you mean",),
    ))

    # Comparison. `compare()` existed and was unit-tested and reachable from
    # nowhere: a comparison question reported two matches and asked the buyer
    # to choose between the two products they had just asked us to compare.
    cases.append(EvalCase(
        "compare:by-code",
        "what is the difference between the APO 200 and the APO 100?",
        must_contain=("APO 200", "APO 100"),
        must_not_contain=("which one did you mean",),
    ))
    cases.append(EvalCase(
        "compare:by-name",
        "compare the Ultrasound Gel and the X-Ray Cassette",
        must_contain=("Ultrasound Gel", "X-Ray Cassette"),
        must_not_contain=("which one did you mean",),
    ))

    # Lead capture. A product question must never be read as a phone number —
    # "APO 200 (6835421)" once matched the phone pattern.
    cases.append(EvalCase(
        "lead:consent", "do you sell MRI machines?",
        must_contain=("get in touch",),
    ))
    # Consent that is not actionable. With nothing offered, "yes please" was
    # classified CONSENT, had no branch, fell through to retrieval, was gated
    # out, and answered with the catalogue decline.
    cases.append(EvalCase(
        "consent:bare-yes", "yes please",
        must_not_contain=("don't have that in our catalogue", "number or email"),
    ))
    cases.append(EvalCase(
        "lead:not-a-phone",
        "what is the price of the Apollo RT CT Dual Head Pressure Injector APO 200?",
        must_not_contain=("passed your details",),
    ))

    return cases
