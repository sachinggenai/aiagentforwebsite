"""Persistence behind a Protocol.

The two products persist differently and neither arrangement belongs in a shared
package: the source platform writes to PostgreSQL with its own schema; ANITA has
no server database at all. A Protocol lets each supply what it has.

`InMemorySentimentStore` ships because a product with no database still needs
somewhere for the core to record what it observed — and because a test double
that is part of the package is one every consumer can rely on rather than each
writing its own.

**No module-level singleton.** The core takes a store as a constructor argument.
A global store would be shared by every product in a process, which is exactly
what this package exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable


class StoreUnavailable(RuntimeError):
    """The store could not be read or written.

    Distinct from returning empty: a caller must be able to tell "no scores yet"
    from "the database is down", because those demand opposite responses.
    """


@dataclass(frozen=True)
class StoredScore:
    """One scored turn, as persisted."""

    lead_id: str
    score: float
    category: str
    emotion: str
    friction: float
    intent: float
    delta: float
    at: str = ""
    objections: tuple[str, ...] = ()


@dataclass
class LeadSentimentState:
    """A lead's running sentiment: the EWMA baseline and the recent trajectory."""

    lead_id: str
    baseline: float | None = None
    latest: float | None = None
    turn_count: int = 0
    category: str = ""
    escalated: bool = False
    scores: list[StoredScore] = field(default_factory=list)

    @property
    def at_risk(self) -> bool:
        return self.category == "At-Risk"


@runtime_checkable
class SentimentStore(Protocol):
    """What the core needs from persistence. Four methods, no more.

    Deliberately small: every method here is one the core actually calls, so a
    second product implements four things rather than a schema.
    """

    def save(self, score: StoredScore) -> None: ...

    def for_lead(self, lead_id: str) -> LeadSentimentState | None: ...

    def recent_scores(self, lead_id: str, limit: int = 10) -> Sequence[StoredScore]: ...

    def count_labelled_outcomes(self) -> int:
        """How many outcomes are labelled — the sample size a predictive layer
        would need.

        This is the method the source's unreachable-Hot defect needed and did not
        have: it compared a 10-row fetch against a threshold of 100. Exposing the
        count as its own call makes that comparison a units match rather than a
        coincidence.
        """
        ...


class InMemorySentimentStore:
    """A working store with no dependency. State lives as long as the instance."""

    def __init__(self) -> None:
        self._by_lead: dict[str, LeadSentimentState] = {}
        self._labelled = 0

    def save(self, score: StoredScore) -> None:
        state = self._by_lead.setdefault(
            score.lead_id, LeadSentimentState(lead_id=score.lead_id)
        )
        state.scores.append(score)
        state.latest = score.score
        state.turn_count = len(state.scores)
        state.category = score.category
        if score.score is not None:
            state.baseline = score.score if state.baseline is None else state.baseline

    def for_lead(self, lead_id: str) -> LeadSentimentState | None:
        return self._by_lead.get(lead_id)

    def recent_scores(self, lead_id: str, limit: int = 10) -> Sequence[StoredScore]:
        state = self._by_lead.get(lead_id)
        if state is None:
            return ()
        return tuple(state.scores[-limit:])

    def count_labelled_outcomes(self) -> int:
        return self._labelled

    def mark_labelled(self, count: int = 1) -> None:
        """Record that outcomes were labelled. The predicate that consumes this
        belongs to the product, not to the store."""
        self._labelled += count


__all__ = [
    "SentimentStore", "StoredScore", "LeadSentimentState",
    "InMemorySentimentStore", "StoreUnavailable",
]
